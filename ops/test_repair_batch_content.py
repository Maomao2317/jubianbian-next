import json
import asyncio
import sqlite3
import unittest
from unittest.mock import patch, AsyncMock

import repair_batch_content as repair


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE tasks(id TEXT PRIMARY KEY, updated_at TEXT, result_json TEXT,
                quality_json TEXT, status TEXT, stage TEXT, progress_percent INTEGER,
                error TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
                total_tokens INTEGER, api_cost_rmb REAL, credits_used INTEGER);
            CREATE TABLE task_recheck_snapshots(id INTEGER PRIMARY KEY, task_id TEXT,
                snapshot_json TEXT, state TEXT, created_at TEXT, resolved_at TEXT);
        """)
        self.before = {"scenes": [{"blocks": [{"type": "dialogue", "speaker": "甲", "text": "原话"}]}]}
        self.connection.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("task", "before", json.dumps(self.before), "{}", "done", "done", 100,
             None, "lite", 10, 2, 12, .1, 9))
        self.connection.commit()
        self.row = self.connection.execute("SELECT * FROM tasks").fetchone()
        self.quality = {"contentRepair": {"model": "turbo"}}

    def tearDown(self):
        self.connection.close()

    def test_candidate_has_snapshot_retains_credit_charge_and_accumulates_usage(self):
        with patch.object(repair, "db", return_value=self.connection):
            repair.save_candidate(self.row, self.before, self.before, self.quality, {"total_tokens": 5})
        row = self.connection.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual((row["status"], row["credits_used"], row["total_tokens"]), ("review", 9, 17))
        snapshot = self.connection.execute("SELECT * FROM task_recheck_snapshots").fetchone()
        self.assertEqual(json.loads(snapshot["snapshot_json"])["result_json"], self.row["result_json"])

    def test_concurrent_user_edit_prevents_overwrite(self):
        self.connection.execute("UPDATE tasks SET updated_at='user-edit'")
        self.connection.commit()
        with patch.object(repair, "db", return_value=self.connection), self.assertRaises(ValueError):
            repair.save_candidate(self.row, self.before, self.before, self.quality, {})
        self.assertEqual(self.connection.execute("SELECT count(*) FROM task_recheck_snapshots").fetchone()[0], 0)

    def test_lost_original_line_cannot_be_published(self):
        with self.assertRaises(ValueError):
            repair.save_candidate(self.row, self.before, {"scenes": []}, self.quality, {})


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_stops_unstarted_tasks(self):
        worker = AsyncMock(return_value=True)
        with patch.object(repair, "repair_one", worker):
            await repair.run_targets([{"id": str(i)} for i in range(9)], 3)
        self.assertEqual(worker.await_count, 1)

    async def test_concurrency_bound_and_each_task_runs_once(self):
        active = peak = 0
        seen = []

        async def fake(row, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            seen.append(row["id"])
            await asyncio.sleep(0)
            active -= 1
            return False

        with patch.object(repair, "repair_one", side_effect=fake):
            await repair.run_targets([{"id": str(i)} for i in range(9)], 3)
        self.assertEqual(peak, 3)
        self.assertEqual(sorted(seen), list(map(str, range(9))))


if __name__ == "__main__":
    unittest.main()
