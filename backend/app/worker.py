"""Durable database-backed task worker."""

from __future__ import annotations

import asyncio

from .auth_store import db, now_iso
from .logging_setup import logger
from .processing import process_task


async def run_worker() -> None:
    with db() as connection:
        connection.execute("UPDATE tasks SET status = 'queued', stage = 'queued', progress_percent = 4, updated_at = ? WHERE status = 'running'", (now_iso(),))
    while True:
        with db() as connection:
            row = connection.execute("SELECT id FROM tasks WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1").fetchone()
            if row:
                connection.execute("UPDATE tasks SET status = 'running', stage = 'queued', updated_at = ? WHERE id = ? AND status = 'queued'", (now_iso(), row["id"]))
        if row:
            try:
                await process_task(row["id"])
            except Exception:
                logger.exception("worker_task_crashed task_id=%s", row["id"])
            continue
        await asyncio.sleep(1)
