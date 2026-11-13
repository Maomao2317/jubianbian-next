"""Snapshot helpers for administrator-triggered rechecks of completed tasks."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .auth_store import db, now_iso


SNAPSHOT_FIELDS = (
    "status",
    "stage",
    "progress_percent",
    "error",
    "result_json",
    "quality_json",
    "evidence_json",
    "provider",
    "model",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "api_cost_rmb",
    "attempts",
    "completed_at",
)


def latest_pending_recheck(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, task_id, snapshot_json, state, created_at, resolved_at
        FROM task_recheck_snapshots
        WHERE task_id = ? AND state = 'pending'
        ORDER BY id DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()


def pending_recheck_snapshot(task_id: str) -> sqlite3.Row | None:
    with db() as connection:
        return latest_pending_recheck(connection, task_id)


def has_pending_recheck(task_id: str) -> bool:
    return pending_recheck_snapshot(task_id) is not None


def create_recheck_snapshot(connection: sqlite3.Connection, row: sqlite3.Row) -> int:
    task_id = str(row["id"])
    if latest_pending_recheck(connection, task_id):
        raise sqlite3.IntegrityError("task already has a pending recheck snapshot")
    row_keys = set(row.keys())
    payload = {field: row[field] for field in SNAPSHOT_FIELDS if field in row_keys}
    cursor = connection.execute(
        """
        INSERT INTO task_recheck_snapshots(task_id, snapshot_json, state, created_at)
        VALUES (?, ?, 'pending', ?)
        """,
        (task_id, json.dumps(payload, ensure_ascii=False), now_iso()),
    )
    return int(cursor.lastrowid)


def mark_recheck_resolved(
    connection: sqlite3.Connection,
    task_id: str,
    state: str,
) -> bool:
    if state not in {"approved", "restored", "failed_restored"}:
        raise ValueError("invalid recheck resolution")
    snapshot = latest_pending_recheck(connection, task_id)
    if not snapshot:
        return False
    changed = connection.execute(
        """
        UPDATE task_recheck_snapshots
        SET state = ?, resolved_at = ?
        WHERE id = ? AND state = 'pending'
        """,
        (state, now_iso(), snapshot["id"]),
    ).rowcount
    return changed == 1


def restore_recheck_snapshot_with_connection(
    connection: sqlite3.Connection,
    task_id: str,
    *,
    resolution: str = "restored",
    allowed_statuses: tuple[str, ...] = ("review", "failed"),
) -> bool:
    snapshot = latest_pending_recheck(connection, task_id)
    if not snapshot:
        return False
    try:
        payload = json.loads(snapshot["snapshot_json"])
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    values: dict[str, Any] = {field: payload.get(field) for field in SNAPSHOT_FIELDS}
    values.update({
        "status": "done",
        "stage": "done",
        "progress_percent": 100,
        "error": None,
        "updated_at": now_iso(),
    })
    placeholders = ",".join("?" for _ in allowed_statuses)
    assignments = ", ".join(f"{field} = ?" for field in values)
    changed = connection.execute(
        f"UPDATE tasks SET {assignments} WHERE id = ? AND status IN ({placeholders})",
        (*values.values(), task_id, *allowed_statuses),
    ).rowcount
    if changed != 1:
        return False
    return mark_recheck_resolved(connection, task_id, resolution)


def restore_recheck_snapshot(
    task_id: str,
    *,
    resolution: str = "restored",
    allowed_statuses: tuple[str, ...] = ("review", "failed"),
) -> bool:
    with db() as connection:
        return restore_recheck_snapshot_with_connection(
            connection,
            task_id,
            resolution=resolution,
            allowed_statuses=allowed_statuses,
        )
