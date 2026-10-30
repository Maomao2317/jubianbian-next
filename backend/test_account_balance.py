"""Billing failures stop retries without losing evidence or double refunds."""

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import AsyncMock, patch

from app import auth_store, config, processing, providers, recognition_cache, rechecks, task_store
from app.errors import ArkError, is_account_balance_error


class AccountBalanceTests(unittest.TestCase):
    def test_upload_capacity_timeout_requeues_without_refund_or_snapshot_restore(self):
        row = {
            "id": "task", "title": "episode", "file_name": "episode.mp4",
            "attempts": 0, "user_id": "user", "credits_used": 5,
        }
        failure = ArkError("upload slot busy", provider_code="video_upload_capacity_timeout")
        with (
            patch.object(processing, "task_row", return_value=row),
            patch.object(processing, "mark_task_stage"),
            patch.object(processing, "run_recognizer", new_callable=AsyncMock, side_effect=failure),
            patch.object(processing, "update_task") as update,
            patch.object(processing, "record_task_event") as event,
            patch.object(processing, "_refund_task_once") as refund,
            patch.object(processing, "has_pending_recheck") as pending_recheck,
        ):
            asyncio.run(processing.process_task("task"))

        self.assertEqual(update.call_count, 1)
        self.assertEqual(update.call_args.kwargs["status"], "queued")
        self.assertEqual(update.call_args.kwargs["attempts"], 1)
        self.assertEqual(event.call_args.args[1], "retry")
        refund.assert_not_called()
        pending_recheck.assert_not_called()

    def test_explicit_balance_messages_and_codes_are_recognized(self):
        for status, message, code in (
            (403, "The request failed because your account has an overdue balance.", ""),
            (402, "insufficient balance", ""),
            (403, "账户欠费", ""), (403, "余额不足", ""),
            (403, "", "AccountOverdue"), (403, "", "Arrearage"),
            (403, "", "InsufficientBalance"), (None, "", "account_balance_exhausted"),
        ):
            with self.subTest(status=status, message=message, code=code):
                self.assertTrue(is_account_balance_error(ArkError(message, status_code=status, provider_code=code)))

    def test_permissions_rate_limits_and_transient_failures_are_distinct(self):
        for status, message in (
            (403, "video_url forbidden"), (403, "account not authorized"),
            (429, "insufficient balance"), (503, "account overdue temporarily"),
        ):
            with self.subTest(status=status, message=message):
                self.assertFalse(is_account_balance_error(ArkError(message, status_code=status)))

    def test_http_billing_error_is_normalized_without_private_details(self):
        for message, code in (("overdue balance Request id: private-account", "Forbidden"),
                              ("video_url https://private.test/?signature=secret", "AccountOverdue")):
            with self.subTest(code=code):
                body = json.dumps({"error": {"message": message, "code": code}}).encode()
                with patch.object(providers, "_cool_down_ark_account") as cooldown:
                    error = providers._ark_http_error(
                        HTTPError("https://api.test", 403, "Forbidden", {}, io.BytesIO(body)),
                        "key",
                    )
                cooldown.assert_called_once_with("key", 300)
                self.assertEqual(error.status_code, 403)
                self.assertEqual(error.provider_code, "account_balance_exhausted")
                self.assertIn("欠费", str(error))
                self.assertNotIn("private", str(error))
                self.assertNotIn("secret", str(error))
                self.assertTrue(processing._is_non_retryable_provider_error(error))
                self.assertIn("欠费", processing._task_error_message(error))
                self.assertIn("欠费", providers.ark_fallback_message(error))

    def test_plain_http_403_retains_video_delivery_recovery(self):
        body = io.BytesIO(b'{"error":{"message":"video_url forbidden","code":"AccessDenied"}}')
        error = providers._ark_http_error(HTTPError("https://api.test", 403, "Forbidden", {}, body), "key")
        self.assertEqual(error.provider_code, "AccessDenied")
        self.assertFalse(processing._is_non_retryable_provider_error(error))
        self.assertTrue(providers._is_video_url_error(error))

    def test_billing_failure_never_recreates_response_or_uploads_video(self):
        error = ArkError("overdue balance while reading video_url", status_code=403)
        with (
            patch.object(providers, "ark_stream_response", side_effect=error) as model,
            patch.object(providers, "ark_prepare_video") as upload,
            patch.object(providers.time, "sleep") as delay,
        ):
            with self.assertRaises(ArkError):
                providers.ark_recognize(Path("source.mp4"), "test", 60,
                    prepared_video={"api_key": "key", "video_url": "https://cos.test/video"})
        model.assert_called_once()
        upload.assert_not_called()
        delay.assert_not_called()

    @contextmanager
    def task_environment(self, *, recheck=False):
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.object(auth_store, "DB_PATH", root / "db.sqlite3"))
            stack.enter_context(patch.object(config, "DATA_DIR", root))
            stack.enter_context(patch.object(config, "DB_PATH", root / "db.sqlite3"))
            # sqlite's context manager commits but does not close the handle.
            # Close every test connection before removing the Windows directory.
            open_db = auth_store.db

            @contextmanager
            def closed_db():
                with closing(open_db()) as connection, connection:
                    yield connection

            for module in (auth_store, processing, rechecks, task_store):
                stack.enter_context(patch.object(module, "db", closed_db))
            original = root / "original.mp4"
            original.write_bytes(b"original-video")
            auth_store.init_db()
            with auth_store.db() as db:
                db.execute("INSERT INTO users(id,email,password_hash,name,credits,created_at) VALUES ('user','test@example.test','unused','test',100,'now')")
                db.execute("""INSERT INTO tasks(id,user_id,title,file_name,stored_path,credits_used,status,stage,
                    evidence_json,result_json,created_at,updated_at) VALUES ('task','user','test','original.mp4',?,2,'done','done',?,?, 'now','now')""",
                    (str(original), '{"previous":true}', '{"delivered":true}'))
                if recheck:
                    rechecks.create_recheck_snapshot(db, db.execute("SELECT * FROM tasks").fetchone())
                db.execute("UPDATE tasks SET status='queued', stage='queued'")
            evidence = {"sources": [{"kind": "audio", "status": "available", "items": [{"text": "啊"}]}]}
            for name, value in (("ARK_API_KEYS", ("key",)), ("OPENAI_API_KEY", "")):
                stack.enter_context(patch.object(processing, name, value))
            stack.enter_context(patch.object(processing, "probe_duration", return_value=0))
            stack.enter_context(patch.object(processing, "materialize_media", return_value=(original, None)))
            stack.enter_context(patch.object(processing, "collect_evidence", return_value=evidence))
            stack.enter_context(patch.object(processing, "ark_prepare_video", return_value={"api_key": "key", "file_id": "file"}))
            model = stack.enter_context(patch.object(processing, "ark_recognize", side_effect=ArkError(
                "account overdue", status_code=403, provider_code="AccountOverdue")))
            stack.enter_context(patch.object(processing, "discard_local_proxy", side_effect=AssertionError("discarded cached media")))
            yield original, evidence, model

    def test_failed_task_does_not_requeue_and_refunds_once_preserving_evidence(self):
        with self.task_environment() as (original, evidence, model):
            # Simulate duplicate delivery after the first terminal failure.
            asyncio.run(processing.process_task("task"))
            asyncio.run(processing.process_task("task"))
            with auth_store.db() as db:
                task = db.execute("SELECT * FROM tasks").fetchone()
                self.assertEqual(task["status"], "failed")
                self.assertEqual(task["attempts"], 0)
                self.assertIn("欠费", task["error"])
                self.assertNotIn("Lite", task["error"])
                self.assertEqual(task["credits_used"], 0)
                self.assertEqual(db.execute("SELECT credits FROM users").fetchone()[0], 100 + 2 * processing.POINTS_PER_MINUTE)
                self.assertEqual(db.execute("SELECT count(*) FROM credit_ledger WHERE entry_type='refund'").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT count(*) FROM task_events WHERE event_type='retry'").fetchone()[0], 0)
            self.assertEqual(model.call_count, 2)  # One request per explicit invocation, no automatic retry.
            self.assertEqual(original.read_bytes(), b"original-video")
            self.assertEqual(recognition_cache.recognition_cache("task", str(original)).load("evidence"), evidence)

    def test_failed_recheck_restores_delivered_version_without_refund(self):
        with self.task_environment(recheck=True) as (original, evidence, model):
            asyncio.run(processing.process_task("task"))
            with auth_store.db() as db:
                task = db.execute("SELECT * FROM tasks").fetchone()
                self.assertEqual(task["status"], "done")
                self.assertEqual(json.loads(task["result_json"]), {"delivered": True})
                self.assertEqual(db.execute("SELECT credits FROM users").fetchone()[0], 100)
                self.assertEqual(db.execute("SELECT count(*) FROM credit_ledger").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT state FROM task_recheck_snapshots").fetchone()[0], "failed_restored")
            model.assert_called_once()
            self.assertTrue(original.exists())


if __name__ == "__main__":
    unittest.main()
