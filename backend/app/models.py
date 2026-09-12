"""Pydantic V2 数据模型：对外 API 契约 + LLM 结构化输出的强校验。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

TaskStatus = Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED", "INTERRUPTED"]


class ApiResponse(BaseModel):
    code: int = 200
    message: str = "success"
    data: Any = None


class TaskInfo(BaseModel):
    id: str
    filename: str
    status: TaskStatus
    total_rows: int = 0
    processed_rows: int = 0
    total_images: int = 0
    processed_images: int = 0
    defective_images: int = 0
    error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    has_result: bool = False


# --------------------------------------------------------------------------
# LLM 结构化输出
# --------------------------------------------------------------------------
class DefectVerdict(BaseModel):
    """单张 UI 截图的检测结论。字段与 prompts.py 中的 JSON Schema 严格对齐。"""

    is_ui_screenshot: bool = Field(
        default=True, description="图片是否为可读的 UI 截图；False 表示非截图或无法判读"
    )
    extracted_text: str = Field(default="", description="OCR 提取的界面文本")
    has_truncation: bool = Field(default=False, description="文本是否被截断")
    has_overlap: bool = Field(default=False, description="文本是否发生重叠/遮挡")
    has_missing_glyph: bool = Field(
        default=False, description="是否出现豆腐块/缺字（小语种字体缺失的典型表现）"
    )
    severity: Literal["none", "low", "medium", "high"] = "none"
    reason: str = Field(default="", description="判定依据，需引用截图中的可见证据")

    @field_validator("severity", mode="before")
    @classmethod
    def _norm_severity(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().lower()
            if v not in {"none", "low", "medium", "high"}:
                return "low"
        return v

    @property
    def has_defect(self) -> bool:
        return self.has_truncation or self.has_overlap or self.has_missing_glyph

    def defect_labels(self) -> list[str]:
        labels = []
        if self.has_truncation:
            labels.append("截断")
        if self.has_overlap:
            labels.append("重叠")
        if self.has_missing_glyph:
            labels.append("缺字")
        return labels


class LanguageFinding(BaseModel):
    """某一行、某一语种的最终结论（供前端表格与结果 Excel 复用）。"""

    row_index: int
    col_index: int
    language: str
    is_english: bool
    header: str = ""
    verdict: DefectVerdict | None = None
    error: str | None = None
