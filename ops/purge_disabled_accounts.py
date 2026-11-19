"""Audit or permanently purge disabled production accounts and their records.

Run inside the production app container with PYTHONPATH=/app/backend:
    python /app/ops/purge_disabled_accounts.py audit
    python /app/ops/purge_disabled_accounts.py apply USERS TASKS LEDGER

No account identifiers, credentials, media locations, or business records are
printed. The apply command needs the exact audited counts and creates a private
temporary SQLite backup; it removes the backup only after full verification.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys

from app import config, recognition_cache, storage


def snapshot(connection: sqlite3.Connection) -> dict:
    users = connection.execute(
        "SELECT id, email, role FROM users WHERE is_active = 0 ORDER BY id"
    ).fetchall()
    assert all(row[2] == "user" for row in users), "Disabled administrator requires separate review"
    ids = [row[0] for row in users]
    emails = [row[1] for row in users]
    if not ids:
        return {"users": [], "emails": [], "tasks": [], "ledger": 0, "events": 0,
                "rechecks": 0, "auth_codes": 0, "audit_logs": 0, "recharges": 0}
    marks = ",".join("?" for _ in ids)
    mail_marks = ",".join("?" for _ in emails)
    tasks = connection.execute(
        f"SELECT id, stored_path, status FROM tasks WHERE user_id IN ({marks}) ORDER BY id", ids
    ).fetchall()
    task_ids = [row[0] for row in tasks]
    task_marks = ",".join("?" for _ in task_ids)
    count = lambda table, field, values, placeholders: connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {field} IN ({placeholders})", values
    ).fetchone()[0] if values else 0
    return {
        "users": ids, "emails": emails, "tasks": tasks,
        "ledger": count("credit_ledger", "user_id", ids, marks),
        "events": count("task_events", "task_id", task_ids, task_marks),
        "rechecks": count("task_recheck_snapshots", "task_id", task_ids, task_marks),
        "auth_codes": count("auth_codes", "email", emails, mail_marks),
        "audit_logs": connection.execute(
            f"SELECT COUNT(*) FROM admin_audit_logs WHERE target_id IN ({marks}) OR admin_user_id IN ({marks})",
            ids + ids,
        ).fetchone()[0],
        "recharges": count("admin_recharge_ledger", "user_id", ids, marks),
    }


def public_summary(target: dict) -> dict:
    return {"users": len(target["users"]), "tasks": len(target["tasks"]),
            "ledger": target["ledger"], "events": target["events"],
            "rechecks": target["rechecks"], "auth_codes": target["auth_codes"],
            "audit_logs": target["audit_logs"], "recharges": target["recharges"],
            "object_media": sum(storage.is_object_uri(row[1]) for row in target["tasks"]),
            "local_media": sum(not storage.is_object_uri(row[1]) for row in target["tasks"])}


def verify_media_scope(tasks: list[tuple]) -> None:
    uploads = config.UPLOAD_DIR.resolve()
    for task_id, uri, status in tasks:
        assert status not in {"queued", "running", "uploading"}, "Active task cannot be purged"
        assert isinstance(task_id, str) and task_id
        if storage.is_object_uri(uri):
            bucket, key = storage._object_parts(uri)
            allowed_buckets = {config.OBJECT_STORAGE_BUCKET, config.OBJECT_STORAGE_LEGACY_BUCKET}
            assert bucket in allowed_buckets and bucket, "Unexpected media bucket"
            assert key == task_id + ".mp4" or key.endswith("/" + task_id + ".mp4"), (
                "Unexpected object key"
            )
        else:
            path = Path(uri).resolve()
            assert path.is_relative_to(uploads) and path.name == task_id + ".mp4", (
                "Unexpected local media path"
            )


def delete_records(connection: sqlite3.Connection, target: dict) -> None:
    ids = target["users"]
    emails = target["emails"]
    task_ids = [row[0] for row in target["tasks"]]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    mail_marks = ",".join("?" for _ in emails)
    if task_ids:
        task_marks = ",".join("?" for _ in task_ids)
        for table in ("task_events", "task_recheck_snapshots"):
            connection.execute(f"DELETE FROM {table} WHERE task_id IN ({task_marks})", task_ids)
        connection.execute(f"DELETE FROM tasks WHERE id IN ({task_marks})", task_ids)
    for table in ("credit_ledger", "admin_recharge_ledger", "sessions"):
        connection.execute(f"DELETE FROM {table} WHERE user_id IN ({marks})", ids)
    connection.execute(f"DELETE FROM auth_codes WHERE email IN ({mail_marks})", emails)
    connection.execute(
        f"DELETE FROM admin_audit_logs WHERE target_id IN ({marks}) OR admin_user_id IN ({marks})",
        ids + ids,
    )
    connection.execute(f"DELETE FROM users WHERE id IN ({marks})", ids)


def verify_records(connection: sqlite3.Connection, target: dict) -> None:
    ids = target["users"]
    emails = target["emails"]
    task_ids = [row[0] for row in target["tasks"]]
    if not ids:
        return
    marks = ",".join("?" for _ in ids)
    mail_marks = ",".join("?" for _ in emails)
    for table in ("users", "tasks", "credit_ledger", "admin_recharge_ledger", "sessions"):
        field = "id" if table == "users" else "user_id"
        assert connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {field} IN ({marks})", ids).fetchone()[0] == 0
    assert connection.execute(f"SELECT COUNT(*) FROM auth_codes WHERE email IN ({mail_marks})", emails).fetchone()[0] == 0
    assert connection.execute(
        f"SELECT COUNT(*) FROM admin_audit_logs WHERE target_id IN ({marks}) OR admin_user_id IN ({marks})",
        ids + ids,
    ).fetchone()[0] == 0
    if task_ids:
        task_marks = ",".join("?" for _ in task_ids)
        for table in ("tasks", "task_events", "task_recheck_snapshots"):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {'id' if table == 'tasks' else 'task_id'} IN ({task_marks})",
                task_ids,
            ).fetchone()[0] == 0


def backup_database(connection: sqlite3.Connection) -> Path:
    directory = config.DATA_DIR / "repair-backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / ("disabled-account-purge-" + stamp + ".sqlite3")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with sqlite3.connect(path) as backup:
        connection.backup(backup)
    return path


def delete_task_cache(task_id: str) -> None:
    directory = recognition_cache.cache_directory(task_id)
    prefix = "/".join(part for part in (config.OBJECT_STORAGE_PREFIX, "recognition") if part) + "/"
    for media_directory in recognition_cache._media_directories(directory):
        metadata = media_directory / "delivery.json"
        if not metadata.exists():
            continue
        uri = json.loads(metadata.read_text(encoding="utf-8"))["data"]["uri"]
        bucket, key = storage._object_parts(uri)
        filename = key.removeprefix(prefix)
        assert bucket == config.OBJECT_STORAGE_BUCKET and key.startswith(prefix)
        assert len(filename) == 68 and filename.endswith(".mp4")
        int(filename[:-4], 16)
        storage.delete_media(uri)
    recognition_cache.cleanup_task_cache(task_id)
    assert not directory.exists(), "Task recognition cache remains"


def main(argv: list[str] | None = None) -> None:
    args = sys.argv if argv is None else argv
    assert args[1:] == ["audit"] or (len(args) == 5 and args[1] == "apply")
    assert config.JBB_ENVIRONMENT == "production", "Production container required"
    with sqlite3.connect(config.DB_PATH, timeout=10) as connection:
        target = snapshot(connection)
        verify_media_scope(target["tasks"])
        summary = public_summary(target)
        if args[1] == "audit":
            print("PURGE_AUDIT", json.dumps(summary, sort_keys=True), flush=True)
            return
        expected = tuple(int(value) for value in args[2:])
        assert expected == (summary["users"], summary["tasks"], summary["ledger"]), (
            "Account counts changed after audit"
        )
        assert summary["users"] > 0
        backup = backup_database(connection)
        connection.execute("BEGIN IMMEDIATE")
        current = snapshot(connection)
        assert current == target, "Account data changed after backup"
        delete_records(connection, target)
        verify_records(connection, target)
        connection.commit()
        errors = 0
        for task_id, uri, _ in target["tasks"]:
            try:
                storage.delete_media(uri)
                delete_task_cache(task_id)
            except Exception:
                errors += 1
        verify_records(connection, target)
        if errors:
            raise RuntimeError(f"Media cleanup failed for {errors} tasks; private backup retained")
        backup.unlink()
        print("PURGE_VERIFIED", json.dumps(summary, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
