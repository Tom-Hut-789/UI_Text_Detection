"""结果文档生成（对应 SDD 4.3）。

输出布局：
    A  英文UI缺陷      —— 英语基准截图的缺陷描述
    B  英文UI截图      —— 英语基准截图原图
    C  非英文UI缺陷汇总 —— 全部小语种缺陷的合并说明（语种：缺陷类型；…）
    D… 动态语种列       —— 仅针对**检出缺陷的语种**创建一列，列头为语种名，单元格内嵌该语种截图

设计取舍：截图原图高达 1200×2600+，若按原始尺寸嵌入，结果文件会膨胀到数百 MB 且无法在
Excel 中浏览。因此统一按「显示高度」等比缩放后再嵌入，并同步调整行高与列宽。
"""
from __future__ import annotations

import logging
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..models import LanguageFinding
from .excel_parser import ParsedWorkbook

logger = logging.getLogger(__name__)

# 截图的显示高度（像素）。保留足够可读性，同时控制文件体积。
DISPLAY_HEIGHT_PX = 300
# Excel 行高单位是磅（pt），1px ≈ 0.75pt
PX_TO_PT = 0.75
MIN_ROW_HEIGHT_PT = 20
MAX_ROW_HEIGHT_PT = 409  # Excel 单行行高上限

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP_TOP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _english_defect_text(finding: LanguageFinding | None) -> str:
    if finding is None:
        return "未提供英语截图"
    if finding.error:
        return f"检测失败：{finding.error}"
    if finding.verdict is None:
        return ""
    labels = finding.verdict.defect_labels()
    if not labels:
        return "无缺陷"
    return f"{'、'.join(labels)}：{finding.verdict.reason}".strip("：")


def _language_defect_text(finding: LanguageFinding) -> str:
    if finding.error:
        return f"检测失败（{finding.error}）"
    if finding.verdict is None:
        return "已跳过"
    labels = finding.verdict.defect_labels()
    return "、".join(labels) if labels else "无缺陷"


def _summary_text(findings: list[LanguageFinding], english: LanguageFinding | None) -> str:
    """非英文UI缺陷汇总：沿用 SDD 的「语种：缺陷类型」逐条罗列。"""
    defective = [
        f
        for f in findings
        if not f.is_english and f.verdict is not None and f.verdict.has_defect
    ]
    failed = [f for f in findings if not f.is_english and f.error]

    parts: list[str] = []
    for f in defective:
        labels = "、".join(f.verdict.defect_labels())
        reason = f.verdict.reason.strip()
        parts.append(f"{f.language}：{labels}" + (f"（{reason}）" if reason else ""))
    if failed:
        parts.append(f"检测失败语种：{'、'.join(f.language for f in failed)}")

    if not parts:
        # 全部小语种均无缺陷时，明确写出结论而不是留空
        checked = [f for f in findings if not f.is_english and f.verdict is not None]
        if not checked:
            return "无小语种截图"
        eng_note = ""
        if english is not None and english.verdict is not None and english.verdict.has_defect:
            eng_note = "（注意：英语基准截图本身存在缺陷）"
        return f"共检测 {len(checked)} 个语种，均未发现截断/重叠/缺字缺陷{eng_note}"
    return "\n".join(parts)


def _scaled_size(path: str, target_height: int) -> tuple[int, int]:
    """按目标显示高度等比换算宽高；读取失败时退回一个保守的默认尺寸。"""
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as im:
            w, h = im.size
        if h <= 0:
            raise ValueError("图片高度为 0")
        ratio = target_height / h
        return max(1, int(w * ratio)), max(1, int(h * ratio))
    except Exception as exc:
        logger.warning("读取截图尺寸失败，使用默认尺寸 %s: %s", path, exc)
        return 150, target_height


def build_result_workbook(
    parsed: ParsedWorkbook,
    findings: list[LanguageFinding],
    out_path: Path,
    source_filename: str = "",
) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "UI缺陷检测结果"

    by_row: dict[int, dict[int, LanguageFinding]] = {}
    for f in findings:
        by_row.setdefault(f.row_index, {})[f.col_index] = f

    # 仅展示「确实检出缺陷」的小语种列，且按 Excel 原始列序稳定排序
    defective_cols: list[tuple[int, str]] = []
    seen: set[int] = set()
    for row_map in by_row.values():
        for col_index, f in row_map.items():
            if f.is_english or col_index in seen:
                continue
            if f.verdict is not None and f.verdict.has_defect:
                seen.add(col_index)
                defective_cols.append((col_index, f.language))
    defective_cols.sort()
    if not defective_cols:
        logger.info("本次未检出任何小语种缺陷，结果文件只保留英文与汇总列")

    headers = ["英文UI缺陷", "英文UI截图", "非英文UI缺陷汇总"]
    headers += [lang for _, lang in defective_cols]

    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(1, col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = BORDER

    widths = [42, 24, 60] + [max(18, 22) for _ in defective_cols]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.row_dimensions[1].height = 26

    image_path_by_cell = parsed.image_by_cell

    for offset, row_index in enumerate(parsed.rows):
        excel_row = offset + 2
        row_map = by_row.get(row_index, {})
        english = row_map.get(parsed.english_col)

        # ---- A 列：英文缺陷描述 ----
        ws.cell(excel_row, 1, _english_defect_text(english)).alignment = WRAP_TOP
        ws.cell(excel_row, 2, "").alignment = CENTER

        # ---- C 列：非英文缺陷汇总 ----
        non_english = [row_map[c] for c in sorted(row_map) if c != parsed.english_col]
        ws.cell(excel_row, 3, _summary_text(non_english, english)).alignment = WRAP_TOP

        max_height_px = 0

        # ---- B 列：英文截图 ----
        if english is not None:
            ref = image_path_by_cell.get((row_index, parsed.english_col))
            if ref is not None:
                size = _insert_image(ws, ref.path, excel_row, 2)
                if size:
                    max_height_px = max(max_height_px, size)

        # ---- D 列起：各缺陷语种截图 ----
        for col_offset, (col_index, _lang) in enumerate(defective_cols):
            finding = row_map.get(col_index)
            if finding is None or finding.verdict is None or not finding.verdict.has_defect:
                continue
            ref = image_path_by_cell.get((row_index, col_index))
            if ref is None:
                continue
            excel_col = 4 + col_offset
            size = _insert_image(ws, ref.path, excel_row, excel_col)
            if size:
                max_height_px = max(max_height_px, size)

        # 行高按本行最高的截图撑开，图片才能完整可见
        needed_pt = max_height_px * PX_TO_PT + 6
        ws.row_dimensions[excel_row].height = min(
            MAX_ROW_HEIGHT_PT, max(MIN_ROW_HEIGHT_PT, needed_pt)
        )

        for col in range(1, len(headers) + 1):
            ws.cell(excel_row, col).border = BORDER

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    wb.close()
    logger.info("结果文件已生成: %s（%d 行，%d 个缺陷语种列）", out_path, len(parsed.rows), len(defective_cols))
    return out_path


def _insert_image(ws, image_path: str, row: int, col: int) -> int:
    """把截图缩放到显示高度后锚定到指定单元格，返回实际显示高度（px）。"""
    try:
        width, height = _scaled_size(image_path, DISPLAY_HEIGHT_PX)
        img = XLImage(image_path)
        img.width = width
        img.height = height
        ws.add_image(img, f"{get_column_letter(col)}{row}")
        return height
    except Exception as exc:
        logger.warning("插入截图失败 %s: %s", image_path, exc)
        ws.cell(row, col, f"[截图插入失败: {Path(image_path).name}]")
        return 0
