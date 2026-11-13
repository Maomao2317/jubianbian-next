from __future__ import annotations

import json
import sqlite3
import unittest

from app.rechecks import (
    create_recheck_snapshot,
    latest_pending_recheck,
    mark_recheck_resolved,
    restore_recheck_snapshot_with_connection,
)


class RecheckSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT,
                stage TEXT,
                progress_percent INTEGER,
                error TEXT,
                result_json TEXT,
                quality_json TEXT,
                evidence_json TEXT,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                api_cost_rmb REAL,
                attempts INTEGER,
                completed_at TEXT,
                updated_at TEXT
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE task_recheck_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        self.connection.execute(
            """
            INSERT INTO tasks (
                id, status, stage, progress_percent, result_json, quality_json,
                evidence_json, provider, model, input_tokens, output_tokens,
                total_tokens, api_cost_rmb, attempts, completed_at, updated_at
            ) VALUES (?, 'done', 'done', 100, ?, ?, ?, 'ark', 'lite', 1, 2, 3, 0.4, 1, ?, ?)
            """,
            (
                "task-1",
                json.dumps({"version": "delivered"}),
                json.dumps({"deliveryStatus": "approved"}),
                json.dumps({"status": "available"}),
                "2026-10-10T01:00:00+00:00",
                "2026-10-10T01:00:00+00:00",
            ),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_restore_returns_delivered_version_after_candidate_review(self) -> None:
        row = self.connection.execute("SELECT * FROM tasks WHERE id = 'task-1'").fetchone()
        create_recheck_snapshot(self.connection, row)
        self.connection.execute(
            """
            UPDATE tasks
            SET status = 'review', stage = 'review', result_json = ?, quality_json = ?
            WHERE id = 'task-1'
            """,
            (json.dumps({"version": "candidate"}), json.dumps({"deliveryStatus": "review_required"})),
        )

        restored = restore_recheck_snapshot_with_connection(self.connection, "task-1")

        self.assertTrue(restored)
        task = self.connection.execute("SELECT * FROM tasks WHERE id = 'task-1'").fetchone()
        self.assertEqual(task["status"], "done")
        self.assertEqual(json.loads(task["result_json"])["version"], "delivered")
        snapshot = self.connection.execute(
            "SELECT state, resolved_at FROM task_recheck_snapshots WHERE task_id = 'task-1'"
        ).fetchone()
        self.assertEqual(snapshot["state"], "restored")
        self.assertTrue(snapshot["resolved_at"])

    def test_approval_keeps_snapshot_as_history(self) -> None:
        row = self.connection.execute("SELECT * FROM tasks WHERE id = 'task-1'").fetchone()
        create_recheck_snapshot(self.connection, row)

        self.assertIsNotNone(latest_pending_recheck(self.connection, "task-1"))
        self.assertTrue(mark_recheck_resolved(self.connection, "task-1", "approved"))
        self.assertIsNone(latest_pending_recheck(self.connection, "task-1"))
        state = self.connection.execute(
            "SELECT state FROM task_recheck_snapshots WHERE task_id = 'task-1'"
        ).fetchone()["state"]
        self.assertEqual(state, "approved")


if __name__ == "__main__":
    unittest.main()
