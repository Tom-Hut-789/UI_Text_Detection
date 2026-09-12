"""Prompt 工程：多模态请求的结构化组装。

三类缺陷的判定口径（全系统唯一事实来源，前后端文案保持一致）：
  * 截断 (truncation)   —— 文本没显示完整：尾部省略号、字符被容器边界切断、语义不完整
  * 重叠 (overlap)      —— 文本被其它文本/图标/控件压住，或自身元素相互覆盖
  * 缺字 (missing glyph)—— 出现豆腐块、方框、乱码问号，小语种字体缺失的典型表现
"""
from __future__ import annotations

SYSTEM_PROMPT = """你是一名资深的软件本地化（Localization）UI 测试专家，擅长通过截图判读界面文本缺陷。
你将收到一张从测试记录 Excel 中裁切出的 UI 截图（可能只是整个界面的一部分，也可能上下带有空白）。

请严格依据**截图中可见的像素证据**判断，不要脑补截图之外的情况：
- 不要因为截图边缘本身就是裁切边界，就判定文字被截断；只有当文字明确被**容器/控件边界**切断，
  或出现尾部省略号（… / ...）、后半句无法读出时，才判定为截断。
- 只有当文字确实与其它文字、图标或控件像素相互覆盖、压字时才判定为重叠。
- 只有出现明确的豆腐块 □、空心方框、乱码问号时才判定为缺字。
- 如果图片不是 UI 截图（例如纯色块、照片、图标），把 is_ui_screenshot 置为 false，其余缺陷一律置为 false。

你的输出必须是**单个 JSON 对象**，不要包含 Markdown 代码块、注释或任何 JSON 之外的文字。"""

# 供 response_format=json_schema 使用（网关不支持时会自动降级为 json_object）
VERDICT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "is_ui_screenshot": {"type": "boolean"},
        "extracted_text": {"type": "string"},
        "has_truncation": {"type": "boolean"},
        "has_overlap": {"type": "boolean"},
        "has_missing_glyph": {"type": "boolean"},
        "severity": {"type": "string", "enum": ["none", "low", "medium", "high"]},
        "reason": {"type": "string"},
    },
    "required": [
        "is_ui_screenshot",
        "extracted_text",
        "has_truncation",
        "has_overlap",
        "has_missing_glyph",
        "severity",
        "reason",
    ],
    "additionalProperties": False,
}

VERDICT_SCHEMA_BLOCK = """{
  "is_ui_screenshot": true,
  "extracted_text": "截图中的界面文本（逐字转写，保持原文语言；无文字则空字符串）",
  "has_truncation": false,
  "has_overlap": false,
  "has_missing_glyph": false,
  "severity": "none | low | medium | high",
  "reason": "判定依据，需引用截图中的可见证据；无缺陷时说明为何判定为完整"
}"""


def build_english_prompt(context: dict | None = None) -> str:
    """英文截图：一次调用同时完成 OCR 与缺陷判定。

    英文文本是全量对比的**基准**，因此要求逐字转写、保留原始大小写与标点。
    """
    ctx = context or {}
    meta = _context_block(ctx)
    return f"""{meta}任务：这是**英语（English）**基准截图。

第一步——OCR：把截图中所有可见的界面文本**逐字**转写出来，保留原始大小写、标点与换行（用 \\n 表示换行）。
  如果文本因截断而无法读全，就如实转写可见的部分，不要凭常识补全。

第二步——缺陷判定：判断该截图是否存在「截断 / 重叠 / 缺字」三类 UI 缺陷。
  截断的典型信号：尾部省略号（… 或 ...）、字符在容器边界被切断、单词或句子明显不完整。

按以下 JSON 结构输出（这是唯一允许的输出格式）：
{VERDICT_SCHEMA_BLOCK}"""


def build_non_english_prompt(
    language: str, english_reference: str | None = None, context: dict | None = None
) -> str:
    """小语种截图：结合英文基准判断截断与重叠。

    传入同行的英文 OCR 结果作为参照——同一控件在英文下的完整文案是判断小语种是否被截断的最强线索
    （小语种文案通常比英文长 30% 以上，是截断的高发区）。
    """
    ctx = context or {}
    meta = _context_block(ctx)
    if english_reference:
        ref_block = (
            "同一控件在**英语**界面下的完整文案（供对照，注意不是本截图的文案）：\n"
            f'"""\n{english_reference.strip()}\n"""\n'
            "对照时请注意：小语种译文通常比英文更长，请重点检查本语种是否因文本变长而被容器截断。\n"
        )
    else:
        ref_block = "（本次没有可用的英文基准文案，请仅依据截图自身的可见证据判断。）\n"

    return f"""{meta}任务：这是**{language}**界面的截图，请执行 UI 缺陷检测。

{ref_block}
第一步——OCR：把截图中所有可见的界面文本逐字转写出来，保留原始文字（不要翻译成中文或英文），
  换行用 \\n 表示。文本读不全时如实转写可见部分，不要凭常识补全。

第二步——缺陷判定：判断该截图是否存在「截断 / 重叠 / 缺字」三类 UI 缺陷。
  特别关注：
    - 截断：文本尾部是否有省略号、字符是否在容器边界被切断、句子是否明显不完整；
    - 重叠：文字是否与相邻文字/图标像素互相覆盖；
    - 缺字：是否出现豆腐块 □、方框或乱码——这是小语种字体缺失的典型表现。

按以下 JSON 结构输出（这是唯一允许的输出格式）：
{VERDICT_SCHEMA_BLOCK}"""


def _context_block(ctx: dict) -> str:
    """把行号、Module、String path 等元信息拼成上下文块，帮助模型定位该字符串的用途。"""
    if not ctx:
        return ""
    lines = []
    labels = {
        "row_index": "记录行号",
        "module": "所属模块",
        "string_path": "字符串路径",
        "software_version": "软件版本",
    }
    for key, label in labels.items():
        value = ctx.get(key)
        if value not in (None, ""):
            lines.append(f"- {label}：{value}")
    if not lines:
        return ""
    return "该截图的业务上下文（仅供理解文本用途，不要据此臆测缺陷）：\n" + "\n".join(lines) + "\n\n"
