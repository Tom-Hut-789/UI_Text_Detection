"""任务相关 RESTful + SSE 接口（对应 SDD 第 6 章）。"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from ..config import settings
from ..core.events import bus
from ..database import get_row_results, get_task, list_tasks, update_task
from ..models import ApiResponse
from ..services.pipeline import runner

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ALLOWED_SUFFIXES = {".xlsx", ".xlsm"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200MB，含内嵌高清图的 Excel 可能较大
HEARTBEAT_SECONDS = 15


@router.post("/upload", response_model=ApiResponse)
async def upload_task(file: UploadFile = File(...)) -> ApiResponse:
    """接收 Excel，落盘并立即返回 task_id；后台流水线随后启动（SDD 4.1）。"""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"仅支持 {', '.join(sorted(ALLOWED_SUFFIXES))} 文件")

    task_id = str(uuid.uuid4())
    work_dir = settings.uploads_dir / task_id
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / f"source{suffix}"

    written = 0
    try:
        with dest.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="文件超过 200MB 上限")
                fh.write(chunk)
    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.exception("保存上传文件失败")
        raise HTTPException(status_code=500, detail=f"保存上传文件失败: {exc}") from exc
    finally:
        await file.close()

    if written == 0:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="上传文件为空")

    # 延迟导入避免循环依赖
    from ..database import create_task

    await create_task(task_id, file.filename or dest.name, str(dest))
    runner.launch(task_id, resume=False)

    return ApiResponse(
        data={"task_id": task_id, "filename": file.filename, "status": "PENDING"}
    )


@router.get("", response_model=ApiResponse)
async def list_all(limit: int = 50) -> ApiResponse:
    tasks = await list_tasks(limit=limit)
    for t in tasks:
        t["has_result"] = bool(t.get("result_file_path"))
        t.pop("source_path", None)
    return ApiResponse(data=tasks)


@router.get("/{task_id}", response_model=ApiResponse)
async def get_task_info(task_id: str) -> ApiResponse:
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    task["has_result"] = bool(task.get("result_file_path"))
    task.pop("source_path", None)
    return ApiResponse(data=task)


@router.post("/{task_id}/resume", response_model=ApiResponse)
async def resume_task(task_id: str) -> ApiResponse:
    """断点续传：跳过已成功的图片，只补跑未完成部分（SDD 7.2 §3）。"""
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if runner.is_running(task_id):
        raise HTTPException(status_code=409, detail="任务正在运行中")
    if task["status"] == "COMPLETED":
        raise HTTPException(status_code=409, detail="任务已完成，无需续跑")

    launched = runner.launch(task_id, resume=True)
    if not launched:
        raise HTTPException(status_code=409, detail="任务启动失败")
    return ApiResponse(data={"task_id": task_id, "status": "PROCESSING", "resumed": True})


@router.get("/{task_id}/findings", response_model=ApiResponse)
async def get_findings(task_id: str) -> ApiResponse:
    """逐图检测结论，供前端结果表格使用。"""
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    rows = await get_row_results(task_id)
    return ApiResponse(data={"task": _public_task(task), "findings": rows})


@router.get("/{task_id}/result", response_model=ApiResponse)
async def get_result_summary(task_id: str) -> ApiResponse:
    """按语种聚合的缺陷统计。"""
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    rows = await get_row_results(task_id)
    stats: dict[str, dict] = {}
    for row in rows:
        result = row["result"] or {}
        key = row["language"]
        entry = stats.setdefault(
            key,
            {
                "language": key,
                "col_index": row["col_index"],
                "is_english": bool(row["is_english"]),
                "checked": 0,
                "truncation": 0,
                "overlap": 0,
                "missing_glyph": 0,
                "defective": 0,
                "failed": 0,
            },
        )
        if row["status"] != "SUCCESS":
            entry["failed"] += 1
            continue
        entry["checked"] += 1
        if result.get("has_truncation"):
            entry["truncation"] += 1
        if result.get("has_overlap"):
            entry["overlap"] += 1
        if result.get("has_missing_glyph"):
            entry["missing_glyph"] += 1
        if result.get("has_truncation") or result.get("has_overlap") or result.get("has_missing_glyph"):
            entry["defective"] += 1

    languages = sorted(stats.values(), key=lambda s: (not s["is_english"], s["col_index"]))
    total_defective = sum(s["defective"] for s in languages)
    return ApiResponse(
        data={
            "task": _public_task(task),
            "languages": languages,
            "summary": {
                "languages_checked": sum(1 for s in languages if s["checked"]),
                "images_checked": sum(s["checked"] for s in languages),
                "defective_images": total_defective,
                "failed_images": sum(s["failed"] for s in languages),
            },
        }
    )


@router.get("/{task_id}/download")
async def download_result(task_id: str) -> FileResponse:
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] != "COMPLETED" or not task.get("result_file_path"):
        raise HTTPException(status_code=409, detail=f"结果文件尚未生成（当前状态 {task['status']}）")

    path = Path(task["result_file_path"])
    if not path.exists():
        await update_task(task_id, status="FAILED", error_message="结果文件已被删除")
        raise HTTPException(status_code=410, detail="结果文件已被删除，请重新跑一次任务")

    stem = Path(task["filename"] or "result").stem
    return FileResponse(path, media_type=XLSX_MIME, filename=f"{stem}_UI缺陷检测结果.xlsx")


@router.get("/{task_id}/stream")
async def stream_progress(task_id: str, request: Request) -> StreamingResponse:
    """SSE 进度流（SDD 6.2）。

    支持 `Last-Event-ID` 断线重连：重新连上后会先补齐漏掉的事件，再进入实时推送。
    """
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    try:
        last_event_id = int(request.headers.get("last-event-id") or 0)
    except ValueError:
        last_event_id = 0

    async def event_source():
        queue = bus.subscribe(task_id, last_event_id)
        try:
            # 订阅后立刻补发一次当前状态，前端不必额外轮询
            current = await get_task(task_id)
            if current is not None:
                yield _sse(
                    {"id": 0, "event": "state", "data": _public_task(current)}
                )
            if current is not None and current["status"] in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                # 终态任务不会再有新事件，推完现状即可关闭连接
                yield _sse({"id": 0, "event": "closed", "data": {"status": current["status"]}})
                return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # SSE 注释行，仅用于保活
                    continue
                yield _sse(payload)
                if payload["event"] in {"complete", "error"}:
                    break
        except asyncio.CancelledError:  # pragma: no cover - 客户端断开
            raise
        finally:
            bus.unsubscribe(task_id, queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 Nginx 缓冲，否则 SSE 会被攒批
        },
    )


def _public_task(task: dict) -> dict:
    out = {k: v for k, v in task.items() if k != "source_path"}
    out["has_result"] = bool(task.get("result_file_path"))
    return out


def _sse(payload: dict) -> str:
    body = json.dumps(payload["data"], ensure_ascii=False, default=str)
    lines = []
    if payload.get("id"):
        lines.append(f"id: {payload['id']}")
    lines.append(f"event: {payload['event']}")
    for line in body.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"
