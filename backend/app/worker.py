"""Durable database-backed task worker."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

from .auth_store import db, now_iso
from .config import UPLOAD_RESERVATION_TTL_SECONDS, WORKER_CONCURRENCY
from .logging_setup import logger, safe_error_text
from .processing import _refund_task_once, process_task
from .rechecks import restore_recheck_snapshot_with_connection
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
           FROM (SELECT DISTINCT user_id FROM tasks INDEXED BY idx_tasks_ready_user
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


def _recover_unexpected_task_exit(task_id: str, error: BaseException | None) -> None:
    detail = safe_error_text(error) if error else "task cancelled"
    retry_message = "处理进程意外中断，任务已自动重新排队。"
    failure_message = "任务处理意外中断。为避免重复调用，系统已停止自动重试并退回本集积分，请重新提交。"
    outcome = None
    task = None
    with db() as connection:
        task = connection.execute(
            "SELECT id, user_id, status, stage, attempts, credits_used FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not task or task["status"] != "running":
            return
        stage = str(task["stage"] or "")
        attempts = int(task["attempts"] or 0)
        if stage in {"queued", "probing"} and attempts < 2:
            changed = connection.execute(
                "UPDATE tasks SET status = 'queued', stage = 'queued', progress_percent = 4, attempts = ?, error = ?, updated_at = ? WHERE id = ? AND status = 'running' AND stage = ?",
                (attempts + 1, retry_message, now_iso(), task_id, stage),
            ).rowcount
            outcome = "retry" if changed == 1 else None
        elif restore_recheck_snapshot_with_connection(
            connection,
            task_id,
            resolution="failed_restored",
            allowed_statuses=("running",),
        ):
            outcome = "restored"
        else:
            completed = now_iso()
            changed = connection.execute(
                "UPDATE tasks SET status = 'failed', stage = 'failed', progress_percent = 100, error = ?, completed_at = ?, updated_at = ? WHERE id = ? AND status = 'running'",
                (failure_message, completed, completed, task_id),
            ).rowcount
            if changed == 1:
                if task["user_id"] and int(task["credits_used"] or 0) > 0:
                    _refund_task_once(
                        task_id,
                        task["user_id"],
                        int(task["credits_used"] or 0),
                        connection=connection,
                    )
                outcome = "failed"

    if outcome == "retry":
        record_task_event(task_id, "worker_recovered", retry_message, status="queued", stage="queued", progress_percent=4)
        logger.warning("worker_task_recovered task_id=%s outcome=retry stage=%s error=%s", task_id, stage, detail)
    elif outcome == "restored":
        record_task_event(task_id, "worker_recovered", "复核处理意外中断，已恢复上次完成版本。", status="done", stage="done", progress_percent=100)
        logger.error("worker_task_recovered task_id=%s outcome=restored stage=%s error=%s", task_id, stage, detail)
    elif outcome == "failed":
        record_task_event(task_id, "worker_interrupted", failure_message, status="failed", stage="failed", progress_percent=100)
        logger.error("worker_task_recovered task_id=%s outcome=failed stage=%s error=%s", task_id, stage, detail)


async def run_worker() -> None:
    pending_recoveries: dict[str, tuple[BaseException, int, float]] = {}
    recovery_scan_complete = False
    active: set[asyncio.Task] = set()
    task_users: dict[asyncio.Task, tuple[str, str]] = {}
    active_users: Counter = Counter()
    last_user = None
    last_upload_cleanup = 0.0
    while True:
        now = asyncio.get_running_loop().time()
        if not recovery_scan_complete:
            try:
                with db() as connection:
                    abandoned = connection.execute(
                        "SELECT id FROM tasks WHERE status = 'running' ORDER BY updated_at, id"
                    ).fetchall()
            except Exception:
                logger.exception("worker_startup_recovery_scan_failed retry_in=1")
                await asyncio.sleep(1)
                continue
            if abandoned:
                logger.warning("worker_startup_recovering count=%s", len(abandoned))
            pending_recoveries = {
                str(row["id"]): (RuntimeError("worker restarted"), 0, 0.0)
                for row in abandoned
            }
            recovery_scan_complete = True
        for task_id, (task_error, failures, retry_at) in list(pending_recoveries.items()):
            if now < retry_at:
                continue
            try:
                _recover_unexpected_task_exit(task_id, task_error)
            except Exception:
                failures += 1
                delay = min(30, 2 ** min(failures, 5))
                pending_recoveries[task_id] = (task_error, failures, now + delay)
                logger.exception(
                    "worker_recovery_failed task_id=%s attempt=%s retry_in=%s",
                    task_id,
                    failures,
                    delay,
                )
            else:
                pending_recoveries.pop(task_id, None)
        if now - last_upload_cleanup >= 60:
            last_upload_cleanup = now
            try:
                await _expire_incomplete_uploads()
            except Exception:
                logger.exception("worker_upload_cleanup_failed retry_in=60")
        while len(active) < WORKER_CONCURRENCY:
            try:
                with db() as connection:
                    claimed = _claim_next_task(connection, active_users, last_user)
            except Exception:
                logger.exception("worker_queue_poll_failed retry_in=1")
                break
            if not claimed:
                break
            task_id, user = claimed
            task = asyncio.create_task(process_task(task_id))
            active.add(task)
            task_users[task] = (task_id, user)
            active_users[user] += 1
            last_user = user
        if active:
            # Uploads finish independently of running recognizers. Refill free
            # slots while they run rather than waiting minutes for one to end.
            done, active = await asyncio.wait(active, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task_id, user = task_users.pop(task)
                active_users[user] -= 1
                if task.cancelled():
                    pending_recoveries[task_id] = (RuntimeError("task cancelled"), 0, 0.0)
                    continue
                error = task.exception()
                if error:
                    logger.error("worker_task_crashed task_id=%s error=%s", task_id, safe_error_text(error))
                    pending_recoveries[task_id] = (error, 0, 0.0)
        else:
            await asyncio.sleep(1)
