#!/usr/bin/env python
"""模型连通性自检：用**一张真实截图**打一次真实请求，验证 .env 配置是否可用。

它会完整走一遍生产代码路径（图片降采样 → Base64 → 结构化 Prompt → 容错 JSON 解析），
因此能一次性暴露网关地址、鉴权、模型名、多模态支持、结构化输出支持等所有问题。

用法：
    python tools/llm_check.py                 # 从 record_image.xlsx 抽第一张英语截图
    python tools/llm_check.py --image a.png   # 指定图片
    python tools/llm_check.py --row 3 --col 8 # 指定 Excel 的行/列

注意：这会产生一次真实 API 调用，消耗少量 Token。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import settings  # noqa: E402
from backend.app.services import prompts  # noqa: E402
from backend.app.services.ai_agent import llm_client  # noqa: E402
from backend.app.services.excel_parser import parse_workbook  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")


def pick_image(args) -> tuple[Path, str, dict]:
    """返回 (图片路径, 该列的语种名, 行元信息)。"""
    if args.image:
        p = Path(args.image)
        if not p.exists():
            sys.exit(f"图片不存在: {p}")
        return p, "自定义图片", {}

    source = ROOT / "record_image.xlsx"
    if not source.exists():
        sys.exit(f"找不到 {source}，请用 --image 指定一张截图")

    work = settings.data_dir / "llm_check"
    print(f"正在解析 {source.name} 并抽取截图 …")
    parsed = parse_workbook(source, "llm_check", work)

    row = args.row or (parsed.rows[0] if parsed.rows else None)
    col = args.col or parsed.english_col
    ref = parsed.image_by_cell.get((row, col))
    if ref is None:
        available = sorted(parsed.image_by_cell)
        sys.exit(f"第 {row} 行第 {col} 列没有截图。可用单元格（行,列）: {available[:12]} …")

    column = next((c for c in parsed.columns if c.col_index == col), None)
    language = column.language if column else f"列{col}"
    return Path(ref.path), language, parsed.meta_for(row)


async def main() -> int:
    parser = argparse.ArgumentParser(description="模型连通性自检")
    parser.add_argument("--image", help="直接指定一张截图路径")
    parser.add_argument("--row", type=int, help="Excel 行号（1-based）")
    parser.add_argument("--col", type=int, help="Excel 列号（1-based，E=5）")
    args = parser.parse_args()

    print("=" * 68)
    print(f"  网关地址 : {settings.openai_base_url or 'https://api.openai.com/v1（默认）'}")
    print(f"  模型     : {settings.llm_model}")
    print(f"  密钥     : {'已配置' if settings.llm_configured else '未配置'}")
    print(f"  MOCK_LLM : {settings.mock_llm}")
    print(f"  并发/超时: {settings.llm_concurrency} / {settings.llm_timeout}s")
    print("=" * 68)

    if settings.mock_llm:
        print("\n[!] 当前 MOCK_LLM=true，本次不会发起真实请求。")
        print("    若要验证真实模型，请把 .env 里的 MOCK_LLM 改为 false。\n")
        return 2

    image_path, language, ctx = pick_image(args)
    print(f"\n使用截图: {image_path}  （语种: {language}）\n")

    prompt = prompts.build_english_prompt(ctx) if language == "英语" else prompts.build_non_english_prompt(language, None, ctx)
    print("正在调用模型 …\n")
    try:
        verdict = await llm_client.detect(image_path, prompt, is_english=(language == "英语"))
    except Exception as exc:
        print("\n" + "!" * 68)
        print(f"调用失败: {type(exc).__name__}: {exc}")
        print("!" * 68)
        print("\n排查建议：")
        print("  · 「未返回可解析的 JSON 结论」→ 模型看不见图，只能用语言猜测并作答；")
        print("    请确认 LLM_MODEL 是真正的多模态模型（可用下面的视觉探针单独验证）")
        print("  · 「回复被 max_tokens 截断」  → 调大 .env 中的 LLM_MAX_TOKENS")
        print("  · 400 / invalid content type  → 该模型或网关不支持 image_url 多模态输入")
        print("  · 401 / invalid api key       → 密钥有误或与网关不匹配")
        print("  · 404 / model not found       → LLM_MODEL 名称在该网关不存在")
        print("  · 连接超时                    → 检查 OPENAI_BASE_URL 与网络/代理")
        return 1

    print("=" * 68)
    print("  调用成功，模型返回解析结果：")
    print("=" * 68)
    print(f"  is_ui_screenshot : {verdict.is_ui_screenshot}")
    print(f"  has_truncation   : {verdict.has_truncation}")
    print(f"  has_overlap      : {verdict.has_overlap}")
    print(f"  has_missing_glyph: {verdict.has_missing_glyph}")
    print(f"  severity         : {verdict.severity}")
    print(f"  reason           : {verdict.reason}")
    print(f"  extracted_text   : {verdict.extracted_text[:200]!r}")
    print("=" * 68)

    # 只有真的读到文字，才能证明模型「看得见」——否则它可能只是按语言猜测作答。
    if not verdict.extracted_text.strip():
        print("\n⚠️  结构化输出解析正常，但 OCR 结果为空。")
        print("    这通常意味着模型根本没看到图（非多模态模型只会输出空字段或套话）。")
        print("    请运行 tools/vision_probe.py 做一次确定性的可见性验证。")
        return 3

    print("\n✅ 配置可用：模型成功识别出截图文字，可以正式跑任务了。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
