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
    python tools/vision_probe.py --model x --base-url y --api-key z
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from backend.app.config import settings  # noqa: E402

# 故意用无意义的随机串：模型无法靠语言先验猜出来，只能靠"看"
SECRET_LINE = "ZQ7K-4419-XM"
SECRET_WORD = "Kaleidoscope"


def make_probe() -> str:
    """画一张写着已知文字的图，返回 data URL。"""
    img = Image.new("RGB", (640, 220), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 629, 209], outline="black", width=3)
    draw.text((32, 50), SECRET_LINE, fill="black")
    draw.text((32, 100), SECRET_WORD, fill="black")
    draw.text((32, 150), "UI Test Screenshot", fill="gray")
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

    delta = None
    if result["text_tokens"] is not None and result["vision_tokens"] is not None:
        delta = result["vision_tokens"] - result["text_tokens"]
    result["token_delta"] = delta
    result["vision_ok"] = bool(result["read_secret"] and delta is not None and delta > 100)
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
    print(f"  模型回复             : {r['reply'][:200]!r}")
    print("-" * 72)
    if r["vision_ok"]:
        print("  ✅ 结论：具备多模态（视觉）能力")
    else:
        why = []
        if not delta or delta <= 100:
            why.append("图片未被编码（token 增量过小）")
        if not r["read_secret"]:
            why.append("未能读出图中的已知文字")
        print(f"  ❌ 结论：不具备视觉能力 —— {'；'.join(why)}")
    return bool(r["vision_ok"])


async def main() -> int:
    ap = argparse.ArgumentParser(description="多模态能力探针")
    ap.add_argument("--model", help="模型名，默认取 .env 中的 LLM_MODEL")
    ap.add_argument("--base-url", help="网关地址，默认取 .env")
    ap.add_argument("--api-key", help="API Key，默认取 .env")
    ap.add_argument("--all", action="store_true", help="探测该网关下所有可用模型")
    args = ap.parse_args()

    api_key = args.api_key or settings.openai_api_key
    base_url = args.base_url or settings.openai_base_url
    if not api_key:
        sys.exit("未配置 API Key：请填 .env 或用 --api-key 指定")

    models = [args.model or settings.llm_model]
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
    probe_url = make_probe()

    results = []
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
        return 0
    print("\n该网关下没有可用的多模态模型，请更换网关或模型。")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
