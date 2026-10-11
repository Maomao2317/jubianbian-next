#!/usr/bin/env python3
"""Delete old completed/failed tasks and orphaned uploads from the data volume.

Run inside the app container. Active tasks are never removed. The default
retention is 30 days and can be changed with JBB_RETENTION_DAYS.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

backend_dir = Path(__file__).resolve().parents[1] / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.storage import delete_media, is_object_uri


data_dir = Path(os.getenv("JBB_DATA_DIR", "/app/data"))
upload_dir = data_dir / "uploads"
db_path = data_dir / "jubianbian.sqlite3"
retention_days = max(1, int(os.getenv("JBB_RETENTION_DAYS", "30")))
cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

deleted_tasks = 0
deleted_files = 0
orphan_files = 0

if db_path.exists():
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, stored_path
            FROM tasks
            WHERE status IN ('done', 'failed')
              AND COALESCE(completed_at, updated_at, created_at) < ?
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            stored_value = row["stored_path"] or ""
            stored_path = Path(stored_value)
            if is_object_uri(stored_value) or stored_path.is_file():
                try:
                    delete_media(stored_value)
                    deleted_files += 1
                except Exception:
                    # A retention run must still remove the stale DB row when
                    # a remote object was already deleted or is unavailable.
                    pass
            connection.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))
            deleted_tasks += 1
        connection.commit()

        referenced = {
            Path(row[0]).resolve()
            for row in connection.execute("SELECT stored_path FROM tasks").fetchall()
            if row[0] and not is_object_uri(row[0])
        }

    if upload_dir.exists():
        for path in upload_dir.iterdir():
            if not path.is_file() or path.resolve() in referenced:
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified.isoformat() < cutoff:
                path.unlink(missing_ok=True)
                deleted_files += 1
                orphan_files += 1

print(
    json.dumps(
        {
            "retentionDays": retention_days,
            "deletedTasks": deleted_tasks,
            "deletedFiles": deleted_files,
            "deletedOrphanFiles": orphan_files,
        },
        ensure_ascii=False,
    )
)
