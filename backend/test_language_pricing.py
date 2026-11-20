"""Real billing transactions with disposable data and no recognition calls."""
import asyncio
import gc
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, UploadFile
from app import auth_store, processing, routes


class Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class LanguagePricingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, name, value in (
            (auth_store, "DB_PATH", self.root / "pricing.sqlite3"),
            (routes, "UPLOAD_DIR", self.root),
            (routes, "local_upload_path", lambda task_id: self.root / f"{task_id}.mp4"),
            (routes, "current_user", lambda _: {"id": "owner", "credits": self.balance()}),
            (routes, "_require_admin", lambda _: {"id": "owner"}),
            (routes, "object_storage_enabled", lambda: False),
            (routes, "media_exists", lambda _: True),
            (routes, "probe_duration", lambda _: 31),
            (routes, "delete_media", lambda _: None),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(gc.collect)
        auth_store.init_db()
        with auth_store.db() as c:
            c.execute("""INSERT INTO users(id,email,name,password_hash,credits,created_at)
                VALUES ('owner','fixture@example.test','Fixture','unused',1000.4,?)""",
                (auth_store.now_iso(),))

    def balance(self):
        with auth_store.db() as c:
            return c.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0]

    def reserve(self, source="zh", output="en", durations=(31, 31)):
        return asyncio.run(routes.create_upload_batch(Request({
            "sourceLanguage": source, "outputLanguage": output,
            "files": [{"name": f"{index}.mp4", "size": 7, "durationSec": duration}
                      for index, duration in enumerate(durations)],
        })))

    def upload(self):
        return UploadFile(filename="1.mp4", file=BytesIO(b"fixture"))

    def test_all_language_pairs_on_reservation_and_both_multipart_routes(self):
        for source in ("zh", "en"):
            for output in ("zh", "en", "both"):
                rate = 5 if (source, output) == ("zh", "zh") else 8
                for transport in ("reserve", "single", "batch"):
                    with self.subTest(source=source, output=output, transport=transport):
                        before = self.balance()
                        if transport == "reserve":
                            result = self.reserve(source, output)
                            tasks = [routes.task_detail(None, item["taskId"]) for item in result["uploads"]]
                        elif transport == "single":
                            tasks = [asyncio.run(routes.create_task(None, BackgroundTasks(), self.upload(),
                                     "Episode", 31, source, output))]
                        else:
                            result = asyncio.run(routes.create_tasks_batch(None, [self.upload(), self.upload()],
                                                "Episode", source, output))
                            tasks = result["tasks"]
                        minutes = 1 if transport == "single" else 2
                        self.assertAlmostEqual(self.balance(), before - minutes * rate)
                        self.assertEqual(sum(t["pointsUsed"] for t in tasks), minutes * rate)
                        self.assertTrue(all(t["pointsPerMinute"] == rate for t in tasks))
                        with auth_store.db() as c:
                            entry = c.execute("SELECT amount,balance_after FROM credit_ledger ORDER BY id DESC").fetchone()
                        self.assertEqual(entry["amount"], -minutes * rate)
                        self.assertAlmostEqual(entry["balance_after"], self.balance())

    def test_batch_rounding_and_abort_refund_once_at_captured_rate(self):
        result = self.reserve(durations=(20, 20, 20))
        self.assertAlmostEqual(self.balance(), 992.4)
        tasks = routes.list_tasks(None)
        self.assertEqual(sum(t["creditsUsed"] for t in tasks), 1)
        self.assertEqual(sum(t["pointsUsed"] for t in tasks), 8)
        for upload in result["uploads"]:
            task_id = upload["taskId"]
            asyncio.run(routes.abort_task_upload(None, task_id))
            processing._refund_task_once(task_id, "owner", 999)
        self.assertAlmostEqual(self.balance(), 1000.4)
        with auth_store.db() as c:
            self.assertEqual(c.execute("SELECT SUM(amount) FROM credit_ledger").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM credit_ledger WHERE entry_type='refund'").fetchone()[0], 1)

    def test_english_insufficient_balance_and_stale_profile_cannot_undercharge(self):
        with auth_store.db() as c:
            c.execute("UPDATE users SET credits=7.4")
        for stale_profile in (False, True):
            with self.subTest(stale_profile=stale_profile):
                with patch.object(routes, "current_user", return_value={"id": "owner", "credits": 1000 if stale_profile else 7.4}):
                    with self.assertRaises(HTTPException) as caught:
                        self.reserve(durations=(60,))
                self.assertEqual(caught.exception.status_code, 402)
                self.assertAlmostEqual(self.balance(), 7.4)
        with auth_store.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 0)
        self.reserve("zh", "zh", (60,))
        self.assertAlmostEqual(self.balance(), 2.4)

    def historic_task(self):
        with auth_store.db() as c:
            c.execute("""INSERT INTO tasks(id,user_id,title,file_name,stored_path,status,stage,
                source_language,output_language,estimated_minutes,credits_used,created_at,updated_at)
                VALUES ('old','owner','Old','old.mp4','old.mp4','failed','failed','en','zh',2,2,?,?)""",
                (auth_store.now_iso(), auth_store.now_iso()))

    def test_migration_keeps_historic_english_rate_and_refunds(self):
        with auth_store.db() as c:
            c.execute("ALTER TABLE tasks DROP COLUMN points_per_minute")
        self.historic_task()
        auth_store.init_db()
        for task in (routes.task_detail(None, "old"), routes.list_tasks(None)[0]):
            self.assertEqual((task["pointsPerMinute"], task["pointsUsed"]), (5, 10))
        processing._refund_task_once("old", "owner", 2)
        processing._refund_task_once("old", "owner", 2)
        self.assertAlmostEqual(self.balance(), 1010.4)

    def test_paid_owner_and_admin_retry_use_new_rate_and_refund_it(self):
        self.historic_task()
        processing._refund_task_once("old", "owner", 2)
        for handler in (routes.retry_task, routes.admin_retry_task):
            with self.subTest(handler=handler.__name__):
                before = self.balance()
                asyncio.run(handler(None, "old", BackgroundTasks()))
                self.assertAlmostEqual(self.balance(), before - 16)
                self.assertEqual(routes.task_detail(None, "old")["pointsUsed"], 16)
                with auth_store.db() as c:
                    c.execute("UPDATE tasks SET status='failed',stage='failed' WHERE id='old'")
                processing._refund_task_once("old", "owner", 2)
                self.assertAlmostEqual(self.balance(), before)

    def test_admin_groups_sum_actual_mixed_rates(self):
        first = self.reserve("zh", "zh", (60,))["uploads"][0]["taskId"]
        second = self.reserve("en", "zh", (60,))["uploads"][0]["taskId"]
        with auth_store.db() as c:
            c.execute("UPDATE tasks SET batch_id='mixed',stage='queued' WHERE id IN (?,?)", (first, second))
        group = routes.admin_tasks(None, limit=20, offset=0)["items"][0]
        self.assertEqual(group["points_used"], 13)
        admin_user = routes.admin_users(None, limit=20, offset=0)["items"][0]
        self.assertAlmostEqual(admin_user["totalUsed"] * 5, 13)


if __name__ == "__main__":
    unittest.main()
