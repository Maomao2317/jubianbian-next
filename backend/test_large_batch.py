"""Large-batch control-plane regressions; no paid recognition or real uploads."""

import asyncio
from collections import Counter
from contextlib import contextmanager
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app import routes, task_store, worker


class Request:
    def __init__(self, payload=None):
        self.payload = payload

    async def json(self):
        return self.payload


class LargeBatchTests(unittest.TestCase):
    def database(self):
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY, credits INTEGER);
            INSERT INTO users VALUES ('owner', 100000);
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, user_id TEXT, title TEXT, file_name TEXT,
                stored_path TEXT, file_size INTEGER, mime_type TEXT, duration_sec REAL,
                estimated_minutes INTEGER, credits_used INTEGER, status TEXT, stage TEXT,
                progress_percent INTEGER, error TEXT, created_at TEXT, updated_at TEXT,
                batch_id TEXT, batch_title TEXT, batch_index INTEGER, batch_total INTEGER
            );
            CREATE INDEX idx_tasks_ready_user ON tasks(user_id, created_at, id)
              WHERE status = 'queued' AND stage = 'queued';
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY, task_id TEXT, event_type TEXT, status TEXT,
                stage TEXT, progress_percent INTEGER, message TEXT, duration_ms REAL, created_at TEXT
            );
            CREATE TABLE credit_ledger (
                id INTEGER PRIMARY KEY, user_id TEXT, amount INTEGER, balance_after INTEGER,
                entry_type TEXT, reason TEXT, created_at TEXT
            );
        """)
        return connection

    def test_reserve_100_and_999_has_one_write_commit_and_exact_billing(self):
        for count in (100, 999):
            with self.subTest(count=count):
                connection = self.database()
                statements = []
                connection.set_trace_callback(statements.append)

                @contextmanager
                def database():
                    with connection:
                        yield connection

                payload = {"files": [{"name": f"{index}.mp4", "size": 1024, "durationSec": 10} for index in range(count, 0, -1)]}
                with (patch.object(routes, "db", database),
                      patch.object(routes, "current_user", return_value={"id": "owner", "credits": 100000}),
                      patch.object(routes, "object_storage_enabled", return_value=False),
                      patch.object(task_store, "db", side_effect=AssertionError("per-episode event connection"))):
                    result = asyncio.run(routes.create_upload_batch(Request(payload)))
                self.assertEqual(result["count"], count)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM task_events").fetchone()[0], count)
                self.assertEqual(sum(statement == "COMMIT" for statement in statements), 1)
                charged = connection.execute("SELECT SUM(credits_used) FROM tasks").fetchone()[0]
                ledger = connection.execute("SELECT -amount FROM credit_ledger").fetchone()[0]
                self.assertEqual(charged * routes.POINTS_PER_MINUTE, ledger)
                self.assertEqual(ledger, ((count * 10 + 59) // 60) * routes.POINTS_PER_MINUTE)
                rows = connection.execute("SELECT file_name FROM tasks ORDER BY batch_index").fetchall()
                self.assertEqual([row[0] for row in rows], [f"{index}.mp4" for index in range(1, count + 1)])
                self.assertEqual(result["uploads"][0]["inputIndex"], count - 1)

    def test_queue_head_query_preserves_fifo_fairness_and_null_owners(self):
        connection = self.database()
        connection.executemany(
            "INSERT INTO tasks(id,user_id,status,stage,created_at) VALUES (?,?,'queued','queued',?)",
            [(f"a-{index:04}", "a", f"{index:04}") for index in range(999)]
            + [(f"b-{index:04}", "b", f"{index:04}") for index in range(100)]
            + [("n-1", None, "1"), ("n-0", None, "0")],
        )
        connection.commit()
        with connection:
            self.assertEqual(worker._claim_next_task(connection, Counter({"a": 8, "b": 7, "": 9}), "a"), ("b-0000", "b"))
        with connection:
            self.assertEqual(worker._claim_next_task(connection, Counter({"a": 8, "b": 8, "": 9}), "b"), ("a-0000", "a"))
        with connection:
            self.assertEqual(worker._claim_next_task(connection, Counter({"a": 9, "b": 9}), "b"), ("n-0", ""))

    def test_same_batch_episodes_remain_eligible_for_parallel_workers(self):
        connection = self.database()
        connection.executemany(
            "INSERT INTO tasks(id,user_id,status,stage,created_at,batch_id,batch_index) VALUES (?, 'owner', 'queued', 'queued', ?, 'batch', ?)",
            [(f"episode-{index}", f"{index:04}", index) for index in range(1, 5)],
        )
        connection.commit()

        claimed = []
        active = Counter()
        last_user = None
        for _ in range(4):
            with connection:
                task_id, last_user = worker._claim_next_task(connection, active, last_user)
            claimed.append(task_id)
            active[last_user] += 1

        self.assertEqual(claimed, ["episode-1", "episode-2", "episode-3", "episode-4"])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0], 4)

    def test_accepted_upload_is_idempotent_after_worker_claim_and_completion(self):
        for status, stage in [("queued", "queued"), ("running", "vision"), ("done", "done"), ("review", "review")]:
            row = {"status": status, "stage": stage}
            with (patch.object(routes, "current_user", return_value={"id": "owner"}),
                  patch.object(routes, "task_row", return_value=row),
                  patch.object(routes, "media_info", side_effect=AssertionError("must not re-read media"))):
                result = asyncio.run(routes.complete_task_upload(Request(), "task"))
                self.assertEqual(result["status"], status)
                result = asyncio.run(routes.upload_task_media(Request(), "task"))
                self.assertEqual(result["status"], status)

    def test_completion_is_conditional_and_does_not_revive_an_aborted_upload(self):
        connection = self.database()
        connection.execute("INSERT INTO tasks(id,user_id,status,stage,stored_path,file_size) VALUES ('task','owner','queued','uploading','object',1024)")
        connection.commit()

        @contextmanager
        def database():
            with connection:
                yield connection

        initial = {"status": "queued", "stage": "uploading", "stored_path": "object", "file_size": 1024}

        def media_info(_):
            connection.execute("UPDATE tasks SET status='failed',stage='failed' WHERE id='task'")
            connection.commit()
            return {"size": 1024}

        with (patch.object(routes, "db", database),
              patch.object(routes, "current_user", return_value={"id": "owner"}),
              patch.object(routes, "task_row", side_effect=[initial, {"status": "failed", "stage": "failed"}]),
              patch.object(routes, "is_object_uri", return_value=True),
              patch.object(routes, "media_info", side_effect=media_info)):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(routes.complete_task_upload(Request(), "task"))
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(connection.execute("SELECT stage FROM tasks WHERE id='task'").fetchone()[0], "failed")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM task_events").fetchone()[0], 0)

    def test_complete_transitions_once_and_records_one_event(self):
        connection = self.database()
        connection.execute("INSERT INTO tasks(id,user_id,status,stage,stored_path,file_size) VALUES ('task','owner','queued','uploading','object',1024)")
        connection.commit()

        @contextmanager
        def database():
            with connection:
                yield connection

        def row(*_):
            return connection.execute("SELECT * FROM tasks WHERE id='task'").fetchone()

        with (patch.object(routes, "db", database),
              patch.object(routes, "current_user", return_value={"id": "owner"}),
              patch.object(routes, "task_row", side_effect=row),
              patch.object(routes, "is_object_uri", return_value=True),
              patch.object(routes, "media_info", return_value={"size": 1024}) as info):
            for _ in range(2):
                self.assertEqual(asyncio.run(routes.complete_task_upload(Request(), "task"))["status"], "queued")
            info.assert_called_once()
        self.assertEqual(connection.execute("SELECT stage FROM tasks WHERE id='task'").fetchone()[0], "queued")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM task_events WHERE event_type='uploaded'").fetchone()[0], 1)

    def test_only_temporarily_incomplete_objects_have_retry_after(self):
        for info in (None, {"size": 12}):
            with (patch.object(routes, "current_user", return_value={"id": "owner"}),
                  patch.object(routes, "task_row", return_value={"status": "queued", "stage": "uploading", "stored_path": "object", "file_size": 1024}),
                  patch.object(routes, "is_object_uri", return_value=True),
                  patch.object(routes, "media_info", return_value=info)):
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(routes.complete_task_upload(Request(), "task"))
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(caught.exception.headers, {"Retry-After": "1"})
        with (patch.object(routes, "current_user", return_value={"id": "owner"}),
              patch.object(routes, "task_row", return_value={"status": "failed", "stage": "failed"})):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(routes.complete_task_upload(Request(), "task"))
            self.assertEqual(caught.exception.status_code, 409)
            self.assertFalse(caught.exception.headers)


if __name__ == "__main__":
    unittest.main()
