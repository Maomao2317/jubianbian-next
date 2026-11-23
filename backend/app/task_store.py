"""Task persistence and timeline helpers."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from .auth_store import db, now_iso
from .config import REQUEST_ID
from .errors import ArkError
from .logging_setup import logger, safe_error_text
from .media import default_batch_title, episode_from_filename, is_default_batch_title, title_with_episode
from .script import apply_batch_character_continuity, normalize_script
from .recognition_cache import cleanup_task_cache, restore_cached_scene_groups
from .storage import delete_media
from .pricing import task_rate

# Read adapters keep the API shape independent from SQLite column names.
def row_to_task(row: sqlite3.Row, *, include_result: bool = True) -> dict[str, Any]:
    task = dict(row)
    file_name = str(task.pop("file_name", "") or "")
    display_title = title_with_episode(str(task.get("title") or "未命名视频"), file_name)
    task["title"] = display_title
    raw_result_json = task.pop("result_json", None)
    raw_result = json.loads(raw_result_json) if include_result and raw_result_json else None
    raw_delivery_json = task.pop("delivery_json", None)
    task["delivery"] = json.loads(raw_delivery_json) if include_result and raw_delivery_json else None
    quality_json = task.pop("quality_json", None)
    task["quality"] = json.loads(quality_json) if include_result and quality_json else None
    raw_evidence = task.pop("evidence_json", None)
    task["evidence"] = json.loads(raw_evidence) if include_result and raw_evidence else None
    # Normalize on read as well as on generation so older tasks immediately
    # benefit from the same punctuation, VO, scene-granularity and micro-detail
    # rules without rewriting their stored source response.
    if include_result and raw_result:
        try:
            if (task["quality"] or {}).get("contentRepair"):
                raw_result = restore_cached_scene_groups(task["id"], raw_result)
            task["result"] = normalize_script(raw_result, display_title, task["evidence"])
        except (ArkError, TypeError, ValueError, KeyError):
            task["result"] = raw_result
    else:
        task["result"] = None
    task["fileName"] = file_name
    task["sourceLanguage"] = task.pop("source_language", "zh") or "zh"
    task["outputLanguage"] = task.pop("output_language", "zh") or "zh"
    task["episodeNumber"] = episode_from_filename(file_name)
    task["fileSize"] = task.pop("file_size", 0)
    task["mimeType"] = task.pop("mime_type", "video/mp4")
    task["durationSec"] = task.pop("duration_sec", 0)
    task["estimatedMinutes"] = task.pop("estimated_minutes", 0)
    task["creditsUsed"] = task.pop("credits_used", 0)
    task["pointsPerMinute"] = task_rate(task)
    task.pop("points_per_minute", None)
    task["pointsUsed"] = task["creditsUsed"] * task["pointsPerMinute"]
    task["progressPercent"] = task.pop("progress_percent", 0)
    stage_progress = task.pop("stage_progress_percent", None)
    task["stageProgressPercent"] = stage_progress if task.get("stage") == "compressing" and task.get("status") == "running" else None
    task["createdAt"] = task.pop("created_at", None)
    task["updatedAt"] = task.pop("updated_at", None)
    task["completedAt"] = task.pop("completed_at", None)
    task["provider"] = task.pop("provider", None)
    task["model"] = task.pop("model", None)
    input_tokens = task.pop("input_tokens", None)
    output_tokens = task.pop("output_tokens", None)
    total_tokens = task.pop("total_tokens", None)
    task["usage"] = (
        {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
        }
        if any(value is not None for value in (input_tokens, output_tokens, total_tokens))
        else None
    )
    task["apiCostRmb"] = round(float(task.pop("api_cost_rmb", 0) or 0), 6)
    task["batchId"] = task.pop("batch_id", None)
    batch_title = task.pop("batch_title", None)
    # Older rows stored the UTC timestamp directly in their automatic batch
    # title. Re-render only those generated labels; user-supplied names stay
    # untouched.
    if is_default_batch_title(batch_title) and task.get("created_at"):
        batch_title = default_batch_title(task["created_at"])
    task["batchTitle"] = batch_title
    task["batchIndex"] = task.pop("batch_index", None)
    task["batchTotal"] = task.pop("batch_total", None)
    task.pop("stored_path", None)
    return task


# Task mutations and event timeline writes
def get_task(task_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    with db() as connection:
        if user_id:
            row = connection.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)).fetchone()
        else:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row_to_task(row) if row else None


def update_task(task_id: str, *, respect_batch_pause: bool = False, **values: Any) -> str | None:
    if not values:
        return None
    if "stage" in values:
        values.setdefault("stage_progress_percent", None)
    values["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with db() as connection:
        if respect_batch_pause:
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            if batch_is_paused(connection, task_id):
                values.update(status="paused", stage="paused")
        connection.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*values.values(), task_id),
        )
    return values.get("status")


def batch_is_paused(connection: sqlite3.Connection, task_id: str) -> bool:
    return connection.execute(
        """SELECT 1 FROM tasks AS task JOIN tasks AS sibling
           ON sibling.batch_id = task.batch_id AND sibling.user_id IS task.user_id
           WHERE task.id = ? AND sibling.status = 'paused' LIMIT 1""",
        (task_id,),
    ).fetchone() is not None


def finish_task_upload(
    connection: sqlite3.Connection, task_id: str,
    *, local_files: tuple[Path, Path] | None = None, expected_updated_at: str | None = None,
    media: tuple[int, float] | None = None,
) -> str | None:
    """Publish an uploaded file without letting an in-flight upload undo a pause."""
    media_fields = "file_size = ?, duration_sec = ?," if media is not None else ""
    accepted = connection.execute(
        f"""UPDATE tasks SET stage = status, {media_fields}
           progress_percent = 4, error = NULL, updated_at = ?
           WHERE id = ? AND status IN ('queued', 'paused') AND stage = 'uploading'
           AND (? IS NULL OR updated_at = ?) RETURNING status""",
        (*(media or ()), now_iso(), task_id, expected_updated_at, expected_updated_at),
    ).fetchone()
    if accepted is None:
        return None
    if local_files:
        temporary, target = local_files
        temporary.replace(target)
    status = accepted["status"]
    record_task_events(connection, [dict(
        task_id=task_id, event_type="upload_reconciled" if expected_updated_at is not None else "uploaded",
        status=status, stage=status, progress_percent=4,
        message="视频上传完成，等待继续" if status == "paused" else "视频上传完成，已进入识别队列",
    )])
    return status


def batch_character_context(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any] | None:
    """Build a best-effort roster from completed earlier episodes, without waiting.

    Episodes are recognized concurrently. Pending or failed episodes are
    skipped, while any already completed earlier cast profiles can still guide
    the current recognition call.
    """
    if not {"batch_id", "user_id"}.issubset(row.keys()) or not row["batch_id"] or not row["user_id"]:
        return None
    with db() as connection:
        rows = connection.execute(
            """SELECT id, title, status, result_json FROM tasks
               WHERE batch_id = ? AND user_id = ?
               ORDER BY COALESCE(batch_index, 2147483647), created_at, id""",
            (row["batch_id"], row["user_id"]),
        ).fetchall()
    if not rows:
        return None
    current_index = next((index for index, item in enumerate(rows) if item["id"] == row["id"]), None)
    if current_index is None or current_index == 0:
        return None
    earlier_rows = rows[:current_index]
    scripts = []
    reference_title = None
    for earlier in earlier_rows:
        if earlier["status"] not in {"done", "review", "finalizing"}:
            continue
        try:
            script = json.loads(earlier["result_json"] or "")
        except (TypeError, ValueError):
            continue
        if isinstance(script, dict):
            scripts.append(script)
            if reference_title is None:
                reference_title = earlier["title"]
    apply_batch_character_continuity(scripts)
    profiles: dict[str, dict[str, Any]] = {}
    for script in scripts:
        for profile in script.get("characterProfiles") or []:
            if not isinstance(profile, dict) or not isinstance(profile.get("name"), str) or not profile["name"].strip():
                continue
            compact = {key: profile[key] for key in ("name", "aliases", "appearance", "clothing", "species",
                                                     "nameEvidence", "identityEvidence", "relationships") if key in profile}
            # Names in this context already came from an earlier episode.
            # Expose the lock explicitly so the provider cannot downgrade an
            # established name to 男主/女主 or invent a replacement name.
            compact["canonicalNameLocked"] = True
            existing = profiles.setdefault(profile["name"], compact)
            existing["aliases"] = list(dict.fromkeys(existing.get("aliases", []) + compact.get("aliases", [])))
            for key, value in compact.items():
                if not existing.get(key):
                    existing[key] = value
    return {"referenceTitle": reference_title, "characterProfiles": list(profiles.values())} if profiles else None


def reconcile_batch_character_continuity(batch_id: str | None) -> int:
    """Publish batch drafts and their reconciled cast together.

    Recognition runs independently per episode and may finish out of order.
    Reconcile once, after every task in the batch is terminal, so the O(batch
    size) parse/merge pass does not become O(batch size squared).
    The source result and its delivery copy are kept in lockstep; source
    evidence and QA records remain untouched for auditability.
    """
    if not batch_id:
        return 0
    with db() as connection:
        blocking_task = """SELECT 1 FROM tasks WHERE batch_id = ?
                           AND status NOT IN ('done', 'review', 'failed', 'finalizing') LIMIT 1"""
        if connection.execute(blocking_task, (batch_id,)).fetchone():
            return 0
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        # Recheck after obtaining the write lock in case an episode was retried
        # or another worker finished publishing this batch.
        if connection.execute(blocking_task, (batch_id,)).fetchone():
            return 0
        rows = connection.execute(
            """
            SELECT id, result_json, delivery_json
            FROM tasks
            WHERE batch_id = ? AND status IN ('done', 'review', 'finalizing') AND result_json IS NOT NULL
            ORDER BY COALESCE(batch_index, 2147483647), created_at ASC, id ASC
            """,
            (batch_id,),
        ).fetchall()
        if not rows:
            return 0
        scripts: list[dict[str, Any]] = []
        deliveries: list[dict[str, Any] | None] = []
        usable_rows: list[sqlite3.Row] = []
        for row in rows:
            try:
                value = json.loads(row["result_json"] or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            usable_rows.append(row)
            scripts.append(value)
            delivery = None
            try:
                candidate = json.loads(row["delivery_json"] or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                candidate = None
            if isinstance(candidate, dict):
                delivery = candidate
            deliveries.append(delivery)
        changed = apply_batch_character_continuity(scripts) if len(scripts) >= 2 else 0
        delivery_changed = apply_batch_character_continuity(
            [delivery for delivery in deliveries if delivery is not None]
        ) if any(delivery is not None for delivery in deliveries) else 0
        timestamp = now_iso()
        if changed or delivery_changed:
            for row, script, delivery in zip(usable_rows, scripts, deliveries):
                connection.execute(
                    "UPDATE tasks SET result_json = ?, delivery_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(script, ensure_ascii=False),
                     json.dumps(delivery, ensure_ascii=False) if delivery is not None else row["delivery_json"],
                     timestamp, row["id"]),
                )
        waiting = connection.execute(
            "SELECT id FROM tasks WHERE batch_id = ? AND status = 'finalizing'",
            (batch_id,),
        ).fetchall()
        if waiting:
            connection.execute(
                """UPDATE tasks SET status = 'done', stage = 'done', progress_percent = 100,
                   completed_at = ?, updated_at = ? WHERE batch_id = ? AND status = 'finalizing'""",
                (timestamp, timestamp, batch_id),
            )
            record_task_events(connection, [
                {"task_id": row["id"], "event_type": "completed", "status": "done",
                 "stage": "done", "progress_percent": 100, "message": "人物名称已统一，剧本可下载"}
                for row in waiting
            ])
        return changed + delivery_changed


def finalize_ready_batches() -> None:
    """Recover drafts left waiting after interruption or a database lock."""
    with db() as connection:
        batches = connection.execute(
            """SELECT DISTINCT waiting.batch_id FROM tasks waiting
               WHERE waiting.status = 'finalizing' AND NOT EXISTS (
                   SELECT 1 FROM tasks sibling WHERE sibling.batch_id = waiting.batch_id
                   AND sibling.status NOT IN ('done', 'review', 'failed', 'finalizing')
               )"""
        ).fetchall()
    for batch in batches:
        reconcile_batch_character_continuity(batch["batch_id"])


def purge_deleted_tasks(active_ids: set[str] | None = None, limit: int = 20) -> None:
    """Remove media before dropping the row needed to retry a failed deletion."""
    with db() as connection:
        rows = connection.execute(
            """SELECT id, stored_path FROM tasks WHERE status = 'deleting'
               ORDER BY updated_at, id LIMIT ?""",
            (limit,),
        ).fetchall()
    for row in rows:
        task_id = row["id"]
        if active_ids and task_id in active_ids:
            continue
        try:
            cleanup_task_cache(task_id, strict=True)
            delete_media(row["stored_path"])
        except Exception as exc:
            logger.warning("admin_task_purge_failed task_id=%s error=%s", task_id, safe_error_text(exc))
            # Move failed rows to the back so other pending deletions can run.
            with db() as connection:
                connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ? AND status = 'deleting'",
                                   (now_iso(), task_id))
            continue
        with db() as connection:
            deleted = connection.execute("DELETE FROM tasks WHERE id = ? AND status = 'deleting'", (task_id,)).rowcount
            if deleted:
                connection.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
                connection.execute("DELETE FROM task_recheck_snapshots WHERE task_id = ?", (task_id,))


def record_task_event(
    task_id: str,
    event_type: str,
    message: str = "",
    *,
    status: str | None = None,
    stage: str | None = None,
    progress_percent: int | None = None,
    duration_ms: float | None = None,
) -> None:
    """Persist a small task timeline while keeping log messages searchable."""
    try:
        with db() as connection:
            _insert_task_event(
                connection, task_id, event_type, message,
                status=status, stage=stage, progress_percent=progress_percent,
                duration_ms=duration_ms,
            )
    except sqlite3.Error:
        logger.exception("task_event_persist_failed task_id=%s event_type=%s", task_id, event_type)


def _insert_task_event(
    connection: sqlite3.Connection,
    task_id: str,
    event_type: str,
    message: str = "",
    *,
    status: str | None = None,
    stage: str | None = None,
    progress_percent: int | None = None,
    duration_ms: float | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO task_events
          (task_id, event_type, status, stage, progress_percent, message, duration_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            event_type,
            status,
            stage,
            progress_percent,
            message[:1000],
            duration_ms,
            now_iso(),
        ),
    )


def record_task_events(connection: sqlite3.Connection, events: list[dict[str, Any]]) -> None:
    """Write a group of timeline events on an existing transaction.

    Batch creation can reserve hundreds of episodes in one transaction.  Keep
    its initial timeline writes on that connection so it does not open and
    commit one SQLite transaction per episode.
    """
    try:
        for event in events:
            _insert_task_event(connection, **event)
    except sqlite3.Error:
        logger.exception("task_event_batch_persist_failed count=%s", len(events))


def mark_task_stage(
    task_id: str,
    *,
    status: str,
    stage: str,
    progress_percent: int,
    message: str,
    event_type: str = "stage",
) -> None:
    update_task(task_id, status=status, stage=stage, progress_percent=progress_percent, error=None)
    record_task_event(
        task_id,
        event_type,
        message,
        status=status,
        stage=stage,
        progress_percent=progress_percent,
    )
    logger.info(
        "task_stage task_id=%s status=%s stage=%s progress=%s message=%s request_id=%s",
        task_id,
        status,
        stage,
        progress_percent,
        message,
        REQUEST_ID.get(),
    )


def task_events(task_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute(
            """
            SELECT event_type, status, stage, progress_percent, message, duration_ms, created_at
            FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?
            """,
            (task_id, max(1, min(limit, 200))),
        ).fetchall()
    return [
        {
            "type": row["event_type"],
            "status": row["status"],
            "stage": row["stage"],
            "progressPercent": row["progress_percent"],
            "message": row["message"],
            "durationMs": row["duration_ms"],
            "createdAt": row["created_at"],
        }
        for row in reversed(rows)
    ]


def task_counts() -> dict[str, int]:
    with db() as connection:
        rows = connection.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0) + counts.get("finalizing", 0),
        "review": counts.get("review", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "total": sum(counts.values()),
    }


def task_row(task_id: str, user_id: str | None = None) -> sqlite3.Row | None:
    with db() as connection:
        if user_id:
            return connection.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)).fetchone()
        return connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
