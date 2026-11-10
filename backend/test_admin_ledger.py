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

    def recharge(self, amount=128, balance=100, reason="test", created="2026-10-27T01:00:00.001+00:00", admin="admin", rmb=None):
        self.connection.execute("INSERT INTO admin_recharge_ledger(user_id, rmb_amount, points_amount, balance_after, admin_user_id, reason, created_at) VALUES ('u', ?, ?, ?, ?, ?, ?)",
                                (amount / 12.8 if rmb is None else rmb, amount, balance, admin, reason, created))

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

    def test_gift_only_recharge_is_reclassified_without_duplicate_or_balance_writes(self):
        self.recharge(reason="赠送")
        self.credit("recharge", 128, reason="赠送")
        before = self.connection.total_changes
        page = self.page()
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["entry_type"], "grant")
        self.assertEqual(page["summary"]["by_type"]["grant"]["amount_rmb"], 10)
        self.assertEqual(page["summary"]["by_type"]["recharge"]["amount_rmb"], 0)
        self.assertEqual(self.page(entry_type="recharge")["total"], 0)
        self.assertEqual(self.page(entry_type="grant")["total"], 1)
        self.assertEqual(self.connection.total_changes, before)

    def test_mixed_bonus_splits_and_filters_both_components_without_double_counting(self):
        self.recharge(amount=139 * 12.8, reason="129套餐加赠送10元")
        page = self.page()
        totals = page["summary"]["by_type"]
        self.assertEqual(totals["recharge"]["amount_rmb"], 129)
        self.assertEqual(totals["grant"]["amount_rmb"], 10)
        self.assertAlmostEqual(sum(i["points_amount"] for i in page["items"]), 139 * 12.8)
        self.assertEqual(self.page(entry_type="grant")["summary"]["by_type"]["recharge"]["amount_rmb"], 0)
        self.assertEqual(self.page(entry_type="recharge")["total"], 1)

    def test_bonus_separate_entry_and_legacy_plus_notes_are_not_counted_twice(self):
        self.recharge(amount=1000 * 12.8, reason="1000套餐赠送200元")
        self.recharge(amount=200 * 12.8, reason="赠送200元")
        self.recharge(amount=1320 * 12.8, reason="1000套餐 + 320")
        self.recharge(amount=60 * 12.8, reason="369赠送60")
        totals = self.page()["summary"]["by_type"]
        self.assertEqual(totals["recharge"]["amount_rmb"], 2000)
        self.assertEqual(totals["grant"]["amount_rmb"], 580)

    def test_gift_first_mixed_note_and_actual_cash_override_package_label(self):
        self.recharge(amount=1200 * 12.8, reason="赠送200元，充值1000元")
        self.recharge(amount=39 * 12.8, rmb=39, reason="129套餐")
        self.recharge(amount=1200 * 12.8, reason="充值1000元，赠送2560积分")
        totals = self.page()["summary"]["by_type"]
        self.assertEqual(totals["recharge"]["amount_rmb"], 2039)
        self.assertEqual(totals["grant"]["amount_rmb"], 400)

    def test_legacy_recharge_without_cash_is_not_invented_and_rounds_after_sum(self):
        self.credit("recharge", 128)
        for _ in range(3):
            self.credit("grant", .1)
        totals = self.page()["summary"]["by_type"]
        self.assertEqual(totals["recharge"]["amount_rmb"], 0)
        self.assertEqual(totals["recharge"]["unrecorded_cash_count"], 1)
        self.assertEqual(totals["grant"]["amount_rmb"], .02)

    def test_totals_cover_all_pages_and_respect_keyword_date_type_and_fractional_last_second(self):
        for i in range(25):
            self.credit("consume", -12.8, created="2026-10-27T23:59:59.999+00:00")
        self.credit("consume", -128, created="2026-10-28T00:00:00+00:00")
        self.credit("adjustment", -25.6)
        self.credit("adjustment", 12.8)
        page = self.page(keyword="example.test", date_to="2026-10-27", entry_type="consume")
        self.assertEqual(len(page["items"]), 10)
        self.assertEqual(page["total"], 25)
        self.assertEqual(page["summary"]["by_type"]["consume"]["amount_rmb"], -25)
        self.assertEqual(self.page(entry_type="adjustment")["summary"]["by_type"]["adjustment"]["amount_rmb"], -1)
        self.assertEqual(self.page(keyword="no match")["summary"]["by_type"]["consume"]["amount_rmb"], 0)

    def test_ambiguous_note_is_flagged_in_other_instead_of_counting_cash(self):
        self.recharge(reason="套餐含赠送，金额待确认")
        page = self.page()
        self.assertEqual(page["summary"]["review_count"], 1)
        self.assertEqual(page["items"][0]["entry_type"], "other")
        self.assertEqual(page["summary"]["by_type"]["recharge"]["amount_rmb"], 0)


if __name__ == "__main__":
    unittest.main()
