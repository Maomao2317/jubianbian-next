"""Read-only ledger presentation: recharge deduplication, types and pagination."""
from contextlib import contextmanager
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app import routes


class AdminLedgerTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE users(id TEXT, email TEXT, name TEXT, credits REAL);
            CREATE TABLE credit_ledger(id INTEGER PRIMARY KEY, user_id TEXT,
                amount REAL, balance_after REAL, entry_type TEXT, reason TEXT,
                admin_user_id TEXT, created_at TEXT);
            CREATE TABLE admin_recharge_ledger(id INTEGER PRIMARY KEY, user_id TEXT,
                rmb_amount REAL, points_amount REAL, balance_after REAL,
                admin_user_id TEXT, reason TEXT, created_at TEXT);
            INSERT INTO users VALUES ('u', 'user@example.test', 'Test', 100);
        """)

        @contextmanager
        def db():
            yield self.connection

        self.db_patch = patch.object(routes, "db", db)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.admin_patch = patch.object(routes, "_require_admin", return_value={"id": "admin"})
        self.admin = self.admin_patch.start()
        self.addCleanup(self.admin_patch.stop)

    def credit(self, kind, amount, balance=100, reason="test", created="2026-10-27T01:00:00+00:00", admin="admin"):
        self.connection.execute("INSERT INTO credit_ledger(user_id, amount, balance_after, entry_type, reason, admin_user_id, created_at) VALUES ('u', ?, ?, ?, ?, ?, ?)",
                                (amount, balance, kind, reason, admin, created))

    def recharge(self, amount=128, balance=100, reason="test", created="2026-10-27T01:00:00.001+00:00", admin="admin"):
        self.connection.execute("INSERT INTO admin_recharge_ledger(user_id, rmb_amount, points_amount, balance_after, admin_user_id, reason, created_at) VALUES ('u', 10, ?, ?, ?, ?, ?)",
                                (amount, balance, admin, reason, created))

    def page(self, **filters):
        return routes.admin_recharge_ledger(None, limit=10, offset=0, **filters)

    def test_paired_recharge_is_one_row_and_does_not_modify_ledgers_or_balance(self):
        self.credit("recharge", 128)
        self.recharge()
        before = self.connection.total_changes
        page = self.page()
        self.assertEqual(page["total"], 1)
        self.assertEqual((page["items"][0]["rmb_amount"], page["items"][0]["points_amount"]), (10, 128))
        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual(self.connection.execute("SELECT credits FROM users").fetchone()[0], 100)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 1)
        self.admin.assert_called_once()

    def test_legacy_and_distinct_recharges_are_preserved(self):
        self.recharge()
        for override in ({"admin": "another"}, {"balance": 200}, {"reason": "different"},
                         {"created": "2026-10-27T01:00:03+00:00"}):
            self.credit("recharge", 128, **override)
        page = self.page(entry_type="recharge")
        self.assertEqual(page["total"], 5)
        self.assertEqual(sum(item["rmb_amount"] is None for item in page["items"]), 4)

    def test_pair_across_second_and_midnight_boundary(self):
        self.credit("recharge", 128, created="2026-10-26T23:59:59.999+00:00")
        self.recharge(created="2026-10-27T00:00:00.001+00:00")
        self.assertEqual(self.page()["total"], 1)

    def test_refund_consume_grant_and_adjustment_are_separate_even_if_values_match(self):
        self.recharge()
        for kind, amount in (("refund", 128), ("consume", -10), ("grant", 25), ("adjustment", -2), ("legacy", 1)):
            self.credit(kind, amount)
        for kind, amount in (("refund", 128), ("consume", -10), ("grant", 25), ("adjustment", -2), ("other", 1)):
            page = self.page(entry_type=kind)
            self.assertEqual(page["total"], 1)
            self.assertEqual(page["items"][0]["points_amount"], amount)
            self.assertIsNone(page["items"][0]["rmb_amount"])

    def test_filters_and_pagination_use_deduplicated_rows(self):
        self.credit("recharge", 128)
        self.recharge()
        self.credit("consume", -10, created="2026-10-27T02:00:00+00:00")
        self.credit("refund", 10, created="2026-10-28T01:00:00+00:00")
        page = self.page(keyword="user@example.test", date_from="2026-10-27", date_to="2026-10-27")
        self.assertEqual(page["total"], 2)
        self.assertEqual([item["entry_type"] for item in page["items"]], ["consume", "recharge"])
        page = routes.admin_recharge_ledger(None, limit=1, offset=1)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["items"][0]["entry_type"], "consume")
        with self.assertRaises(HTTPException) as error:
            self.page(entry_type="invalid")
        self.assertEqual(error.exception.status_code, 400)

    def test_requires_admin_before_reading_or_validating_filters(self):
        self.admin.side_effect = HTTPException(status_code=403)
        with self.assertRaises(HTTPException) as error:
            self.page()
        self.assertEqual(error.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
