"""Purge removes only disabled accounts and their dependent rows."""
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import purge_disabled_accounts as purge


class DisabledAccountPurgeTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        schema = [
            "CREATE TABLE users (id TEXT, email TEXT, role TEXT, is_active INTEGER)",
            "CREATE TABLE tasks (id TEXT, user_id TEXT, stored_path TEXT, status TEXT)",
            "CREATE TABLE task_events (task_id TEXT)",
            "CREATE TABLE task_recheck_snapshots (task_id TEXT)",
            "CREATE TABLE credit_ledger (user_id TEXT)",
            "CREATE TABLE admin_recharge_ledger (user_id TEXT)",
            "CREATE TABLE sessions (user_id TEXT)",
            "CREATE TABLE auth_codes (email TEXT)",
            "CREATE TABLE admin_audit_logs (target_id TEXT, admin_user_id TEXT)",
        ]
        for statement in schema:
            self.connection.execute(statement)
        self.connection.executemany("INSERT INTO users VALUES (?,?,?,?)", [
            ("disabled", "disabled@example.test", "user", 0),
            ("active", "active@example.test", "user", 1),
        ])
        self.connection.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [
            ("old-task", "disabled", "/uploads/old-task.mp4", "done"),
            ("active-task", "active", "/uploads/active-task.mp4", "done"),
        ])
        for table, field, values in [
            ("task_events", "task_id", ["old-task", "active-task"]),
            ("task_recheck_snapshots", "task_id", ["old-task", "active-task"]),
            ("credit_ledger", "user_id", ["disabled", "active"]),
            ("admin_recharge_ledger", "user_id", ["disabled", "active"]),
            ("sessions", "user_id", ["disabled", "active"]),
            ("auth_codes", "email", ["disabled@example.test", "active@example.test"]),
        ]:
            self.connection.executemany(f"INSERT INTO {table} ({field}) VALUES (?)", [(v,) for v in values])
        self.connection.executemany("INSERT INTO admin_audit_logs VALUES (?,?)", [
            ("disabled", "active"), ("active", "active"),
        ])

    def tearDown(self):
        self.connection.close()

    def test_delete_records_keeps_active_user_and_history(self):
        target = purge.snapshot(self.connection)
        self.assertEqual(purge.public_summary(target)["users"], 1)
        purge.delete_records(self.connection, target)
        purge.verify_records(self.connection, target)
        for table in ("users", "tasks", "task_events", "task_recheck_snapshots",
                      "credit_ledger", "admin_recharge_ledger", "sessions", "auth_codes",
                      "admin_audit_logs"):
            self.assertEqual(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)

    def test_scope_rejects_path_outside_uploads_and_active_task(self):
        directory = Path.cwd() / "test-uploads"
        with patch.object(purge.config, "UPLOAD_DIR", directory):
            purge.verify_media_scope([("safe", str(directory / "safe.mp4"), "done")])
            with self.assertRaises(AssertionError):
                purge.verify_media_scope([("safe", "/elsewhere/safe.mp4", "done")])
            with self.assertRaises(AssertionError):
                purge.verify_media_scope([("safe", str(directory / "safe.mp4"), "running")])

    def test_scope_rejects_admin_account(self):
        self.connection.execute("UPDATE users SET role='admin' WHERE id='disabled'")
        with self.assertRaises(AssertionError):
            purge.snapshot(self.connection)


if __name__ == "__main__":
    unittest.main()
