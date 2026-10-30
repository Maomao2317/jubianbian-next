"""Owner review uses real SQLite transactions without recognition or paid calls."""
import asyncio
import gc
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from app import auth_store, routes, task_store
from app.rechecks import create_recheck_snapshot


class JsonRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class UserReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(auth_store, "DB_PATH", Path(self.temp.name) / "review.sqlite3")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.user_patch = patch.object(routes, "current_user", return_value={"id": "owner", "role": "user"})
        self.user_patch.start()
        self.addCleanup(self.user_patch.stop)
        self.addCleanup(gc.collect)  # sqlite contexts commit; GC closes handles before Windows cleanup.
        auth_store.init_db()
        self.script = {"title": "测试", "scenes": [{"heading": "1-1 日 内 客厅",
                       "blocks": [{"type": "dialogue", "speaker": "林长风", "text": "我们出发吧。"}]}]}
        self.issue = {"severity": "P0", "tag": "coverage", "description": "请核对完整性"}
        self.add_task("one")
        self.add_task("two", index=2)

    def add_task(self, task_id, *, user="owner", status="review", script=None, index=1):
        with auth_store.db() as c:
            c.execute("""INSERT INTO tasks (id, user_id, title, file_name, stored_path, status, stage,
                credits_used, result_json, quality_json, created_at, updated_at, batch_id, batch_index)
                VALUES (?, ?, ?, ?, '', ?, ?, 2, ?, ?, ?, ?, 'batch', ?)""",
                (task_id, user, task_id, f"{index}.mp4", status, status,
                 json.dumps(self.script if script is None else script),
                 json.dumps({"deliveryStatus": "review_required", "issues": [self.issue],
                             "recheck": {"adminApprovalRequired": True}}), auth_store.now_iso(), auth_store.now_iso(), index))

    def row(self, task_id):
        with auth_store.db() as c:
            return c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def approve(self, ids):
        return asyncio.run(routes.approve_tasks_batch(JsonRequest({"taskIds": ids})))

    def test_single_review_unlocks_download_and_preserves_findings_and_credits(self):
        with self.assertRaises(HTTPException) as caught:
            routes.download_task(None, "one", "md")
        self.assertEqual(caught.exception.status_code, 409)
        asyncio.run(routes.approve_task(None, "one"))
        row = self.row("one")
        self.assertEqual((row["status"], row["stage"], row["progress_percent"], row["credits_used"]), ("done", "done", 100, 2))
        quality = json.loads(row["quality_json"])
        self.assertEqual(quality["issues"], [self.issue])
        self.assertEqual((quality["approvedBy"], quality["approvalRole"]), ("owner", "user"))
        self.assertFalse(quality["recheck"]["adminApprovalRequired"])
        for fmt in ("md", "txt"):
            self.assertIn("我们出发吧", routes.download_task(None, "one", fmt).body.decode())

    def test_batch_is_idempotent_and_resolves_existing_snapshot(self):
        with auth_store.db() as c:
            create_recheck_snapshot(c, self.row("one"))
        self.assertEqual(self.approve(["one", "two", "one"])["approvedCount"], 2)
        self.assertEqual(self.approve(["one", "two"])["alreadyApprovedCount"], 2)
        with auth_store.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM task_events WHERE event_type='user_approved'").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT state FROM task_recheck_snapshots").fetchone()[0], "approved")
            self.assertEqual(c.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)
        self.assertIn("我们出发吧", routes.download_batch(None, "batch", "txt").body.decode())

    def test_cross_owner_or_missing_task_rolls_back_whole_batch(self):
        self.add_task("foreign", user="other")
        for task_id in ("foreign", "missing"):
            with self.assertRaises(HTTPException) as caught:
                self.approve(["one", task_id])
            self.assertEqual(caught.exception.status_code, 404)
            self.assertEqual(self.row("one")["status"], "review")
        with patch.object(routes, "current_user", return_value={"id": "admin", "role": "admin"}):
            with self.assertRaises(HTTPException):
                asyncio.run(routes.approve_task(None, "one"))

    def test_empty_heading_and_hidden_body_block_batch_atomically(self):
        for blocks in ([], [{"type": "screen_text", "text": "第2集"}],
                       [{"type": "action", "text": "开门", "suppressInOutput": True}]):
            with auth_store.db() as c:
                c.execute("UPDATE tasks SET result_json=? WHERE id='two'",
                          (json.dumps({"scenes": [{"blocks": blocks}]}),))
            with self.assertRaises(HTTPException) as caught:
                self.approve(["one", "two"])
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(self.row("one")["status"], "review")
            self.assertEqual(task_store.task_events("one"), [])

    def test_wrong_state_and_invalid_input_do_not_change_any_task(self):
        for status in ("running", "failed", "queued", "done"):
            with auth_store.db() as c:
                c.execute("UPDATE tasks SET status=? WHERE id='two'", (status,))
            with self.assertRaises(HTTPException) as caught:
                self.approve(["one", "two"])
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(self.row("one")["status"], "review")
        for payload in ({}, [], {"taskIds": []}, {"taskIds": [None]}, {"taskIds": [" "]}, {"taskIds": ["one"] * 1000}):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(routes.approve_tasks_batch(JsonRequest(payload)))
            self.assertEqual(caught.exception.status_code, 400)

    def test_admin_detail_lists_all_owned_batch_episodes_for_every_status(self):
        self.add_task("foreign", user="other")
        with patch.object(routes, "current_user", return_value={"id": "admin", "role": "admin"}):
            for status in ("review", "done", "failed", "running", "queued"):
                with auth_store.db() as c:
                    c.execute("UPDATE tasks SET status=? WHERE id='one'", (status,))
                detail = routes.admin_task_detail(None, "one")
                self.assertEqual(detail["status"], status)
                self.assertEqual([episode["id"] for episode in detail["episodes"]], ["one", "two"])
                self.assertTrue(detail["result"])
        with self.assertRaises(HTTPException) as caught:
            routes.admin_task_detail(None, "one")
        self.assertEqual(caught.exception.status_code, 403)

    def test_owner_filters_separate_review_from_running(self):
        self.add_task("active", status="running")
        self.assertEqual([task["id"] for task in routes.list_tasks(None, status="running")], ["active"])
        self.assertEqual({task["id"] for task in routes.list_tasks(None, status="review")}, {"one", "two"})

    def test_customer_copy_hides_internal_diagnostics_and_admin_retains_them(self):
        issue = {"severity": "P0", "tag": "疑似漏台词片段", "description": "独立 ASR OCR 模型覆盖率 0%",
                 "startSec": 32.1, "endSec": 38, "scene": 1, "block": 2}
        with auth_store.db() as c:
            c.execute("""UPDATE tasks SET quality_json=?, provider='ark', model='internal-model',
                       input_tokens=100, output_tokens=20, total_tokens=120, api_cost_rmb=0.2 WHERE id='one'""",
                      (json.dumps({"issues": [issue], "blockingIssues": [issue], "evidenceSources": {"ASR": "vendor"},
                                   "providerUsage": {"calls": [{"model": "secret-lite", "account": "account-1",
                                                                  "total_tokens": 120, "api_cost_rmb": 0.2}]}}),))
        task_store.record_task_event("one", "review_required", "独立 OCR ASR 模型覆盖率 0%", status="review", stage="review")
        detail = routes.task_detail(None, "one")
        self.assertEqual(detail["status"], "review")
        self.assertEqual(len(detail["quality"]["issues"]), 1)
        self.assertIn("00:32–00:38", detail["quality"]["issues"][0]["description"])
        self.assertIn("第 1 场第 2 段", detail["quality"]["issues"][0]["description"])
        self.assertIn("对白可能有遗漏", detail["quality"]["issues"][0]["description"])
        for data in (detail, routes.list_tasks(None), routes.task_event_list(None, "one", 100)):
            text = json.dumps(data, ensure_ascii=False)
            for internal in (
                "ASR", "OCR", "internal-model", "secret-lite", "account-1",
                "api_cost_rmb", "apiCostRmb", "total_tokens", "gross_profit_rmb",
                "gross_margin", "Lite", "Turbo", "覆盖率", "P0", "vendor",
            ):
                self.assertNotIn(internal, text)
        with patch.object(routes, "current_user", return_value={"id": "admin", "role": "admin"}):
            admin = routes.admin_task_detail(None, "one")
            self.assertEqual(admin["quality"]["issues"], [issue])
            self.assertEqual(admin["model"], "internal-model")
            self.assertIn("独立 OCR", admin["events"][0]["message"])
        with self.assertRaises(HTTPException):
            routes.download_task(None, "one", "md")
        self.approve(["one"])
        self.assertIn("我们出发吧", routes.download_task(None, "one", "md").body.decode())

    def test_quality_blocked_candidate_is_admin_only(self):
        with auth_store.db() as c:
            c.execute(
                "UPDATE tasks SET status='failed', stage='quality_blocked' WHERE id='one'"
            )
        task_store.record_task_event(
            "one", "quality_blocked", "internal quality diagnosis",
            status="quality_blocked", stage="quality_blocked",
        )
        detail = routes.task_detail(None, "one")
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["stage"], "failed")
        self.assertIsNone(detail["result"])
        self.assertFalse(detail["reviewAvailable"])
        self.assertEqual(detail["quality"], {"issues": []})
        self.assertNotIn("quality_blocked", json.dumps(detail, ensure_ascii=False))
        public_events = routes.task_event_list(None, "one", 100)
        self.assertNotIn("quality_blocked", json.dumps(public_events, ensure_ascii=False))
        self.assertEqual(public_events[0]["status"], "failed")
        self.assertEqual(public_events[0]["stage"], "failed")
        with patch.object(routes, "current_user", return_value={"id": "admin", "role": "admin"}):
            admin = routes.admin_task_detail(None, "one")
        self.assertEqual(admin["status"], "failed")
        self.assertEqual(admin["stage"], "quality_blocked")
        self.assertTrue(admin["result"])

    def test_unknown_issue_never_echoes_raw_text_and_body_is_preserved(self):
        from app.user_view import user_issue, user_result
        self.assertNotIn("OCR", str(user_issue({"tag": "new-OCR-provider", "description": "secret-model"})))
        self.assertIn("尚未完成核对", user_issue({"tag": "独立音频转写缺失"})["description"])
        script = {**self.script, "model": "secret", "evidence": {"OCR": "secret"}}
        script["scenes"][0]["blocks"][0]["evidence"] = "ASR"
        public = user_result(script)
        self.assertEqual(public["scenes"][0]["blocks"][0]["text"], "我们出发吧。")
        self.assertNotIn("secret", str(public))
        self.assertNotIn("ASR", str(public))

    def test_admin_minutes_use_net_consumption_without_converting_balance(self):
        with auth_store.db() as c:
            c.execute("INSERT INTO users (id,email,password_hash,name,credits,created_at) VALUES ('owner','test@example.invalid','unused','test',1330,?)", (auth_store.now_iso(),))
        with patch.object(routes, "current_user", return_value={"id": "admin", "role": "admin"}):
            # Legacy accounts with no consumption ledger retain task-minute fallback, including review.
            self.assertEqual(routes.admin_users(None, limit=20, offset=0)["items"][0]["totalUsed"], 4)
            with auth_store.db() as c:
                for amount, kind in ((-25, "consume"), (10, "refund"), (100, "recharge")):
                    c.execute("INSERT INTO credit_ledger (user_id,amount,balance_after,entry_type,created_at) VALUES ('owner',?,1330,?,?)",
                              (amount, kind, auth_store.now_iso()))
                c.execute("DELETE FROM tasks WHERE id='two'")
            user = routes.admin_users(None, keyword="test@", status="active", limit=20, offset=0)["items"][0]
            self.assertEqual(user["credits"], 1330)
            self.assertEqual(user["totalUsed"], 3)
            self.assertEqual(user["taskCount"], 1)
