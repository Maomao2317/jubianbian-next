"""Durable database-backed task worker."""

from __future__ import annotations

import asyncio

from .auth_store import db, now_iso
from .config import WORKER_CONCURRENCY
from .logging_setup import logger
from .processing import process_task


async def run_worker() -> None:
    with db() as connection:
        connection.execute("UPDATE tasks SET status = 'queued', stage = 'queued', progress_percent = 4, updated_at = ? WHERE status = 'running'", (now_iso(),))
    active: set[asyncio.Task] = set()
    while True:
        while len(active) < WORKER_CONCURRENCY:
            with db() as connection:
                row = connection.execute("SELECT id FROM tasks WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1").fetchone()
                if row:
                    connection.execute("UPDATE tasks SET status = 'running', stage = 'queued', updated_at = ? WHERE id = ? AND status = 'queued'", (now_iso(), row["id"]))
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
