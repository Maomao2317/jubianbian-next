"""Durable database-backed task worker."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

from .auth_store import db, now_iso
from .config import UPLOAD_RESERVATION_TTL_SECONDS, WORKER_CONCURRENCY
from .logging_setup import logger
from .processing import _refund_task_once, process_task
from .storage import delete_media
from .task_store import record_task_event


def _claim_next_task(connection, active_users: Counter, last_user: str | None):
    """Choose a least-occupied ready user, rotating ties; FIFO within each user."""
    connection.execute("BEGIN IMMEDIATE")
    # Only fetch each user's FIFO head.  The previous query materialized every
    # queued row on every free worker slot, so a 999-episode batch repeatedly
    # scanned and transferred the same large queue while choosing one task.
    rows = connection.execute(
        """SELECT task.id, task.user_id
           FROM (SELECT DISTINCT user_id FROM tasks
                 WHERE status = 'queued' AND stage = 'queued') AS owner
           JOIN tasks AS task ON task.id = (
               SELECT ready.id FROM tasks AS ready
               WHERE ready.user_id IS owner.user_id
                 AND ready.status = 'queued' AND ready.stage = 'queued'
               ORDER BY ready.created_at, ready.id LIMIT 1
           )
           ORDER BY task.created_at, task.id"""
    ).fetchall()
    heads = {}
    for row in rows:
        heads.setdefault(str(row["user_id"] or ""), row)
    if not heads:
        return None
    minimum = min(active_users[user] for user in heads)
    candidates = sorted(user for user in heads if active_users[user] == minimum)
    user = next((user for user in candidates if last_user is None or user > last_user), candidates[0])
    row = heads[user]
    claimed = connection.execute(
        "UPDATE tasks SET status = 'running', stage = 'queued', updated_at = ? WHERE id = ? AND status = 'queued' AND stage = 'queued'",
        (now_iso(), row["id"]),
    ).rowcount
    return (row["id"], user) if claimed == 1 else None


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
    task_users: dict[asyncio.Task, str] = {}
    active_users: Counter = Counter()
    last_user = None
    last_upload_cleanup = 0.0
    while True:
        now = asyncio.get_running_loop().time()
        if now - last_upload_cleanup >= 60:
            await _expire_incomplete_uploads()
            last_upload_cleanup = now
        while len(active) < WORKER_CONCURRENCY:
            with db() as connection:
                claimed = _claim_next_task(connection, active_users, last_user)
            if not claimed:
                break
            task_id, user = claimed
            task = asyncio.create_task(process_task(task_id))
            active.add(task)
            task_users[task] = user
            active_users[user] += 1
            last_user = user
        if active:
            # Uploads finish independently of running recognizers. Refill free
            # slots while they run rather than waiting minutes for one to end.
            done, active = await asyncio.wait(active, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                active_users[task_users.pop(task)] -= 1
                if task.exception():
                    logger.error("worker_task_crashed error=%s", task.exception())
        else:
            await asyncio.sleep(1)
