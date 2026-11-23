"""HTTP handlers enqueue jobs; only the durable worker may start recognition."""
import asyncio
from collections import Counter
import gc
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, UploadFile
from app import auth_store, routes, task_store, worker
from test_cast_evidence import episode


class RouteQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, name, value in (
            (auth_store, "DB_PATH", self.root / "queue.sqlite3"),
            (routes, "UPLOAD_DIR", self.root),
            (routes, "current_user", lambda _: {"id": "owner", "role": "admin", "credits": 100}),
            (routes, "media_exists", lambda _: True),
            (routes, "probe_duration", lambda _: 60),
        ):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(gc.collect)
        auth_store.init_db()
        with auth_store.db() as c:
            c.execute("INSERT INTO users(id,email,name,password_hash,credits,created_at,role) VALUES ('owner','owner@example.test','Fixture','unused',100,?,'admin')", (auth_store.now_iso(),))

    def assert_queued_once(self, task_id, background):
        self.assertEqual(background.tasks, [], "HTTP and worker must not both execute the job")
        with auth_store.db() as c:
            claim = worker._claim_next_task(c, Counter(), None)
        self.assertEqual(claim, (task_id, "owner"))
        with auth_store.db() as c:
            self.assertIsNone(worker._claim_next_task(c, Counter(), None))

    def seed(self, status):
        with auth_store.db() as c:
            c.execute("""INSERT INTO tasks(id,user_id,title,file_name,stored_path,status,stage,
                estimated_minutes,credits_used,created_at,updated_at,result_json,delivery_json)
                VALUES ('task','owner','Episode','1.mp4','fixture.mp4',?,?,1,1,?,?,'{"scenes":[]}','{"delivery":"previous"}')""",
                (status, status, auth_store.now_iso(), auth_store.now_iso()))

    def assert_delivery_cleared(self):
        with auth_store.db() as c:
            row = c.execute("SELECT delivery_json FROM tasks WHERE id = 'task'").fetchone()
        self.assertIsNone(row["delivery_json"])

    def test_legacy_single_upload_is_only_started_by_worker(self):
        background = BackgroundTasks()
        result = asyncio.run(routes.create_task(None, background,
            UploadFile(filename="1.mp4", file=BytesIO(b"fixture")), "Episode", 60, "zh", "zh"))
        self.assert_queued_once(result["id"], background)

    def test_out_of_order_batch_publishes_only_reconciled_screenplays(self):
        stamp = auth_store.now_iso()
        scripts = [
            episode("年轻女子"),
            episode("年轻女子"),
            episode("沈宁", aliases=["年轻女子"], nameEvidence="年轻女子的画面姓名条显示沈宁"),
        ]
        with auth_store.db() as c:
            for index, script in enumerate(scripts, 1):
                body = json.dumps(script, ensure_ascii=False)
                c.execute(
                    """INSERT INTO tasks(id,user_id,title,file_name,stored_path,status,stage,progress_percent,
                       estimated_minutes,credits_used,created_at,updated_at,result_json,delivery_json,batch_id,batch_index,batch_total)
                       VALUES (?,'owner',?,?,'fixture.mp4',?,?,98,1,1,?,?,?,?,'batch',?,3)""",
                    (str(index), f"第{index}集", f"{index}.mp4",
                     "running" if index == 1 else "finalizing", "batch_finalizing",
                     stamp, stamp, body, body, index),
                )

        self.assertEqual(task_store.reconcile_batch_character_continuity("batch"), 0)
        self.assertIsNone(routes.task_detail(None, "2")["result"])
        for export, args in ((routes.download_task, ("2",)), (routes.download_batch, ("batch",)),
                             (routes.download_all_tasks, ())):
            with self.assertRaises(HTTPException) as denied:
                export(None, *args, fmt="md")
            self.assertEqual(denied.exception.status_code, 409)

        task_store.update_task("1", status="finalizing")
        task_store.finalize_ready_batches()

        rendered = routes.download_batch(None, "batch", fmt="md").body.decode()
        self.assertEqual(rendered.count("沈宁"), 6, rendered)
        self.assertNotIn("年轻女子", rendered)
        self.assertEqual(rendered.count("林先生，你到底是不是顾青山"), 3)
        for index in range(1, 4):
            detail = routes.task_detail(None, str(index))
            self.assertEqual(detail["status"], "done")
            self.assertEqual(detail["result"]["scenes"][0]["blocks"][0]["speaker"], "沈宁")
        self.assertEqual(task_store.reconcile_batch_character_continuity("batch"), 0)

    def test_owner_retry_is_only_started_by_worker(self):
        self.seed("failed")
        background = BackgroundTasks()
        asyncio.run(routes.retry_task(None, "task", background))
        self.assert_queued_once("task", background)

    def test_admin_failed_retry_is_only_started_by_worker(self):
        self.seed("failed")
        background = BackgroundTasks()
        asyncio.run(routes.admin_retry_task(None, "task", background))
        self.assert_queued_once("task", background)
        self.assert_delivery_cleared()

    def test_admin_review_retry_is_only_started_by_worker(self):
        self.seed("review")
        background = BackgroundTasks()
        result = asyncio.run(routes.admin_retry_task(None, "task", background))
        self.assertFalse(result["charged"])
        self.assert_queued_once("task", background)
        self.assert_delivery_cleared()

    def test_admin_done_recheck_is_only_started_by_worker(self):
        self.seed("done")
        background = BackgroundTasks()
        result = asyncio.run(routes.admin_retry_task(None, "task", background))
        self.assertTrue(result["previousVersionPreserved"])
        self.assert_queued_once("task", background)
        self.assert_delivery_cleared()
        with auth_store.db() as c:
            snapshot = c.execute("SELECT snapshot_json FROM task_recheck_snapshots WHERE task_id = 'task'").fetchone()
        self.assertEqual(json.loads(snapshot["snapshot_json"])["delivery_json"], '{"delivery":"previous"}')

    def check_stale_retry_rolls_back_charge(self, handler):
        self.seed("failed")
        stale = routes.task_row("task")
        asyncio.run(handler(None, "task", BackgroundTasks()))
        with auth_store.db() as c:
            balance = c.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0]
            entries = c.execute("SELECT count(*) FROM credit_ledger").fetchone()[0]
        with patch.object(routes, "task_row", return_value=stale):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(handler(None, "task", BackgroundTasks()))
        self.assertEqual(error.exception.status_code, 409)
        with auth_store.db() as c:
            self.assertEqual(c.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], balance)
            self.assertEqual(c.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], entries)

    def test_owner_competing_retry_does_not_charge_twice(self):
        self.check_stale_retry_rolls_back_charge(routes.retry_task)

    def test_admin_competing_retry_does_not_charge_twice(self):
        self.check_stale_retry_rolls_back_charge(routes.admin_retry_task)


if __name__ == "__main__":
    unittest.main()
