"""Task persistence and timeline helpers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .auth_store import db, now_iso
from .config import REQUEST_ID
from .errors import ArkError
from .logging_setup import logger
from .media import episode_from_filename, title_with_episode
from .script import normalize_script

# Read adapters keep the API shape independent from SQLite column names.
def row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    task = dict(row)
    file_name = str(task.pop("file_name") or "")
    display_title = title_with_episode(str(task.get("title") or "未命名视频"), file_name)
    task["title"] = display_title
    raw_result = json.loads(task.pop("result_json")) if task.get("result_json") else None
    # Normalize on read as well as on generation so older tasks immediately
    # benefit from the same punctuation, VO, scene-granularity and micro-detail
    # rules without rewriting their stored source response.
    if raw_result:
        try:
            task["result"] = normalize_script(raw_result, display_title)
        except (ArkError, TypeError, ValueError, KeyError):
            task["result"] = raw_result
    else:
        task["result"] = None
    task["quality"] = json.loads(task.pop("quality_json")) if task.get("quality_json") else None
    task["fileName"] = file_name
    task["episodeNumber"] = episode_from_filename(file_name)
    task["fileSize"] = task.pop("file_size")
    task["mimeType"] = task.pop("mime_type")
    task["durationSec"] = task.pop("duration_sec")
    task["estimatedMinutes"] = task.pop("estimated_minutes")
    task["creditsUsed"] = task.pop("credits_used")
    task["progressPercent"] = task.pop("progress_percent")
    task["createdAt"] = task.pop("created_at")
    task["updatedAt"] = task.pop("updated_at")
    task["completedAt"] = task.pop("completed_at")
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
    task["batchTitle"] = task.pop("batch_title", None)
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


def update_task(task_id: str, **values: Any) -> None:
    if not values:
        return
    values["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with db() as connection:
        connection.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*values.values(), task_id),
        )


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
    except sqlite3.Error:
        logger.exception("task_event_persist_failed task_id=%s event_type=%s", task_id, event_type)


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
        "running": counts.get("running", 0),
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
