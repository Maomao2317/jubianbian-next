#!/usr/bin/env python3
"""Delete source videos after 14 days while retaining every task and screenplay.

``audit`` is read-only. ``run`` is idempotent and is also the default for the
existing weekly cleanup command. Active tasks are never eligible.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

backend_dir = Path(__file__).resolve().parents[1] / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app import config, recognition_cache, storage

TERMINAL = {"done", "failed", "review"}
BATCH_SIZE = 100


def cutoff_time(days: int, now: datetime | None = None) -> datetime:
    if not 1 <= days <= 365:
        raise ValueError("Invalid retention period")
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(days=days)


def older_than(value: str, cutoff: datetime) -> bool:
    created = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("Task timestamp has no timezone")
    return created < cutoff


def marker_exists(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_retention'").fetchone() is not None


def candidates(db: sqlite3.Connection, cutoff: datetime) -> list[sqlite3.Row]:
    marked = {row[0] for row in db.execute("SELECT task_id FROM video_retention")} if marker_exists(db) else set()
    rows = db.execute("SELECT id, stored_path, status, created_at FROM tasks "
                      "WHERE status IN ('done','failed','review') ORDER BY created_at,id").fetchall()
    return [row for row in rows if row["id"] not in marked and older_than(row["created_at"], cutoff)]


def checked_object(uri: str, task_id: str, proxy: bool) -> tuple[str, str]:
    bucket, key = storage._object_parts(uri)
    if proxy:
        prefix = "/".join(p for p in (config.OBJECT_STORAGE_PREFIX, "recognition") if p) + "/"
        filename = key.removeprefix(prefix)
        if not (bucket == config.OBJECT_STORAGE_BUCKET and key.startswith(prefix)
                and len(filename) == 68 and filename.endswith(".mp4")):
            raise ValueError("Unexpected recognition proxy object")
        int(filename[:-4], 16)
    elif not (bucket in {config.OBJECT_STORAGE_BUCKET, config.OBJECT_STORAGE_LEGACY_BUCKET}
              and bucket and (key == task_id + ".mp4" or key.endswith("/" + task_id + ".mp4"))):
        raise ValueError("Unexpected source video object")
    return bucket, key


def plan(row: sqlite3.Row) -> dict:
    task_id, uri = row["id"], row["stored_path"]
    if not isinstance(task_id, str) or not task_id or not isinstance(uri, str) or not uri:
        raise ValueError("Invalid task media reference")
    objects, local, metadata = [], [], []
    if storage.is_object_uri(uri):
        checked_object(uri, task_id, False)
        objects.append(uri)
    else:
        path = Path(uri).resolve()
        if not path.is_relative_to(config.UPLOAD_DIR.resolve()) or path.name != task_id + ".mp4":
            raise ValueError("Unexpected local source video")
        local.append(path)
    root = (config.DATA_DIR / "recognition-cache").resolve()
    directory = recognition_cache.cache_directory(task_id)
    if not directory.resolve().is_relative_to(root):
        raise ValueError("Unexpected cache directory")
    for media_dir in recognition_cache._media_directories(directory):
        if not media_dir.resolve().is_relative_to(root):
            raise ValueError("Unexpected cache segment")
        proxy_file = media_dir / "proxy.mp4"
        if proxy_file.exists() or proxy_file.is_symlink():
            local.append(proxy_file)
        for name in ("proxy.json", "delivery.json"):
            file = media_dir / name
            if file.exists():
                if not file.resolve().is_relative_to(root):
                    raise ValueError("Unexpected proxy metadata")
                metadata.append(file)
                if name == "delivery.json":
                    proxy_uri = json.loads(file.read_text(encoding="utf-8"))["data"]["uri"]
                    checked_object(proxy_uri, task_id, True)
                    objects.append(proxy_uri)
    return {"id": task_id, "uri": uri, "created_at": row["created_at"],
            "objects": tuple(objects), "local": tuple(local), "metadata": tuple(metadata)}


def protected_proxies(db: sqlite3.Connection, eligible_ids: set[str]) -> set[str]:
    """Keep content-addressed proxy videos still referenced by newer/active tasks."""
    protected = set()
    for row in db.execute("SELECT id FROM tasks"):
        if row[0] in eligible_ids:
            continue
        directory = recognition_cache.cache_directory(row[0])
        for media_dir in recognition_cache._media_directories(directory):
            file = media_dir / "delivery.json"
            if file.exists():
                try:
                    protected.add(json.loads(file.read_text(encoding="utf-8"))["data"]["uri"])
                except (OSError, ValueError, KeyError, TypeError):
                    raise ValueError("Cannot audit a retained task proxy") from None
    return protected


def bulk_delete(plans: list[dict], protected: set[str] | None = None) -> set[str]:
    protected = protected or set()
    grouped = defaultdict(lambda: defaultdict(set))
    for item in plans:
        for uri in item["objects"]:
            if uri in protected:
                continue
            bucket, key = storage._object_parts(uri)
            grouped[bucket][key].add(uri)
    deleted = set()
    for bucket, keys in grouped.items():
        names = list(keys)
        for start in range(0, len(names), 1000):
            batch = names[start:start + 1000]
            try:
                result = storage._client().delete_objects(
                    Bucket=bucket, Delete={"Objects": [{"Key": name} for name in batch], "Quiet": False})
                confirmed = {entry["Key"] for entry in result.get("Deleted", [])}
                errors = {entry["Key"] for entry in result.get("Error", [])}
                if not confirmed <= set(batch) or confirmed & errors:
                    continue
                for name in confirmed:
                    deleted.update(keys[name])
            except Exception:
                continue
    return deleted


def still_eligible(db: sqlite3.Connection, item: dict, cutoff: datetime) -> bool:
    row = db.execute("SELECT stored_path,status,created_at FROM tasks WHERE id=?", (item["id"],)).fetchone()
    return bool(row and row["stored_path"] == item["uri"] and row["created_at"] == item["created_at"]
                and row["status"] in TERMINAL and older_than(row["created_at"], cutoff)
                and not db.execute("SELECT 1 FROM video_retention WHERE task_id=?", (item["id"],)).fetchone())


def remove_orphans(db: sqlite3.Connection, cutoff: datetime) -> int:
    uploads = config.UPLOAD_DIR.resolve()
    referenced = {Path(row[0]).resolve() for row in db.execute(
        "SELECT stored_path FROM tasks WHERE stored_path NOT LIKE 's3://%'"
    ) if row[0]}
    removed = 0
    for path in uploads.glob("*.mp4") if uploads.exists() else ():
        if path.is_symlink() or path.resolve() in referenced:
            continue
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
            path.unlink()
            removed += 1
    return removed


def run(db: sqlite3.Connection, cutoff: datetime) -> dict[str, int]:
    db.execute("CREATE TABLE IF NOT EXISTS video_retention (task_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL)")
    db.commit()
    rows = candidates(db, cutoff)
    protected = protected_proxies(db, {row["id"] for row in rows})
    result = Counter(eligible=len(rows), deleted=0, failed=0, orphanLocal=0)
    for start in range(0, len(rows), BATCH_SIZE):
        plans = []
        for row in rows[start:start + BATCH_SIZE]:
            try:
                plans.append(plan(row))
            except (OSError, ValueError, KeyError, TypeError):
                result["failed"] += 1
        db.execute("BEGIN IMMEDIATE")
        try:
            plans = [item for item in plans if still_eligible(db, item, cutoff)]
            confirmed = bulk_delete(plans, protected)
            for item in plans:
                if not set(item["objects"]) - protected <= confirmed:
                    result["failed"] += 1
                    continue
                try:
                    for path in (*item["local"], *item["metadata"]):
                        path.unlink(missing_ok=True)
                    db.execute("INSERT INTO video_retention(task_id,deleted_at) VALUES (?,?)",
                               (item["id"], datetime.now(timezone.utc).isoformat()))
                    result["deleted"] += 1
                except (OSError, sqlite3.Error):
                    result["failed"] += 1
            db.commit()
        except BaseException:
            db.rollback()
            raise
        print("VIDEO_RETENTION_PROGRESS", min(start + BATCH_SIZE, len(rows)), len(rows), flush=True)
    result["orphanLocal"] = remove_orphans(db, cutoff)
    return dict(result)


def audit(db: sqlite3.Connection, cutoff: datetime) -> dict[str, int]:
    rows = candidates(db, cutoff)
    protected = protected_proxies(db, {row["id"] for row in rows})
    plans, invalid = [], 0
    for row in rows:
        try:
            plans.append(plan(row))
        except (OSError, ValueError, KeyError, TypeError):
            invalid += 1
    return {"eligible": len(rows), "valid": len(plans), "invalid": invalid,
            "objectOriginals": sum(storage.is_object_uri(item["uri"]) for item in plans),
            "localOriginals": sum(not storage.is_object_uri(item["uri"]) for item in plans),
            "proxyObjects": sum(len(item["objects"]) - storage.is_object_uri(item["uri"]) for item in plans),
            "sharedProxyObjectsRetained": len(protected & {
                uri for item in plans for uri in item["objects"] if uri != item["uri"]})}


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["audit"], ["run"]):
        raise SystemExit("Usage: video_retention.py audit|run")
    mode = args[0] if args else "run"
    days = int(os.getenv("JBB_RETENTION_DAYS", "14"))
    cutoff = cutoff_time(days)
    with sqlite3.connect(config.DB_PATH, timeout=30) as db:
        db.row_factory = sqlite3.Row
        if mode == "audit":
            print("VIDEO_RETENTION_AUDIT", json.dumps({"environment": config.JBB_ENVIRONMENT,
                  "retentionDays": days, **audit(db, cutoff)}, sort_keys=True), flush=True)
            return
        import fcntl
        with (config.DATA_DIR / "video-retention.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run(db, cutoff)
        print("VIDEO_RETENTION_RUN", json.dumps({"environment": config.JBB_ENVIRONMENT,
              "retentionDays": days, **result}, sort_keys=True), flush=True)
        if result["failed"]:
            raise RuntimeError("Video deletion incomplete; failed media remain eligible for retry")


if __name__ == "__main__":
    main()
