"""Excel 解析：提取表头语种列 + 逐行逐列抽出入图片，落盘为独立文件。

设计要点（对应 SDD 7.2 §1 内存溢出控制）：
  * openpyxl 必须非 read_only 模式加载，否则拿不到 `ws._images`；
  * 图片一旦写盘立即 `del` 引用，进程内不长期持有全部图像字节；
  * 解析产物是一份 manifest（JSON），后续流水线按需从磁盘读图。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

ENGLISH_HEADER_TOKENS = ("英语", "english")

MANIFEST_NAME = "manifest.json"


@dataclass
class ColumnSpec:
    """一个语种列。"""

    col_index: int  # 1-based，与 Excel 一致
    letter: str
    header: str
    language: str  # 从表头解析出的语种名
    is_english: bool

    def to_dict(self) -> dict:
        return {
            "col_index": self.col_index,
            "letter": self.letter,
            "header": self.header,
            "language": self.language,
            "is_english": self.is_english,
        }


@dataclass
class ImageRef:
    """一张已落盘的截图。"""

    row_index: int  # 1-based Excel 行号
    col_index: int
    path: str
    width: int
    height: int
    mime: str = "image/png"

    def to_dict(self) -> dict:
        return {
            "row_index": self.row_index,
            "col_index": self.col_index,
            "path": self.path,
            "width": self.width,
            "height": self.height,
            "mime": self.mime,
        }


@dataclass
class ParsedWorkbook:
    sheet_name: str
    english_col: int
    columns: list[ColumnSpec] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    # 行号 -> {表头: 单元格文本}，仅含英语列之前的元信息列（Module / String path / 版本 等）
    row_meta: dict[int, dict[str, str]] = field(default_factory=dict)

    @property
    def image_by_cell(self) -> dict[tuple[int, int], ImageRef]:
        return {(i.row_index, i.col_index): i for i in self.images}

    def to_dict(self) -> dict:
        return {
            "sheet_name": self.sheet_name,
            "english_col": self.english_col,
            "columns": [c.to_dict() for c in self.columns],
            "rows": self.rows,
            "images": [i.to_dict() for i in self.images],
            "row_meta": {str(k): v for k, v in self.row_meta.items()},
        }

    def meta_for(self, row_index: int) -> dict[str, str]:
        """取该行的业务上下文，并把表头键名归一化成 prompt 认识的字段。"""
        raw = self.row_meta.get(row_index, {})
        norm: dict[str, str] = {"row_index": str(row_index)}
        for key, value in raw.items():
            low = key.lower()
            if "module" in low:
                norm["module"] = value
            elif "path" in low:
                norm["string_path"] = value
            elif "version" in low:
                norm["software_version"] = value
        return norm


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _split_language(header: str) -> str:
    """`KH2语官方(မြန်မာ)` -> `KH2语官方`；`英语(English)` -> `英语`。

    表头形如「中文名(本地语言名)」，取括号前的部分作为列名，取不到则用原文。
    """
    if not header:
        return header
    for left in ("（", "("):
        if left in header:
            return header.split(left, 1)[0].strip() or header
    return header


def _english_header_score(header: str) -> int:
    """给表头与「英语列」的匹配度打分，0 表示不是英语列。

    不能简单用 `"english" in header`——模板里 B 列叫 `String path in English`，
    那样会误命中。因此优先看括号前的语种名，再看括号内的本地语言名。
    """
    if not header:
        return 0
    low = header.strip().lower()
    name, _, local = low.partition("(")
    if not local:
        name, _, local = low.partition("（")
    name = name.strip()
    local = local.rstrip(")）").strip()

    if name in ("英语", "english"):
        return 100
    if any(tok in name for tok in ENGLISH_HEADER_TOKENS):
        return 60
    if local == "english":
        return 80
    if low in ("英语", "english"):
        return 100
    return 0


def find_english_column(headers: dict[int, str], fallback: int = 5) -> int:
    """定位英语列：取匹配度最高的表头，平分时取最左列；全都匹配不上则回退 E 列（SDD 约定）。"""
    best_idx, best_score = None, 0
    for idx in sorted(headers):
        score = _english_header_score(headers[idx])
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx if best_idx is not None else fallback


def _anchor_cell(anchor) -> tuple[int, int] | None:
    """把 openpyxl 的锚点换算成 1-based 的 (row, col)。"""
    frm = getattr(anchor, "_from", None)
    if frm is None:  # AbsoluteAnchor 没有 _from，无法定位单元格
        return None
    return frm.row + 1, frm.col + 1


def _image_bytes(img) -> bytes:
    """openpyxl 的 Image 有两种数据形态：bytes 或返回 bytes 的 callable。"""
    data = getattr(img, "_data", None)
    if data is None:
        raise ValueError("图片对象缺少 _data")
    return data() if callable(data) else data


def parse_workbook(
    xlsx_path: Path,
    task_id: str,
    work_dir: Path,
    header_row: int = 1,
    sheet_name: str | None = None,
) -> ParsedWorkbook:
    """解析 Excel，把每张图片写到 `work_dir` 下，返回 manifest 结构。"""
    work_dir.mkdir(parents=True, exist_ok=True)

    wb = load_workbook(xlsx_path, data_only=True)
    try:
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb[wb.sheetnames[0]]

        headers: dict[int, str] = {}
        for col in range(1, ws.max_column + 1):
            text = _cell_text(ws.cell(header_row, col).value)
            if text:
                headers[col] = text

        english_col = find_english_column(headers)

        columns: list[ColumnSpec] = []
        for col in sorted(headers):
            if col < english_col:
                continue  # A~D 是 Module / String path 等元信息列，不参与检测
            header = headers[col]
            columns.append(
                ColumnSpec(
                    col_index=col,
                    letter=get_column_letter(col),
                    header=header,
                    language=_split_language(header),
                    is_english=col == english_col,
                )
            )

        # 收集图片锚点：以锚点所在单元格为准
        anchors: list[tuple[int, int, object]] = []
        for img in ws._images:
            cell = _anchor_cell(img.anchor)
            if cell is None:
                logger.warning("跳过无法定位单元格的图片（AbsoluteAnchor）")
                continue
            anchors.append((cell[0], cell[1], img))
        anchors.sort(key=lambda t: (t[0], t[1]))

        images: list[ImageRef] = []
        for row_idx, col_idx, img in anchors:
            try:
                blob = _image_bytes(img)
            except Exception as exc:  # pragma: no cover - 依赖 openpyxl 内部结构
                logger.warning("读取图片数据失败 row=%s col=%s: %s", row_idx, col_idx, exc)
                continue
            if not blob:
                continue
            suffix = Path(getattr(img, "path", "") or "").suffix or ".png"
            out = work_dir / f"r{row_idx}_c{col_idx}{suffix}"
            out.write_bytes(blob)
            images.append(
                ImageRef(
                    row_index=row_idx,
                    col_index=col_idx,
                    path=str(out),
                    width=int(getattr(img, "width", 0) or 0),
                    height=int(getattr(img, "height", 0) or 0),
                )
            )
            del img, blob  # 及时释放，避免大图堆积

        rows = sorted({r for r, _, _ in anchors})

        # 英语列之前的元信息列（Module / String path in English / Software Version …）
        meta_cols = [c for c in sorted(headers) if c < english_col]
        row_meta: dict[int, dict[str, str]] = {}
        for row_idx in rows:
            meta = {
                headers[c]: text
                for c in meta_cols
                if (text := _cell_text(ws.cell(row_idx, c).value))
            }
            if meta:
                row_meta[row_idx] = meta

        parsed = ParsedWorkbook(
            sheet_name=ws.title,
            english_col=english_col,
            columns=columns,
            rows=rows,
            images=images,
            row_meta=row_meta,
        )
    finally:
        wb.close()

    (work_dir / MANIFEST_NAME).write_text(
        json.dumps(parsed.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "解析完成 sheet=%s 行数=%d 图片数=%d 英语列=%s",
        parsed.sheet_name,
        len(parsed.rows),
        len(parsed.images),
        get_column_letter(parsed.english_col),
    )
    return parsed


def load_manifest(work_dir: Path) -> ParsedWorkbook | None:
    """从磁盘恢复解析产物，供断点续传复用（避免重新解析 25MB 级 xlsx）。"""
    path = work_dir / MANIFEST_NAME
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ParsedWorkbook(
        sheet_name=raw["sheet_name"],
        english_col=raw["english_col"],
        columns=[ColumnSpec(**c) for c in raw["columns"]],
        rows=list(raw["rows"]),
        images=[ImageRef(**i) for i in raw["images"]],
        row_meta={int(k): v for k, v in raw.get("row_meta", {}).items()},
    )
