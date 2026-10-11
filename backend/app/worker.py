"""Durable database-backed task worker."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .auth_store import db, now_iso
from .config import UPLOAD_RESERVATION_TTL_SECONDS, WORKER_CONCURRENCY
from .logging_setup import logger
from .processing import _refund_task_once, process_task
from .storage import delete_media
from .task_store import record_task_event


async def _expire_incomplete_uploads() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=UPLOAD_RESERVATION_TTL_SECONDS)).isoformat()
    with db() as connection:
        rows = connection.execute(
            "SELECT id, user_id, stored_path, credits_used FROM tasks WHERE status = 'queued' AND stage = 'uploading' AND created_at < ?",
            (cutoff,),
        ).fetchall()
    for row in rows:
        completed = now_iso()
        with db() as connection:
            claimed = connection.execute(
                "UPDATE tasks SET status = 'failed', stage = 'failed', progress_percent = 100, error = ?, completed_at = ?, updated_at = ? WHERE id = ? AND status = 'queued' AND stage = 'uploading'",
                ("视频上传超时，已退回本集积分", completed, completed, row["id"]),
            ).rowcount
        if claimed != 1:
            continue
        try:
            await asyncio.to_thread(delete_media, row["stored_path"])
        except Exception:
            logger.warning("upload_expiry_delete_failed task_id=%s", row["id"])
        _refund_task_once(row["id"], row["user_id"], int(row["credits_used"] or 0))
        record_task_event(row["id"], "upload_expired", "视频上传超时，已退回本集积分", status="failed", stage="failed", progress_percent=100)


async def run_worker() -> None:
    with db() as connection:
        connection.execute("UPDATE tasks SET status = 'queued', stage = 'queued', progress_percent = 4, updated_at = ? WHERE status = 'running'", (now_iso(),))
    active: set[asyncio.Task] = set()
    last_upload_cleanup = 0.0
    while True:
        now = asyncio.get_running_loop().time()
        if now - last_upload_cleanup >= 60:
            await _expire_incomplete_uploads()
            last_upload_cleanup = now
        while len(active) < WORKER_CONCURRENCY:
            with db() as connection:
                row = connection.execute("SELECT id FROM tasks WHERE status = 'queued' AND stage = 'queued' ORDER BY created_at ASC LIMIT 1").fetchone()
                if row:
                    connection.execute("UPDATE tasks SET status = 'running', stage = 'queued', updated_at = ? WHERE id = ? AND status = 'queued' AND stage = 'queued'", (now_iso(), row["id"]))
            if not row:
                break
            active.add(asyncio.create_task(process_task(row["id"])))
        if active:
            done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception():
                    logger.error("worker_task_crashed error=%s", task.exception())
        else:
            await asyncio.sleep(1)
