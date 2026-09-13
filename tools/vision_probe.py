#!/usr/bin/env python
"""多模态能力探针：用一张**内容已知**的合成图片，确定性地验证模型到底能不能看见图。

为什么不直接用真实业务截图判断？
  因为真实截图里有没有文字、文字是什么，你事先并不知道，模型胡诌一段你也无从证伪。
  本脚本现场画一张写着已知字符串的图，模型要么读出来，要么读不出来，结论非黑即白。

判定依据有两条，缺一不可：
  1. **语义**：模型能否原样报出图中的已知文字；
  2. **计量**：带图请求的 prompt_tokens 是否显著高于纯文本请求。
     真正的视觉模型会把图片编码成数百个 image token；非多模态网关则直接丢弃图片，
     token 数几乎不变（这是最客观、无法伪装的证据）。

用法：
    python tools/vision_probe.py                # 探测 .env 中配置的模型
    python tools/vision_probe.py --all          # 探测该网关下所有可用模型
    python tools/vision_probe.py --dual         # 验证多图输入（双图对比功能的前置条件）
    python tools/vision_probe.py --model x --base-url y --api-key z
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，编不出下文要打印的 ✓ ✗ ✅ ❌ 等符号；一旦输出被重定向到管道或文件，
# Python 会按系统编码构造 stdout，print 直接抛 UnicodeEncodeError，把一次正常探测变成 traceback。
# 只放宽错误处理、不改编码，这样 GBK 控制台不会出现乱码。
for _s in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_s, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(errors="replace")

import httpx  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from backend.app.config import settings  # noqa: E402

# 故意用无意义的随机串：模型无法靠语言先验猜出来，只能靠"看"
SECRET_LINE = "ZQ7K-4419-XM"
SECRET_WORD = "Kaleidoscope"
# 双图模式的第二张图必须用**另一个**随机串：两张图内容相同的话，
# 「只看到了第一张」和「两张都看到了」产生的结果完全一样，验证就失效了。
SECRET_LINE_2 = "MB3V-8820-QP"


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _closest(text: str, target: str) -> tuple[str, float]:
    """在 text 里找与 target 最像的词，返回 (该词, 相似度)。

    用来把「完全没看见图」和「看见了但读错一两个字符」区分开：合成图用的是 PIL 默认
    点阵字体，字形本就不好认，`ZQ7K` 读成 `ZO7K` 是 OCR 精度问题，不是视觉能力缺失。
    若不做这个区分，会把完全可用的模型误判成「不支持视觉」。
    """
    best, score = "", 0.0
    for token in text.split():
        cleaned = token.strip("\"'`,.:;()[]")
        current = _similarity(cleaned, target)
        if current > score:
            best, score = cleaned, current
    return best, score


def make_probe(lines: list[str]) -> str:
    """画一张写着指定文字的合成图，返回 data URL。"""
    img = Image.new("RGB", (640, 220), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 629, 209], outline="black", width=3)
    y = 50
    for text in lines:
        draw.text((32, y), text, fill="black")
        y += 50
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


async def probe(client: AsyncOpenAI, model: str, probe_url: str) -> dict:
    """对单个模型跑「纯文本 / 带图」两次请求，返回对比结果。"""
    result: dict = {"model": model}

    # --- 基线：纯文本，用于对照 token 计数 ---
    try:
        base = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Say OK."}],
            max_tokens=2200,
        )
        result["text_tokens"] = base.usage.prompt_tokens if base.usage else None
    except Exception as exc:
        result["error"] = f"纯文本调用失败: {type(exc).__name__}: {exc}"
        return result

    # --- 探针：带图，要求原样读出文字 ---
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": probe_url, "detail": "high"}},
                        {"type": "text", "text": "What exact text is written in this image? Answer briefly."},
                    ],
                }
            ],
            max_tokens=2200,
        )
    except Exception as exc:
        result["error"] = f"带图调用失败: {type(exc).__name__}: {exc}"
        return result

    content = (resp.choices[0].message.content or "").strip()
    result["vision_tokens"] = resp.usage.prompt_tokens if resp.usage else None
    result["reply"] = content
    result["finish_reason"] = resp.choices[0].finish_reason
    result["read_secret"] = SECRET_LINE.lower() in content.lower()
    result["read_word"] = SECRET_WORD.lower() in content.lower()
    near, similarity = _closest(content, SECRET_LINE)
    result["near_match"] = near
    result["secret_similarity"] = round(similarity, 2)

    delta = None
    if result["text_tokens"] is not None and result["vision_tokens"] is not None:
        delta = result["vision_tokens"] - result["text_tokens"]
    result["token_delta"] = delta

    # 判据同时看「读到了图里的字」与「图片确实被编码」。读对任一段已知文字即可——
    # 两段都是模型无从预知的，任一对上就只剩「看见了」一种解释。
    read_anything = result["read_secret"] or result["read_word"]
    result["vision_ok"] = bool(read_anything and delta is not None and delta > 100)
    return result


async def probe_dual(client: AsyncOpenAI, model: str, url_a: str, url_b: str) -> dict:
    """验证网关能否真正吃下**两张**图，并保留第二张。

    多图输入最危险的失效模式不是报错，而是**静默丢图**：网关只把第一张转给模型，
    请求照样 200、模型照样作答，看起来一切正常，实际只看了半张。
    所以这里用两张**内容不同**的图做双重保险：
      * 语义上——两个随机串必须都被读出来，只读到 A 说明 B 被丢了；
      * 计量上——双图 prompt_tokens 应约为单图的 2 倍（image token 是线性叠加的）。
    """
    result: dict = {"model": model}

    # --- 基线：只发 IMAGE 2，作为 token 增量的参照 ---
    try:
        single = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": url_b, "detail": "high"}},
                        {"type": "text", "text": "What exact text is written in this image? Answer briefly."},
                    ],
                }
            ],
            max_tokens=2200,
        )
        result["single_tokens"] = single.usage.prompt_tokens if single.usage else None
    except Exception as exc:
        result["error"] = f"单图基线调用失败: {type(exc).__name__}: {exc}"
        return result

    # --- 双图：一次请求发送两张不同的图 ---
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "IMAGE 1 and IMAGE 2 are two different screenshots."},
                        {"type": "image_url", "image_url": {"url": url_a, "detail": "high"}},
                        {"type": "text", "text": "IMAGE 2:"},
                        {"type": "image_url", "image_url": {"url": url_b, "detail": "high"}},
                        {
                            "type": "text",
                            "text": "What exact text is written in each image? "
                            "Answer briefly and label your answers IMAGE 1 and IMAGE 2.",
                        },
                    ],
                }
            ],
            max_tokens=2200,
        )
    except Exception as exc:
        result["error"] = f"双图调用失败: {type(exc).__name__}: {exc}"
        return result

    content = (resp.choices[0].message.content or "").strip()
    result["dual_tokens"] = resp.usage.prompt_tokens if resp.usage else None
    result["reply"] = content
    result["finish_reason"] = resp.choices[0].finish_reason
    result["read_a"] = SECRET_LINE.lower() in content.lower()
    result["read_b"] = SECRET_LINE_2.lower() in content.lower()
    near_a, sim_a = _closest(content, SECRET_LINE)
    near_b, sim_b = _closest(content, SECRET_LINE_2)
    result["near_a"], result["sim_a"] = near_a, round(sim_a, 2)
    result["near_b"], result["sim_b"] = near_b, round(sim_b, 2)
    # 与单图探针同样的放宽：读错一两个字符仍说明这张图**送达了**，
    # 而 --dual 要回答的恰恰是「第二张有没有被丢」，不该被 OCR 精度问题干扰。
    result["seen_a"] = bool(result["read_a"] or sim_a >= 0.7)
    result["seen_b"] = bool(result["read_b"] or sim_b >= 0.7)

    single_tokens = result["single_tokens"]
    dual_tokens = result["dual_tokens"]
    result["token_ratio"] = (
        round(dual_tokens / single_tokens, 2) if single_tokens and dual_tokens else None
    )
    # 两张图都被编码时，image token 应大致翻倍；用 +100 兜住 label 文本带来的零头
    result["second_image_encoded"] = bool(
        dual_tokens is not None and single_tokens is not None and dual_tokens - single_tokens > 100
    )
    result["dual_ok"] = bool(result["seen_a"] and result["seen_b"] and result["second_image_encoded"])
    return result


def report(r: dict) -> bool:
    print("\n" + "=" * 72)
    print(f"  模型: {r['model']}")
    print("=" * 72)
    if "error" in r:
        print(f"  ✗ {r['error']}")
        return False

    delta = r["token_delta"]
    print(f"  纯文本 prompt_tokens : {r['text_tokens']}")
    print(f"  带图   prompt_tokens : {r['vision_tokens']}  (Δ {delta:+})")
    print(f"  图片是否被编码       : {'是' if delta and delta > 100 else '否 —— token 几乎没变，图片被网关丢弃了'}")
    print(f"  finish_reason        : {r['finish_reason']}")
    print(f"  读到随机串 {SECRET_LINE} : {'✓ 是' if r['read_secret'] else '✗ 否'}")
    if not r["read_secret"] and r["secret_similarity"] >= 0.5:
        print(
            f"    最接近的读出结果 {r['near_match']!r}（相似度 {r['secret_similarity']}）"
            "—— 属于 OCR 精度问题（合成图是点阵字体），不算看不见图"
        )
    print(f"  读到单词 {SECRET_WORD} : {'✓ 是' if r['read_word'] else '✗ 否'}")
    print(f"  模型回复             : {r['reply'][:200]!r}")
    print("-" * 72)
    if r["vision_ok"]:
        print("  ✅ 结论：具备多模态（视觉）能力")
    else:
        why = []
        if not delta or delta <= 100:
            why.append("图片未被编码（token 增量过小）")
        if not (r["read_secret"] or r["read_word"]):
            why.append("图中两段已知文字一段都没读出来")
        print(f"  ❌ 结论：不具备视觉能力 —— {'；'.join(why)}")
    return bool(r["vision_ok"])


def report_dual(r: dict) -> bool:
    print("\n" + "=" * 72)
    print(f"  双图探针 · 模型: {r['model']}")
    print("=" * 72)
    if "error" in r:
        print(f"  ✗ {r['error']}")
        return False

    ratio = r["token_ratio"]
    print(f"  单图 prompt_tokens   : {r['single_tokens']}")
    print(f"  双图 prompt_tokens   : {r['dual_tokens']}  (比值 {ratio}×)")
    print(
        f"  第二张图是否被编码   : "
        f"{'是' if r['second_image_encoded'] else '否 —— token 没随图片数量增长，第二张被丢弃了'}"
    )
    print(f"  finish_reason        : {r['finish_reason']}")
    print(f"  读到 IMAGE 1 的 {SECRET_LINE} : {'✓ 是' if r['read_a'] else '✗ 否'}")
    if not r["read_a"] and r["sim_a"] >= 0.5:
        print(f"    最接近 {r['near_a']!r}（相似度 {r['sim_a']}）—— 计入「已送达」，属 OCR 精度问题")
    print(f"  读到 IMAGE 2 的 {SECRET_LINE_2} : {'✓ 是' if r['read_b'] else '✗ 否'}")
    if not r["read_b"] and r["sim_b"] >= 0.5:
        print(f"    最接近 {r['near_b']!r}（相似度 {r['sim_b']}）—— 计入「已送达」，属 OCR 精度问题")
    print(f"  模型回复             : {r['reply'][:300]!r}")
    print("-" * 72)
    if r["dual_ok"]:
        print("  ✅ 结论：网关支持多图输入，双图路径可用")
    else:
        why = []
        if not r["second_image_encoded"]:
            why.append("第二张图未被编码（token 未增长），疑似被网关静默丢弃")
        if not r["seen_a"]:
            why.append(f"未读出 IMAGE 1 中的 {SECRET_LINE}")
        if not r["seen_b"]:
            why.append(f"未读出 IMAGE 2 中的 {SECRET_LINE_2}")
        print(f"  ❌ 结论：双图不可用 —— {'；'.join(why)}")
        print("     → 请在 .env 中设置 LLM_DUAL_IMAGE=false 回退单图模式")
    return bool(r["dual_ok"])


async def main() -> int:
    ap = argparse.ArgumentParser(description="多模态能力探针")
    ap.add_argument(
        "--model",
        help="模型名，默认取 .env 中的 LLM_MODEL；可用逗号分隔一次探测多个候选",
    )
    ap.add_argument("--base-url", help="网关地址，默认取 .env")
    ap.add_argument("--api-key", help="API Key，默认取 .env")
    ap.add_argument("--all", action="store_true", help="探测该网关下所有可用模型")
    ap.add_argument(
        "--dual",
        action="store_true",
        help="验证多图输入：一次发送两张内容不同的图，断言两张都被模型读到",
    )
    args = ap.parse_args()

    api_key = args.api_key or settings.openai_api_key
    base_url = args.base_url or settings.openai_base_url
    if not api_key:
        sys.exit("未配置 API Key：请填 .env 或用 --api-key 指定")

    models = [
        m.strip() for m in (args.model or settings.llm_model).split(",") if m.strip()
    ]
    if args.all:
        try:
            r = httpx.get(
                (base_url or "https://api.openai.com/v1").rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            models = [m["id"] for m in r.json().get("data", [])]
        except Exception as exc:
            sys.exit(f"拉取模型列表失败: {exc}")

    print(f"网关: {base_url or 'https://api.openai.com/v1（默认）'}")
    print(f"待测模型: {', '.join(models)}")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=120)

    results = []
    if args.dual:
        # 两张图必须写着**不同**的随机串，否则无法区分「两张都看到」与「只看到第一张」
        dual_a = make_probe([SECRET_LINE, "English Reference", "UI Test Screenshot"])
        dual_b = make_probe([SECRET_LINE_2, "Translated Target", "UI Test Screenshot"])
        print(f"双图探针: IMAGE 1 含 {SECRET_LINE} / IMAGE 2 含 {SECRET_LINE_2}")
        for model in models:
            results.append(await probe_dual(client, model, dual_a, dual_b))
        ok = [report_dual(r) for r in results]

        print("\n" + "=" * 72)
        print("  汇总")
        print("=" * 72)
        for r, passed in zip(results, ok):
            print(f"  {'✅ 支持多图' if passed else '❌ 不支持多图（请置 LLM_DUAL_IMAGE=false）'}  {r['model']}")
        return 0 if all(ok) else 1

    probe_url = make_probe([SECRET_LINE, SECRET_WORD, "UI Test Screenshot"])
    for model in models:
        results.append(await probe(client, model, probe_url))

    ok = [report(r) for r in results]

    print("\n" + "=" * 72)
    print("  汇总")
    print("=" * 72)
    for r, passed in zip(results, ok):
        print(f"  {'✅ 支持视觉' if passed else '❌ 不支持视觉'}  {r['model']}")

    usable = [r["model"] for r, passed in zip(results, ok) if passed]
    if usable:
        print(f"\n建议在 .env 中设置： LLM_MODEL={usable[0]}")
        print("下一步可运行： python tools/vision_probe.py --dual   # 验证多图输入")
        return 0
    print("\n该网关下没有可用的多模态模型，请更换网关或模型。")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
