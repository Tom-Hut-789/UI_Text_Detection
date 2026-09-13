#!/usr/bin/env python
"""模型连通性自检：用**真实截图**打真实请求，验证 .env 配置是否可用。

它会完整走一遍生产代码路径（图片降采样 → Base64 → 结构化 Prompt → 容错 JSON 解析），
因此能一次性暴露网关地址、鉴权、模型名、多模态支持、结构化输出支持等所有问题。

默认复刻 pipeline 的双图对比流程：先对同行英文截图跑一次 OCR 取得语义锚点，
再把英文基准图 + 目标语种图一起发给模型（与 `pipeline._detect_one` 完全一致）。
用 `--single` 可强制走单图路径做对照。

用法：
    python tools/llm_check.py                 # 从 record_image.xlsx 抽第一张小语种截图
    python tools/llm_check.py --image a.png   # 指定图片
    python tools/llm_check.py --row 3 --col 8 # 指定 Excel 的行/列
    python tools/llm_check.py --single        # 强制单图模式（对照用）

注意：这会产生真实 API 调用——双图模式下是 2 次（英文基准 + 目标语种），
      `--single` 是 1 次。消耗 Token 少量。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，编不出下文要打印的 ⚠ ✅ 等符号；一旦输出被重定向到管道或文件，
# Python 会按系统编码构造 stdout，print 直接抛 UnicodeEncodeError，把一次正常自检变成 traceback。
# 只放宽错误处理、不改编码，这样 GBK 控制台不会出现乱码。
for _s in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_s, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(errors="replace")

from backend.app.config import settings  # noqa: E402
from backend.app.services import prompts  # noqa: E402
from backend.app.services.ai_agent import llm_client  # noqa: E402
from backend.app.services.excel_parser import parse_workbook  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")


def _first_dual_cell(parsed) -> tuple[int, int]:
    """挑第一个「自身有截图、且同行英文列也有截图」的小语种单元格。

    默认选它而不是英文列，是为了让不带参数的自检**天然就覆盖双图路径**；
    找不到这样的单元格时退回英文列（只能走单图）。
    """
    for row, col in sorted(parsed.image_by_cell):
        if col != parsed.english_col and (row, parsed.english_col) in parsed.image_by_cell:
            return row, col
    return (parsed.rows[0] if parsed.rows else 1), parsed.english_col


def pick_image(args) -> tuple[Path, str, dict, Path | None]:
    """返回 (待检图路径, 该列的语种名, 行元信息, 同行英文基准图路径或 None)。"""
    if args.image:
        p = Path(args.image)
        if not p.exists():
            sys.exit(f"图片不存在: {p}")
        return p, "自定义图片", {}, None

    source = ROOT / "record_image.xlsx"
    if not source.exists():
        sys.exit(f"找不到 {source}，请用 --image 指定一张截图")

    work = settings.data_dir / "llm_check"
    print(f"正在解析 {source.name} 并抽取截图 …")
    parsed = parse_workbook(source, "llm_check", work)

    if args.row is None and args.col is None:
        row, col = _first_dual_cell(parsed)
    else:
        row = args.row or (parsed.rows[0] if parsed.rows else None)
        col = args.col or parsed.english_col
    ref = parsed.image_by_cell.get((row, col))
    if ref is None:
        available = sorted(parsed.image_by_cell)
        sys.exit(f"第 {row} 行第 {col} 列没有截图。可用单元格（行,列）: {available[:12]} …")

    column = next((c for c in parsed.columns if c.col_index == col), None)
    language = column.language if column else f"列{col}"
    # 双图对比要用的基准图，与 pipeline 一样从同行英文列的单元格取
    english = parsed.image_by_cell.get((row, parsed.english_col))
    english_path = Path(english.path) if english is not None else None
    return Path(ref.path), language, parsed.meta_for(row), english_path


async def main() -> int:
    parser = argparse.ArgumentParser(description="模型连通性自检")
    parser.add_argument("--image", help="直接指定一张截图路径")
    parser.add_argument("--row", type=int, help="Excel 行号（1-based）")
    parser.add_argument("--col", type=int, help="Excel 列号（1-based，E=5）")
    parser.add_argument(
        "--single",
        action="store_true",
        help="强制单图模式（即使 LLM_DUAL_IMAGE=true），用于对照双图与单图的结果差异",
    )
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

    image_path, language, ctx, english_image = pick_image(args)
    is_english = language == "英语"

    # 与 pipeline._detect_one 判定双图的三个前提完全一致：开关开、不是英文列、拿到了基准图
    use_dual = bool(
        settings.llm_dual_image and not args.single and not is_english and english_image is not None
    )

    print(f"\n  待检截图 : {image_path}  （语种: {language}）")
    print(f"  基准截图 : {english_image if use_dual else '——（走单图模式）'}")
    why_single = ""
    if not use_dual:
        if args.single:
            why_single = "（--single 强制）"
        elif not settings.llm_dual_image:
            why_single = "（.env 中 LLM_DUAL_IMAGE=false）"
        elif is_english:
            why_single = "（英文列自身没有基准）"
        elif english_image is None:
            why_single = "（同行英文列没有截图）"
    print(f"  双图对比 : {'开' if use_dual else '关'}{why_single}\n")

    english_text: str | None = None
    if use_dual:
        # 复刻 pipeline：英文基准先跑一次 OCR，其文本作为小语种调用的语义锚点。
        # 这一步失败不致命——退化成「只有图、没有文本锚点」，也正是生产环境的一种合法情形。
        print("[1/2] 先检测英文基准截图，取得语义锚点 …")
        try:
            ref_verdict = await llm_client.detect(
                english_image, prompts.build_english_prompt(ctx), is_english=True
            )
            english_text = ref_verdict.extracted_text.strip() or None
            print(f"      英文 OCR: {english_text[:120]!r}" if english_text else "      英文 OCR 为空，本次仅用图片作几何对照")
        except Exception as exc:
            print(f"      ⚠ 英文基准检测失败（{type(exc).__name__}: {exc}），继续用纯图片对照")

    prompt = (
        prompts.build_english_prompt(ctx)
        if is_english
        else prompts.build_non_english_prompt(
            language, english_text, ctx, has_reference_image=use_dual
        )
    )
    print(f"\n[{'2/2' if use_dual else '1/1'}] 正在调用模型检测目标截图 …\n")
    try:
        verdict = await llm_client.detect(
            image_path,
            prompt,
            is_english=is_english,
            reference_image_path=english_image if use_dual else None,
        )
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

    if use_dual:
        print(f"\n✅ 配置可用（双图模式）：英文基准图 + {language} 目标图同时送达模型并正常作答。")
        print("   如需确认网关没有静默丢弃第二张图，请运行： python tools/vision_probe.py --dual")
        print("   如需对照单图结果： python tools/llm_check.py --single --row <行> --col <列>")
    else:
        print("\n✅ 配置可用（单图模式）：模型成功识别出截图文字，可以正式跑任务了。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
