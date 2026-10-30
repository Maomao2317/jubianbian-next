"""Read-only admin summaries and name search, using disposable SQLite data."""
from contextlib import contextmanager
import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app import routes


class AdminConsoleTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE users(id TEXT, email TEXT, name TEXT, credits REAL, is_active INTEGER);
            CREATE TABLE tasks(id TEXT, user_id TEXT, title TEXT, file_name TEXT,
                batch_title TEXT, status TEXT, stage TEXT, progress_percent REAL,
                estimated_minutes REAL, credits_used REAL, error TEXT,
                created_at TEXT, updated_at TEXT, batch_id TEXT, batch_index INTEGER,
                batch_total INTEGER, api_cost_rmb REAL, quality_json TEXT, provider TEXT,
                model TEXT, input_tokens INTEGER, output_tokens INTEGER,
                total_tokens INTEGER, completed_at TEXT);
            INSERT INTO users VALUES ('u', 'user@example.test', 'Example Name', 128.4, 1);
            INSERT INTO users VALUES ('v', 'other@example.test', NULL, 10.2, 0);
        """)
        for index, status in enumerate(("done", "review", "failed", "running", "queued")):
            self.connection.execute("""INSERT INTO tasks
                (id,user_id,title,file_name,batch_title,status,stage,progress_percent,
                 estimated_minutes,credits_used,created_at,updated_at,batch_id,
                 batch_index,batch_total,api_cost_rmb,completed_at)
                VALUES (?, 'u', 'Episode', 'episode.mp4', 'Batch', ?, ?, 50,
                        2, 2, '2026-10-27T01:00:00+00:00', '2026-10-27T01:00:00+00:00',
                        'batch', ?, 5, 0.15, ?)""", (
                            str(index), status, status, index,
                            "2026-10-27T02:00:00+00:00" if status in {"done", "review", "failed"} else None,
                        ))
        calls = {"providerUsage": {"calls": [
            {"provider": "ark", "model": "doubao-seed-2-1-lite-260915", "account": "account-1",
             "status": "success", "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
             "api_cost_rmb": 0.000134, "cost_source": "public_price_estimate"},
            {"provider": "ark", "model": "doubao-seed-2-1-turbo-260628", "account": "account-2",
             "status": "success", "input_tokens": 50, "output_tokens": 10, "total_tokens": 60,
             "api_cost_rmb": 0.0003, "cost_source": "public_price_estimate"},
        ]}}
        self.connection.execute("UPDATE tasks SET quality_json = ? WHERE id = '0'", (json.dumps(calls),))

        @contextmanager
        def db():
            yield self.connection

        for target, value in (("db", db), ("_require_admin", lambda request: {"id": "admin"})):
            item = patch.object(routes, target, value)
            item.start()
            self.addCleanup(item.stop)

    def test_overview_retains_decimal_balance_and_labels_local_cost(self):
        before = self.connection.total_changes
        with patch.object(routes, "_admin_billing_cost", return_value=None):
            value = routes.admin_overview(None)
        self.assertEqual(value["users"], {"total": 2, "active": 1, "credits": 138.6})
        self.assertEqual(value["apiCostSource"], "local_records")
        self.assertEqual(value["apiCostRmb"], 0.3)
        self.assertEqual(value["tasks"]["running"], 1)
        self.assertEqual(value["tasks"]["queued"], 1)
        self.assertEqual(value["recentTasks"][0]["name"], "Example Name")
        self.assertEqual(self.connection.total_changes, before)

    def test_provider_billing_including_zero_has_explicit_month_and_source(self):
        for cost in (0, 12.34):
            with patch.object(routes, "_admin_billing_cost", return_value=cost):
                value = routes.admin_overview(None)
            self.assertEqual(value["apiCostSource"], "provider_billing")
            self.assertEqual(value["apiCostRmb"], cost)
            self.assertRegex(value["apiCostMonth"], r"^\d{4}-\d{2}$")

    def test_task_name_search_keeps_grouping_counts_and_is_read_only(self):
        before = self.connection.total_changes
        for keyword in ("Example Name", "user@example.test", "Episode"):
            value = routes.admin_tasks(None, keyword=keyword, limit=20, offset=0)
            self.assertEqual(value["total"], 1)
            row = value["items"][0]
            self.assertEqual((row["name"], row["email"]), ("Example Name", "user@example.test"))
            self.assertEqual(row["task_count"], 5)
            for key in ("done_count", "review_count", "failed_count", "running_count", "queued_count"):
                self.assertEqual(row[key], 1)
            self.assertEqual(row["credits_used"], 10)
            self.assertEqual(row["points_used"], 50)
            self.assertEqual(row["points_value_rmb"], 3.91)
            self.assertEqual(row["model_usage"]["lite"]["total_tokens"], 120)
            self.assertEqual(row["model_usage"]["turbo"]["total_tokens"], 60)
            self.assertEqual(row["model_usage"]["lite"]["accounts"], ["account-1"])
            self.assertIsNone(row["completed_at"])
        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual(routes.admin_tasks(None, keyword="' OR 1=1 --", limit=20, offset=0)["total"], 0)

    def test_completed_batch_reports_last_completion_time(self):
        value = routes.admin_tasks(None, status="done", limit=20, offset=0)
        self.assertEqual(value["items"][0]["completed_at"], "2026-10-27T02:00:00+00:00")

    def test_admin_authorization_precedes_summary_and_search(self):
        with patch.object(routes, "_require_admin", side_effect=HTTPException(status_code=403)):
            for call in (lambda: routes.admin_overview(None), lambda: routes.admin_tasks(None, limit=20, offset=0)):
                with self.assertRaises(HTTPException) as error:
                    call()
                self.assertEqual(error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
