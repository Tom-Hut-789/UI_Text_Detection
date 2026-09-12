"""核心处理引擎：编排「解析 → 并发调用 LLM → 汇总 → 生成结果 Excel」全流程。

并发模型：
  * 行**串行**处理，行内各语种用 `asyncio.gather` 并发——既保证同一行英文基准先于小语种拿到，
    也让同时在途的图片数量被 `LLMClient` 的全局信号量兜住，内存占用可控；
  * 单张图片在重试耗尽后只记录该图的 error 并继续，不拖垮整个任务。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config import settings
from ..core.events import bus
from ..database import (
    bump_task_counters,
    get_done_keys,
    get_row_results,
    get_task,
    save_row_result,
    update_task,
)
from ..models import DefectVerdict, LanguageFinding
from . import prompts
from .ai_agent import llm_client
from .excel_parser import ParsedWorkbook, load_manifest, parse_workbook
from .result_builder import build_result_workbook

logger = logging.getLogger(__name__)


class TaskRunner:
    """任务调度器：保证同一任务只有一条在跑的流水线。"""

    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task] = {}

    def is_running(self, task_id: str) -> bool:
        task = self._running.get(task_id)
        return task is not None and not task.done()

    def launch(self, task_id: str, *, resume: bool = False) -> bool:
        """异步启动任务，立即返回；真正执行在后台协程里。"""
        if self.is_running(task_id):
            logger.warning("任务 %s 已在运行，忽略重复启动", task_id)
            return False

        async def _wrapped() -> None:
            try:
                await self._run(task_id, resume=resume)
            except asyncio.CancelledError:  # pragma: no cover
                await update_task(task_id, status="INTERRUPTED", error_message="任务被取消")
                bus.publish(task_id, "error", {"message": "任务已被取消", "status": "INTERRUPTED"})
                raise
            except Exception as exc:
                logger.exception("任务 %s 执行失败", task_id)
                await update_task(task_id, status="FAILED", error_message=f"{type(exc).__name__}: {exc}")
                bus.publish(task_id, "error", {"message": str(exc), "status": "FAILED"})
            finally:
                self._running.pop(task_id, None)

        self._running[task_id] = asyncio.create_task(_wrapped(), name=f"task-{task_id}")
        return True

    async def _run(self, task_id: str, *, resume: bool) -> None:
        task = await get_task(task_id)
        if task is None:
            raise FileNotFoundError(f"任务不存在: {task_id}")

        # 快速失败：缺少凭证时不要让 115 张图逐一重试再报错，
        # 那样既浪费时间，也会让任务以「COMPLETED 但全部失败」的形式蒙混过关。
        if not settings.mock_llm and not settings.llm_configured:
            raise RuntimeError(
                "未配置 OPENAI_API_KEY。请在项目根目录的 .env 中填写密钥，"
                "或设置 MOCK_LLM=true 以模拟模式跑通链路。"
            )

        await update_task(task_id, status="PROCESSING", error_message=None)

        work_dir = settings.uploads_dir / task_id
        parsed = load_manifest(work_dir)
        if parsed is None:
            source = Path(task["source_path"] or "")
            if not source.exists():
                raise FileNotFoundError(f"源文件已丢失: {source}")
            bus.publish(task_id, "progress", {"current_step": "正在解析 Excel 并抽取内嵌图片…"})
            parsed = await asyncio.to_thread(parse_workbook, source, task_id, work_dir)

        total_images = len(parsed.images)
        await update_task(task_id, total_rows=len(parsed.rows), total_images=total_images)

        # 进度计数器：以库中已有进度为起点，断点续传时数字连续
        done_keys = await get_done_keys(task_id) if resume else set()
        counters = {
            "processed_images": task["processed_images"] if resume else 0,
            "processed_rows": task["processed_rows"] if resume else 0,
            "defective_images": task["defective_images"] if resume else 0,
        }
        if not resume:
            await update_task(task_id, processed_rows=0, processed_images=0, defective_images=0)
            done_keys = set()

        # 断点续传：把上次已完成的结论读回来，供英文基准与最终汇总复用
        prior: dict[tuple[int, int], DefectVerdict] = {}
        prior_findings: list[LanguageFinding] = []
        if resume and done_keys:
            for row in await get_row_results(task_id):
                key = (row["row_index"], row["col_index"])
                if key not in done_keys or not row["result"]:
                    continue
                verdict = DefectVerdict.model_validate(row["result"])
                prior[key] = verdict
                prior_findings.append(
                    _to_finding(parsed, row["row_index"], row["col_index"], verdict, None)
                )

        bus.publish(
            task_id,
            "started",
            {
                "task_id": task_id,
                "filename": task["filename"],
                "sheet_name": parsed.sheet_name,
                "english_col": parsed.english_col,
                "total_rows": len(parsed.rows),
                "total_images": total_images,
                "columns": [c.to_dict() for c in parsed.columns],
                "rows": parsed.rows,
                "resumed": resume,
                "skipped": len(done_keys),
                "prior_findings": [f.model_dump() for f in prior_findings],
            },
        )

        findings: list[LanguageFinding] = list(prior_findings)
        image_index = parsed.image_by_cell

        for ordinal, row_index in enumerate(parsed.rows, start=1):
            row_images = {
                col: img
                for (r, col), img in image_index.items()
                if r == row_index
            }
            if not row_images:
                continue

            english_col = parsed.english_col
            english_text: str | None = None
            pending: list[tuple[int, Any]] = []

            for col_index, img_ref in sorted(row_images.items()):
                if (row_index, col_index) in done_keys:
                    continue
                pending.append((col_index, img_ref))

            english_ref = row_images.get(english_col)
            # 英文基准优先跑完，再并发小语种——小语种 prompt 需要它做对照
            if english_ref is not None and (row_index, english_col) not in done_keys:
                finding = await self._detect_one(
                    task_id, parsed, row_index, english_col, english_ref, None, counters
                )
                findings.append(finding)
                english_text = _text_of(finding)
                pending = [(c, i) for c, i in pending if c != english_col]
            elif (row_index, english_col) in prior:
                english_text = prior[(row_index, english_col)].extracted_text

            if pending:
                results = await asyncio.gather(
                    *(
                        self._detect_one(
                            task_id, parsed, row_index, col, img_ref, english_text, counters
                        )
                        for col, img_ref in pending
                    )
                )
                findings.extend(results)

            counters["processed_rows"] += 1
            await bump_task_counters(task_id, rows=1)
            await update_task(task_id, processed_rows=counters["processed_rows"])
            bus.publish(
                task_id,
                "progress",
                {
                    "processed_rows": counters["processed_rows"],
                    "total_rows": len(parsed.rows),
                    "processed_images": counters["processed_images"],
                    "total_images": total_images,
                    "defective_images": counters["defective_images"],
                    "status": "PROCESSING",
                    "current_step": f"已完成 {ordinal}/{len(parsed.rows)} 行（Excel 第 {row_index} 行）",
                },
            )

        # 汇总 & 生成结果文件由 result_builder 负责
        findings.sort(key=lambda f: (f.row_index, f.col_index))
        out_path = settings.results_dir / f"{task_id}_result.xlsx"
        await asyncio.to_thread(build_result_workbook, parsed, findings, out_path, task["filename"])

        await update_task(
            task_id,
            status="COMPLETED",
            result_file_path=str(out_path),
            processed_rows=len(parsed.rows),
            processed_images=total_images,
        )
        bus.publish(
            task_id,
            "complete",
            {
                "task_id": task_id,
                "status": "COMPLETED",
                "total_rows": len(parsed.rows),
                "total_images": total_images,
                "processed_images": counters["processed_images"],
                "defective_images": counters["defective_images"],
                "download_url": f"/api/v1/tasks/{task_id}/download",
            },
        )
        logger.info("任务 %s 完成，结果文件 %s", task_id, out_path)

    async def _detect_one(
        self,
        task_id: str,
        parsed: ParsedWorkbook,
        row_index: int,
        col_index: int,
        img_ref,
        english_text: str | None,
        counters: dict[str, int],
    ) -> LanguageFinding:
        column = next((c for c in parsed.columns if c.col_index == col_index), None)
        language = column.language if column else f"列{col_index}"
        is_english = col_index == parsed.english_col
        ctx = parsed.meta_for(row_index)

        bus.publish(
            task_id,
            "image_started",
            {"row_index": row_index, "col_index": col_index, "language": language},
        )

        verdict: DefectVerdict | None = None
        error: str | None = None
        try:
            prompt = (
                prompts.build_english_prompt(ctx)
                if is_english
                else prompts.build_non_english_prompt(language, english_text, ctx)
            )
            verdict = await llm_client.detect(img_ref.path, prompt, is_english=is_english)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("检测失败 row=%s col=%s: %s", row_index, col_index, error)

        await save_row_result(
            task_id,
            row_index,
            col_index,
            language,
            is_english,
            "FAILED" if error else "SUCCESS",
            verdict.model_dump() if verdict else None,
            error,
        )

        counters["processed_images"] += 1
        if verdict is not None and verdict.has_defect:
            counters["defective_images"] += 1
        await bump_task_counters(
            task_id,
            images=1,
            defects=1 if (verdict is not None and verdict.has_defect) else 0,
        )

        finding = _to_finding(parsed, row_index, col_index, verdict, error)
        bus.publish(task_id, "finding", finding.model_dump())
        bus.publish(
            task_id,
            "progress",
            {
                "processed_rows": counters["processed_rows"],
                "total_rows": len(parsed.rows),
                "processed_images": counters["processed_images"],
                "total_images": len(parsed.images),
                "defective_images": counters["defective_images"],
                "status": "PROCESSING",
                "current_step": f"{language} · 第 {row_index} 行",
            },
        )
        return finding


def _to_finding(
    parsed: ParsedWorkbook,
    row_index: int,
    col_index: int,
    verdict: DefectVerdict | None,
    error: str | None,
) -> LanguageFinding:
    column = next((c for c in parsed.columns if c.col_index == col_index), None)
    return LanguageFinding(
        row_index=row_index,
        col_index=col_index,
        language=column.language if column else f"列{col_index}",
        is_english=col_index == parsed.english_col,
        header=column.header if column else "",
        verdict=verdict,
        error=error,
    )


def _text_of(finding: LanguageFinding) -> str | None:
    if finding.verdict and finding.verdict.extracted_text.strip():
        return finding.verdict.extracted_text
    return None


runner = TaskRunner()
