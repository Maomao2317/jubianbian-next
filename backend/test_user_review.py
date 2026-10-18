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
