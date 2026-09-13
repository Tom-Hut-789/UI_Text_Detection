"""AI 代理层：AsyncOpenAI 客户端 + 全局并发限流 + 指数退避重试 + 结构化输出解析。

对应 SDD 2.3 / 7.2 §2：
  * `asyncio.Semaphore(N)` 限制全局在途请求数，避免触发网关限流；
  * `tenacity` 指数退避重试，仅对**瞬时错误**（限流、超时、连接失败、5xx）生效；
  * 强制模型返回严格 JSON，并做容错解析（网关不支持 json_schema 时自动降级）。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from pathlib import Path

import openai
from openai import AsyncOpenAI
from PIL import Image
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..config import settings
from ..models import DefectVerdict
from . import prompts

logger = logging.getLogger(__name__)

# 仅这些异常值得重试；参数错误、鉴权失败重试没有意义
RETRYABLE = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, RETRYABLE):
        return True
    # 网关把限流包成 5xx / 429 但不是标准异常类时兜底
    status = getattr(exc, "status_code", None)
    return status in (408, 409, 429, 500, 502, 503, 504)


class ImageEncoder:
    """把磁盘上的截图编码成 data URL，并在编码前做降采样。

    Excel 里的截图往往是 1200×2600+ 的超长图，原样直传既贵又容易触发尺寸限制。
    """

    def __init__(self, max_side: int | None = None, max_bytes: int | None = None):
        self.max_side = max_side or settings.image_max_side
        self.max_bytes = max_bytes or settings.image_max_bytes

    def encode(self, path: str | Path) -> str:
        path = Path(path)
        raw = path.read_bytes()
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception:
            logger.warning("图片无法解码，按原始字节发送: %s", path)
            return self._data_url(raw, "image/png")

        fmt = (img.format or "PNG").upper()
        changed = fmt not in ("PNG", "JPEG")
        if changed:
            img = img.convert("RGB")
            fmt = "PNG"

        buf = self._render(img, fmt)
        # 仍然超限时按 0.8 比例逐次降采样，直到满足预算
        scale = 1.0
        while len(buf) > self.max_bytes and scale > 0.2:
            scale *= 0.8
            smaller = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.LANCZOS,
            )
            buf = self._render(smaller, fmt)
        img.close()

        mime = "image/png" if fmt == "PNG" else "image/jpeg"
        return self._data_url(buf, mime)

    def _render(self, img: Image.Image, fmt: str) -> bytes:
        out = io.BytesIO()
        if self.max_side and max(img.width, img.height) > self.max_side:
            ratio = self.max_side / max(img.width, img.height)
            img = img.resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                Image.LANCZOS,
            )
        if fmt == "JPEG":
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(out, format="JPEG", quality=88, optimize=True)
        else:
            img.save(out, format="PNG", optimize=True)
        return out.getvalue()

    @staticmethod
    def _data_url(blob: bytes, mime: str) -> str:
        return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


def _check_finish_reason(resp) -> None:
    """把 finish_reason 翻译成可读的报错。

    推理型模型会把 `reasoning_tokens` 计入 max_tokens：额度不够时 finish_reason
    就是 `length`，且 message.content 可能为空字符串。若不显式检查，这种情况只会
    表现为「模型返回为空」，让人误以为是网关或 Prompt 的问题。
    """
    choice = resp.choices[0]
    if choice.finish_reason != "length":
        return
    usage = getattr(resp, "usage", None)
    reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
    hint = (
        f"（其中推理 token {reasoning}）" if reasoning else ""
    )
    raise RuntimeError(
        f"模型回复被 max_tokens={settings.llm_max_tokens} 截断{hint}，未产出可解析的结论。"
        "请调大 .env 中的 LLM_MAX_TOKENS。"
    )


def _log_usage(resp, model: str) -> None:
    """记录 Token 用量，便于估算整批任务的成本。"""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    logger.info(
        "LLM 用量 model=%s prompt=%s completion=%s total=%s",
        model,
        getattr(usage, "prompt_tokens", "?"),
        getattr(usage, "completion_tokens", "?"),
        getattr(usage, "total_tokens", "?"),
    )


def parse_verdict_json(content: str) -> DefectVerdict:
    """容错解析模型返回：剥离 ```json 围栏、截取首尾大括号、兜底正则抽布尔字段。"""
    if not content or not content.strip():
        raise ValueError("模型返回为空")

    text = content.strip()
    fence = _JSON_FENCE.search(text)
    if fence:
        text = fence.group(1).strip()

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = _regex_fallback(text)

    if not isinstance(payload, dict):
        raise ValueError(f"模型返回不是 JSON 对象: {content[:200]}")

    # 只保留模型认识的字段，避免网关多返回的字段触发校验错误
    allowed = set(DefectVerdict.model_fields)
    payload = {k: v for k, v in payload.items() if k in allowed}
    return DefectVerdict.model_validate(payload)


def _regex_fallback(text: str) -> dict:
    # 一个能识别出的字段都没有，说明模型根本没按 JSON 契约作答（返回了散文、
    # 拒绝回答、或内容被截断）。此时**必须报错**：若返回全 false 的默认值，
    # 一张有缺陷的截图就会被静默记成「无缺陷」，是最危险的漏报来源。
    if not any(
        key in text
        for key in ("has_truncation", "has_overlap", "has_missing_glyph", "is_ui_screenshot")
    ):
        raise ValueError(f"模型未返回可解析的 JSON 结论，原始返回：{text[:300]}")

    def flag(name: str) -> bool:
        m = re.search(rf'"{name}"\s*:\s*(true|false)', text, re.IGNORECASE)
        return bool(m and m.group(1).lower() == "true")

    def string(name: str) -> str:
        m = re.search(rf'"{name}"\s*:\s*"(.*?)"\s*[,\}}]', text, re.DOTALL)
        return m.group(1) if m else ""

    severity_match = re.search(r'"severity"\s*:\s*"(\w+)"', text)
    severity = severity_match.group(1).lower() if severity_match else "low"

    return {
        "is_ui_screenshot": not re.search(r'"is_ui_screenshot"\s*:\s*false', text, re.I),
        "extracted_text": string("extracted_text"),
        "has_truncation": flag("has_truncation"),
        "has_overlap": flag("has_overlap"),
        "has_missing_glyph": flag("has_missing_glyph"),
        "severity": severity,
        "reason": string("reason") or "模型返回非标准 JSON，已按关键字兜底解析",
    }


class LLMClient:
    """多模态检测客户端。全进程共用一个实例，由内部信号量控制并发。"""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._semaphore = asyncio.Semaphore(max(1, settings.llm_concurrency))
        self._encoder = ImageEncoder()
        # 网关一旦拒绝某种 response_format，后续请求直接降级，避免每次都多打一次无效请求
        self._json_schema_supported = True
        self._json_object_supported = True
        self._lock = asyncio.Lock()

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not settings.llm_configured:
                raise RuntimeError(
                    "未配置 OPENAI_API_KEY，请在项目根目录 .env 中填写，或设置 MOCK_LLM=true 使用模拟模式"
                )
            kwargs: dict = {"api_key": settings.openai_api_key, "timeout": settings.llm_timeout}
            if settings.openai_base_url:
                kwargs["base_url"] = settings.openai_base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def detect(
        self,
        image_path: str | Path,
        prompt: str,
        *,
        is_english: bool = False,
        reference_image_path: str | Path | None = None,
    ) -> DefectVerdict:
        """检测单张截图。

        传入 `reference_image_path` 时走**双图对比**：把同一控件的英文基准截图作为
        IMAGE 1、待检测截图作为 IMAGE 2 一起发送，让模型做几何对照——截断本质是几何
        问题（同一控件里英文占 60% 宽度、译文占 118%），只比较文本长度会丢掉这个证据。
        """
        if settings.mock_llm:
            return await self._mock_detect(image_path, is_english=is_english)

        # 两张图都在重试循环**之外**编码：重试针对的是网络侧瞬时错误，图片本身没变，
        # 放进循环里会让每次重试都重做一遍读盘 + 降采样 + Base64。
        if reference_image_path is not None:
            data_url, ref_data_url = await asyncio.gather(
                asyncio.to_thread(self._encoder.encode, image_path),
                asyncio.to_thread(self._encoder.encode, reference_image_path),
            )
        else:
            data_url = await asyncio.to_thread(self._encoder.encode, image_path)
            ref_data_url = None

        async with self._semaphore:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max(1, settings.llm_max_retries)),
                wait=wait_exponential_jitter(initial=1, max=30),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    return await self._call_model(data_url, prompt, ref_data_url)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _call_model(
        self, data_url: str, prompt: str, ref_data_url: str | None = None
    ) -> DefectVerdict:
        detail = settings.llm_image_detail
        content: list[dict] = []
        if ref_data_url is not None:
            # 图片之间插入标签，避免模型把「参照图」和「待检图」弄反——顺序错了会直接把
            # 英文截图自身的排版问题报成小语种缺陷。
            content.append({"type": "text", "text": prompts.IMAGE_1_LABEL})
            content.append(
                {"type": "image_url", "image_url": {"url": ref_data_url, "detail": detail}}
            )
            content.append({"type": "text", "text": prompts.IMAGE_2_LABEL})
        # 无参考图时这一段的顺序与改造前**完全一致**（先图后文），单图路径行为不变
        content.append({"type": "image_url", "image_url": {"url": data_url, "detail": detail}})
        content.append({"type": "text", "text": prompt})

        messages = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]
        base: dict = {
            "model": settings.llm_model,
            "messages": messages,
            "temperature": settings.temperature,
            "max_tokens": settings.llm_max_tokens,
        }
        if settings.llm_disable_thinking:
            # 关掉推理，避免在文字密集的截图上陷入推理循环把额度耗光（详见 config.py 注释）。
            # 用 extra_body 而非顶层参数：它不在 OpenAI 协议里，是网关的扩展字段。
            base["extra_body"] = {"thinking": {"type": "disabled"}}

        if self._json_schema_supported:
            try:
                return await self._request(
                    base,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "ui_defect_verdict",
                            "strict": True,
                            "schema": prompts.VERDICT_JSON_SCHEMA,
                        },
                    },
                )
            except openai.BadRequestError as exc:
                async with self._lock:
                    self._json_schema_supported = False
                logger.warning("网关不支持 response_format=json_schema，降级为 json_object：%s", exc)

        if self._json_object_supported:
            try:
                return await self._request(base, response_format={"type": "json_object"})
            except openai.BadRequestError as exc:
                # 有些网关（如 SiliconFlow 上的部分模型）**整个 JSON 模式都不支持**，
                # 400 提示 "Json mode is not supported for this model."。
                # 只降级到 json_object 是不够的——那样每次调用都会以 400 失败。
                async with self._lock:
                    self._json_object_supported = False
                logger.warning(
                    "网关不支持 response_format=json_object，改为不带 response_format 请求"
                    "（Prompt 已写明 JSON 契约，解析层另有容错）：%s",
                    exc,
                )

        # 最后一档：完全不指定 response_format。Prompt 里的 JSON 结构说明与
        # parse_verdict_json 的围栏剥离 / 正则兜底共同保证仍能取到结论。
        return await self._request(base, response_format=None)

    async def _request(self, base: dict, *, response_format: dict | None) -> DefectVerdict:
        """发一次请求并解析结论；`response_format=None` 表示完全不指定。"""
        kwargs = dict(base)
        if response_format is not None:
            kwargs["response_format"] = response_format
        resp = await self.client.chat.completions.create(**kwargs)
        _log_usage(resp, base["model"])
        _check_finish_reason(resp)
        return parse_verdict_json(resp.choices[0].message.content or "")

    async def _mock_detect(self, image_path: str | Path, *, is_english: bool) -> DefectVerdict:
        """模拟模式：不产生任何网络请求，用于打通全链路与前端联调。"""
        await asyncio.sleep(0.4)
        name = Path(image_path).stem
        seed = sum(ord(c) for c in name)
        has_defect = seed % 3 == 0
        return DefectVerdict(
            is_ui_screenshot=True,
            extracted_text=f"[MOCK] text of {name}",
            has_truncation=has_defect and seed % 2 == 0,
            has_overlap=has_defect and seed % 2 == 1,
            has_missing_glyph=False,
            severity="medium" if has_defect else "none",
            reason="MOCK 模式生成的模拟结论" if has_defect else "MOCK 模式：未发现缺陷",
        )


llm_client = LLMClient()
