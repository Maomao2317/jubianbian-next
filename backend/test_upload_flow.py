import asyncio
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from app import routes, task_store


class FakeRequest:
    def __init__(self, *, payload=None, body=b""):
        self._payload = payload
        self._body = body
        self.headers = {"content-length": str(len(body))} if body else {}

    async def json(self):
        return self._payload

    async def stream(self):
        if self._body:
            yield self._body


class ParallelUploadFlowTests(unittest.TestCase):
    def test_reserve_then_upload_queues_episode(self):
        database = "file:jbb-upload-flow?mode=memory&cache=shared"
        target_path: Path | None = None

        def open_db():
            connection = sqlite3.connect(database, uri=True)
            connection.row_factory = sqlite3.Row
            return connection

        keeper = open_db()
        try:
            with keeper as connection:
                connection.executescript(
                    """
                    CREATE TABLE users (id TEXT PRIMARY KEY, credits INTEGER NOT NULL);
                    INSERT INTO users(id, credits) VALUES ('user-1', 100);
                    CREATE TABLE tasks (
                        id TEXT PRIMARY KEY, user_id TEXT, title TEXT, file_name TEXT,
                        stored_path TEXT NOT NULL, file_size INTEGER NOT NULL,
                        mime_type TEXT, duration_sec REAL, estimated_minutes INTEGER,
                        credits_used INTEGER, status TEXT, stage TEXT, progress_percent INTEGER,
                        error TEXT, result_json TEXT, quality_json TEXT, evidence_json TEXT,
                        provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
                        total_tokens INTEGER, api_cost_rmb REAL DEFAULT 0, attempts INTEGER DEFAULT 0,
                        created_at TEXT, updated_at TEXT, completed_at TEXT, batch_id TEXT,
                        batch_title TEXT, batch_index INTEGER, batch_total INTEGER
                    );
                    CREATE TABLE credit_ledger (
                        id INTEGER PRIMARY KEY, user_id TEXT, amount INTEGER,
                        balance_after INTEGER, entry_type TEXT, reason TEXT, created_at TEXT
                    );
                    CREATE TABLE task_events (
                        id INTEGER PRIMARY KEY, task_id TEXT, event_type TEXT, status TEXT,
                        stage TEXT, progress_percent INTEGER, message TEXT, duration_ms REAL,
                        created_at TEXT
                    );
                    """
                )

            file_bytes = b"small-test-video"
            payload = {
                "title": "测试短剧",
                "files": [{"name": "第01集.mp4", "size": len(file_bytes), "durationSec": 10}],
            }
            user = {"id": "user-1", "credits": 100}
            target_path = Path(__file__).resolve().parent / "test-upload-flow.mp4"
            with (
                patch.object(routes, "db", open_db),
                patch.object(task_store, "db", open_db),
                patch.object(routes, "current_user", return_value=user),
                patch.object(routes, "object_storage_enabled", return_value=False),
                patch.object(routes, "local_upload_path", return_value=target_path),
                patch.object(routes, "probe_duration", return_value=10),
            ):
                reservation = asyncio.run(routes.create_upload_batch(FakeRequest(payload=payload)))
                target = reservation["uploads"][0]
                task_id = target["taskId"]
                result = asyncio.run(routes.upload_task_media(FakeRequest(body=file_bytes), task_id))

            self.assertEqual(reservation["storage"], "local")
            self.assertEqual(result["status"], "queued")
            with open_db() as connection:
                row = connection.execute("SELECT stage, progress_percent, stored_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
            self.assertEqual((row["stage"], row["progress_percent"]), ("queued", 4))
            self.assertEqual(Path(row["stored_path"]).read_bytes(), file_bytes)
        finally:
            keeper.close()
            if target_path is not None:
                target_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
