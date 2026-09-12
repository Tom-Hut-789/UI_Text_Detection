"""SQLite 存储层。

两张表：
  tasks       —— 任务主表（对应 SDD 5.1，并补充若干运行期字段）
  row_results —— 逐张图片的检测结果，支撑 SDD 7.2 §3 的断点续传
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    filename         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'PENDING',
    total_rows       INTEGER NOT NULL DEFAULT 0,
    processed_rows   INTEGER NOT NULL DEFAULT 0,
    total_images     INTEGER NOT NULL DEFAULT 0,
    processed_images INTEGER NOT NULL DEFAULT 0,
    defective_images INTEGER NOT NULL DEFAULT 0,
    result_file_path TEXT,
    error_message    TEXT,
    source_path      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS row_results (
    task_id    TEXT NOT NULL,
    row_index  INTEGER NOT NULL,
    col_index  INTEGER NOT NULL,
    language   TEXT NOT NULL,
    is_english INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL,
    result     TEXT,
    error      TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, row_index, col_index)
);

CREATE INDEX IF NOT EXISTS idx_row_results_task ON row_results(task_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(settings.db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def init_db() -> None:
    settings.ensure_dirs()
    conn = await connect()
    try:
        await conn.executescript(SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


async def create_task(task_id: str, filename: str, source_path: str) -> dict[str, Any]:
    ts = _now()
    conn = await connect()
    try:
        await conn.execute(
            "INSERT INTO tasks (id, filename, status, source_path, created_at, updated_at)"
            " VALUES (?, ?, 'PENDING', ?, ?, ?)",
            (task_id, filename, source_path, ts, ts),
        )
        await conn.commit()
    finally:
        await conn.close()
    task = await get_task(task_id)
    assert task is not None
    return task


async def get_task(task_id: str) -> dict[str, Any] | None:
    conn = await connect()
    try:
        cur = await conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def list_tasks(limit: int = 50) -> list[dict[str, Any]]:
    conn = await connect()
    try:
        cur = await conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = await connect()
    try:
        await conn.execute(
            f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id)
        )
        await conn.commit()
    finally:
        await conn.close()


async def bump_task_counters(
    task_id: str, *, images: int = 0, rows: int = 0, defects: int = 0
) -> None:
    """原子自增进度计数器，避免并发写入时丢更新。"""
    conn = await connect()
    try:
        await conn.execute(
            "UPDATE tasks SET processed_images = processed_images + ?,"
            " processed_rows = processed_rows + ?,"
            " defective_images = defective_images + ?,"
            " updated_at = ? WHERE id = ?",
            (images, rows, defects, _now(), task_id),
        )
        await conn.commit()
    finally:
        await conn.close()


async def save_row_result(
    task_id: str,
    row_index: int,
    col_index: int,
    language: str,
    is_english: bool,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    conn = await connect()
    try:
        await conn.execute(
            "INSERT OR REPLACE INTO row_results"
            " (task_id, row_index, col_index, language, is_english, status, result, error, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                row_index,
                col_index,
                language,
                int(is_english),
                status,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error,
                _now(),
            ),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_row_results(task_id: str) -> list[dict[str, Any]]:
    conn = await connect()
    try:
        cur = await conn.execute(
            "SELECT * FROM row_results WHERE task_id = ? ORDER BY row_index, col_index",
            (task_id,),
        )
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["result"] = json.loads(d["result"]) if d["result"] else None
            out.append(d)
        return out
    finally:
        await conn.close()


async def get_done_keys(task_id: str) -> set[tuple[int, int]]:
    """已成功处理的行列坐标集合，用于断点续传时跳过。"""
    conn = await connect()
    try:
        cur = await conn.execute(
            "SELECT row_index, col_index FROM row_results"
            " WHERE task_id = ? AND status = 'SUCCESS'",
            (task_id,),
        )
        return {(r["row_index"], r["col_index"]) for r in await cur.fetchall()}
    finally:
        await conn.close()


async def mark_interrupted_tasks() -> int:
    """进程重启后，把上次残留的 PROCESSING 任务标记为 INTERRUPTED，等待用户续跑。"""
    conn = await connect()
    try:
        cur = await conn.execute(
            "UPDATE tasks SET status = 'INTERRUPTED', updated_at = ? WHERE status IN ('PROCESSING','PENDING')",
            (_now(),),
        )
        await conn.commit()
        return cur.rowcount or 0
    finally:
        await conn.close()
