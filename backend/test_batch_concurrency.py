"""Batch throughput regressions without changing source evidence or QA."""

import asyncio
import os
import runpy
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import config, evidence, processing, providers, worker


def twenty_four_lane_settings():
    with patch.dict(os.environ, {"JBB_WORKER_CONCURRENCY": "24", "JBB_ARK_MAX_IN_FLIGHT": "24"}):
        return runpy.run_path(config.__file__)


class RuntimeSettingsTests(unittest.TestCase):
    def test_three_accounts_and_independent_stage_defaults_support_twenty_four_lanes(self):
        values = {
            "ARK_API_KEY": "account-one",
            "ARK_API_KEY_2": "account-two",
            "ARK_API_KEY_3": "account-three",
            "JBB_WORKER_CONCURRENCY": "24",
            "JBB_ARK_MAX_IN_FLIGHT": "24",
            "JBB_ARK_ACCOUNT_MAX_IN_FLIGHT": "8",
            "JBB_SHARED_MODEL_CONCURRENCY": "24",
            "JBB_EVIDENCE_CONCURRENCY": "4",
            "JBB_ARK_UPLOAD_MAX_IN_FLIGHT": "4",
            "JBB_ARK_PROXY_MAX_IN_FLIGHT": "2",
            "JBB_ARK_QUOTA_SCOPE": "per-key",
        }
        with patch.dict(os.environ, values):
            settings = runpy.run_path(config.__file__)
        self.assertEqual(settings["ARK_API_KEYS"], ("account-one", "account-two", "account-three"))
        self.assertEqual(settings["WORKER_CONCURRENCY"], 24)
        self.assertEqual(settings["ARK_MAX_IN_FLIGHT"], 24)
        self.assertEqual(settings["ARK_ACCOUNT_MAX_IN_FLIGHT"], 8)
        self.assertEqual(settings["SHARED_MODEL_CONCURRENCY"], 24)
        self.assertEqual(settings["EVIDENCE_CONCURRENCY"], 4)
        self.assertEqual(settings["ARK_UPLOAD_MAX_IN_FLIGHT"], 4)
        self.assertEqual(settings["ARK_UPLOAD_PROXY_MAX_IN_FLIGHT"], 2)

    def test_account_selection_round_robins_three_distinct_keys(self):
        keys = ("account-one", "account-two", "account-three")
        with (patch.object(providers, "ARK_API_KEYS", keys),
              patch.object(providers, "_ark_pool_index", 0),
              patch.object(providers, "_ark_cooldowns", {})):
            self.assertEqual([providers.ark_account_key() for _ in range(6)], list(keys) * 2)


class WorkerSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_scan_retries_a_temporary_database_error(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE tasks (id TEXT, status TEXT, updated_at TEXT)")
        connection.commit()
        scans = 0
        scan_completed = asyncio.Event()

        @contextmanager
        def database():
            nonlocal scans
            scans += 1
            if scans == 1:
                raise sqlite3.OperationalError("temporary database lock")
            with connection:
                yield connection
            scan_completed.set()

        with (patch.object(worker, "db", database),
              patch.object(worker, "WORKER_CONCURRENCY", 1),
              patch.object(worker, "_expire_incomplete_uploads", AsyncMock()),
              patch.object(worker, "_claim_next_task", return_value=None),
              patch.object(worker.logger, "exception")):
            running = asyncio.create_task(worker.run_worker())
            try:
                await asyncio.wait_for(scan_completed.wait(), 2)
                self.assertGreaterEqual(scans, 2)
                self.assertFalse(running.done())
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                connection.close()

    async def test_cleanup_and_queue_poll_errors_do_not_stop_worker(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE tasks (id TEXT, status TEXT, updated_at TEXT)")
        connection.commit()

        @contextmanager
        def database():
            with connection:
                yield connection

        claim_calls = 0
        task_started = asyncio.Event()

        def claim(*_args):
            nonlocal claim_calls
            claim_calls += 1
            if claim_calls == 1:
                raise sqlite3.OperationalError("temporary queue lock")
            if claim_calls == 2:
                return "task", "user"
            return None

        async def process(_task_id):
            task_started.set()

        with (patch.object(worker, "db", database),
              patch.object(worker, "WORKER_CONCURRENCY", 1),
              patch.object(worker, "_expire_incomplete_uploads", AsyncMock(
                  side_effect=[sqlite3.OperationalError("temporary cleanup lock"), None])),
              patch.object(worker, "_claim_next_task", side_effect=claim),
              patch.object(worker, "record_task_event"),
              patch.object(worker, "process_task", side_effect=process),
              patch.object(worker.logger, "exception")):
            running = asyncio.create_task(worker.run_worker())
            try:
                await asyncio.wait_for(task_started.wait(), 2)
                self.assertGreaterEqual(claim_calls, 2)
                self.assertFalse(running.done())
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                connection.close()

    async def test_cancelled_early_task_is_requeued_without_stopping_worker(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, user_id TEXT, status TEXT, stage TEXT, created_at TEXT, updated_at TEXT, progress_percent INTEGER, attempts INTEGER, error TEXT, completed_at TEXT, credits_used INTEGER)"
        )
        connection.execute("CREATE INDEX idx_tasks_ready_user ON tasks(user_id, created_at, id) WHERE status = 'queued' AND stage = 'queued'")
        connection.execute(
            "INSERT INTO tasks VALUES ('task', 'user', 'queued', 'queued', '1', '', 0, 0, NULL, NULL, 0)"
        )
        connection.commit()

        @contextmanager
        def database():
            with connection:
                yield connection

        second_started = asyncio.Event()
        finish_second = asyncio.Event()
        calls = 0
        child_tasks = []

        async def process(task_id):
            nonlocal calls
            child_tasks.append(asyncio.current_task())
            calls += 1
            if calls == 1:
                raise asyncio.CancelledError()
            second_started.set()
            await finish_second.wait()
            with connection:
                connection.execute(
                    "UPDATE tasks SET status = 'done', stage = 'done' WHERE id = ?",
                    (task_id,),
                )

        with (patch.object(worker, "db", database),
              patch.object(worker, "WORKER_CONCURRENCY", 1),
              patch.object(worker, "_expire_incomplete_uploads", AsyncMock()),
              patch.object(worker, "record_task_event"),
              patch.object(worker, "process_task", side_effect=process)):
            running = asyncio.create_task(worker.run_worker())
            try:
                await asyncio.wait_for(second_started.wait(), 2)
                row = connection.execute("SELECT status, stage, attempts FROM tasks WHERE id = 'task'").fetchone()
                self.assertEqual(tuple(row), ("running", "queued", 1))
                self.assertFalse(running.done())
                self.assertEqual(calls, 2)
                finish_second.set()
                await asyncio.wait_for(child_tasks[-1], 2)
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                finish_second.set()
                await asyncio.gather(*child_tasks, return_exceptions=True)
                connection.close()

    async def test_startup_recovers_abandoned_paid_task_without_requeueing_it(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, user_id TEXT, status TEXT, stage TEXT,
                created_at TEXT, updated_at TEXT, progress_percent INTEGER,
                attempts INTEGER, error TEXT, completed_at TEXT, credits_used INTEGER
            );
            CREATE INDEX idx_tasks_ready_user ON tasks(user_id, created_at, id)
              WHERE status = 'queued' AND stage = 'queued';
            CREATE TABLE users (id TEXT PRIMARY KEY, credits INTEGER);
            CREATE TABLE credit_ledger (
                user_id TEXT, amount INTEGER, balance_after INTEGER,
                entry_type TEXT, reason TEXT, created_at TEXT
            );
            CREATE TABLE task_recheck_snapshots (
                id INTEGER PRIMARY KEY, task_id TEXT, snapshot_json TEXT,
                state TEXT, created_at TEXT, resolved_at TEXT
            );
            INSERT INTO tasks VALUES ('task', 'user', 'running', 'vision', '1', '1', 62, 0, NULL, NULL, 3);
            INSERT INTO users VALUES ('user', 10);
            """
        )

        @contextmanager
        def database():
            with connection:
                yield connection

        with (patch.object(worker, "db", database),
              patch.object(worker, "WORKER_CONCURRENCY", 1),
              patch.object(worker, "_expire_incomplete_uploads", AsyncMock()),
              patch.object(worker, "record_task_event"),
              patch.object(worker, "process_task", new_callable=AsyncMock) as process):
            running = asyncio.create_task(worker.run_worker())
            try:
                await asyncio.sleep(0)
                row = connection.execute("SELECT status, stage, credits_used FROM tasks WHERE id = 'task'").fetchone()
                self.assertEqual(tuple(row), ("failed", "failed", 0))
                self.assertEqual(connection.execute("SELECT credits FROM users WHERE id = 'user'").fetchone()[0], 25)
                ledger_rows = connection.execute("SELECT amount, entry_type FROM credit_ledger").fetchall()
                self.assertEqual([tuple(row) for row in ledger_rows], [(15, "refund")])
                process.assert_not_awaited()
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                connection.close()

    async def test_twenty_four_episodes_run_and_twenty_fifth_waits_for_a_free_slot(self):
        settings = twenty_four_lane_settings()
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, stage TEXT, created_at TEXT, updated_at TEXT, progress_percent INTEGER, user_id TEXT)")
        connection.execute("CREATE INDEX idx_tasks_ready_user ON tasks(user_id, created_at, id) WHERE status = 'queued' AND stage = 'queued'")
        names = [f"{index:02d}" for index in range(1, 26)]
        connection.executemany("INSERT INTO tasks VALUES (?, 'queued', 'queued', ?, '', 0, 'user')", [(name, name) for name in names])
        connection.commit()

        @contextmanager
        def database():
            with connection:
                yield connection

        started = {name: asyncio.Event() for name in names}
        release = {name: asyncio.Event() for name in names}
        active = set()
        peak = 0
        child_tasks = []

        async def process(task_id):
            nonlocal peak
            child_tasks.append(asyncio.current_task())
            active.add(task_id)
            peak = max(peak, len(active))
            started[task_id].set()
            try:
                await release[task_id].wait()
            finally:
                active.remove(task_id)

        with (patch.object(worker, "db", database),
              patch.object(worker, "WORKER_CONCURRENCY", settings["WORKER_CONCURRENCY"]),
              patch.object(worker, "_expire_incomplete_uploads", AsyncMock()),
              patch.object(worker, "process_task", side_effect=process)):
            running = asyncio.create_task(worker.run_worker())
            try:
                await asyncio.wait_for(asyncio.gather(*(started[name].wait() for name in names[:24])), 2)
                self.assertEqual(len(active), 24)
                self.assertFalse(started["25"].is_set())
                self.assertEqual(connection.execute("SELECT status FROM tasks WHERE id = '25'").fetchone()[0], "queued")
                release["01"].set()
                await asyncio.wait_for(started["25"].wait(), 2)
                self.assertEqual(len(active), 24)
                self.assertEqual(peak, 24)
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                for event in release.values():
                    event.set()
                await asyncio.gather(*child_tasks, return_exceptions=True)
                connection.close()

    async def test_late_upload_uses_free_slot_before_running_episode_finishes(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, stage TEXT, created_at TEXT, updated_at TEXT, progress_percent INTEGER, user_id TEXT)")
        connection.execute("CREATE INDEX idx_tasks_ready_user ON tasks(user_id, created_at, id) WHERE status = 'queued' AND stage = 'queued'")

        @contextmanager
        def database():
            with connection:
                yield connection

        def enqueue(task_id, stage="queued"):
            connection.execute("INSERT INTO tasks VALUES (?, 'queued', ?, ?, '', 0, 'user')", (task_id, stage, task_id))
            connection.commit()

        started = {name: asyncio.Event() for name in ("1", "2", "3")}
        release = {name: asyncio.Event() for name in started}
        active = set()
        peak = 0
        child_tasks = []

        async def process(task_id):
            nonlocal peak
            child_tasks.append(asyncio.current_task())
            active.add(task_id)
            peak = max(peak, len(active))
            started[task_id].set()
            try:
                await release[task_id].wait()
            finally:
                active.remove(task_id)

        enqueue("1")
        with (patch.object(worker, "db", database),
              patch.object(worker, "WORKER_CONCURRENCY", 2),
              patch.object(worker, "_expire_incomplete_uploads", AsyncMock()),
              patch.object(worker, "process_task", side_effect=process)):
            running = asyncio.create_task(worker.run_worker())
            try:
                await asyncio.wait_for(started["1"].wait(), 2)
                enqueue("2")  # Simulate another upload completing later.
                enqueue("3")
                enqueue("unfinished", "uploading")
                await asyncio.wait_for(started["2"].wait(), 2)
                self.assertIn("1", active)
                self.assertFalse(started["3"].is_set())
                self.assertEqual(peak, 2)
                release["1"].set()
                await asyncio.wait_for(started["3"].wait(), 2)
                self.assertEqual(peak, 2)
                self.assertEqual(connection.execute("SELECT status FROM tasks WHERE id = 'unfinished'").fetchone()[0], "queued")
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                for event in release.values():
                    event.set()
                await asyncio.gather(*child_tasks, return_exceptions=True)
                connection.close()


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, user_id TEXT, status TEXT, stage TEXT, created_at TEXT, updated_at TEXT, progress_percent INTEGER, attempts INTEGER, error TEXT, completed_at TEXT, credits_used INTEGER, batch_id TEXT)"
        )
        self.connection.execute(
            "CREATE TABLE task_recheck_snapshots (id INTEGER PRIMARY KEY, task_id TEXT, snapshot_json TEXT, state TEXT, created_at TEXT, resolved_at TEXT)"
        )
        self.connection.execute("CREATE TABLE users (id TEXT PRIMARY KEY, credits INTEGER)")
        self.connection.execute(
            "CREATE TABLE credit_ledger (user_id TEXT, amount INTEGER, balance_after INTEGER, entry_type TEXT, reason TEXT, created_at TEXT)"
        )
        self.connection.execute(
            "INSERT INTO tasks VALUES ('task', 'user', 'running', 'vision', '1', '', 62, 0, NULL, NULL, 3, NULL)"
        )
        self.connection.execute("INSERT INTO users VALUES ('user', 10)")
        self.connection.commit()

        @contextmanager
        def database():
            with self.connection:
                yield self.connection

        self.database = database
        self.addCleanup(self.connection.close)

    def test_after_provider_stage_fails_without_repeating_request_and_refunds_once(self):
        with (patch.object(worker, "db", self.database),
              patch.object(worker, "record_task_event") as event):
            worker._recover_unexpected_task_exit("task", RuntimeError("provider interrupted"))
            row = self.connection.execute("SELECT status, stage, attempts FROM tasks WHERE id = 'task'").fetchone()
            self.assertEqual(tuple(row), ("failed", "failed", 0))
            self.assertEqual(self.connection.execute("SELECT credits FROM users WHERE id = 'user'").fetchone()[0], 25)
            self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 1)
            worker._recover_unexpected_task_exit("task", RuntimeError("duplicate recovery"))
            self.assertEqual(self.connection.execute("SELECT credits FROM users WHERE id = 'user'").fetchone()[0], 25)
            self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM credit_ledger").fetchone()[0], 1)
            self.assertEqual(event.call_args.args[1], "worker_interrupted")

    def test_interrupted_recheck_restores_previous_result_without_refund(self):
        self.connection.execute("UPDATE tasks SET batch_id = 'batch' WHERE id = 'task'")

        def restore(connection, task_id, **kwargs):
            self.assertEqual(task_id, "task")
            self.assertEqual(kwargs["allowed_statuses"], ("running",))
            connection.execute("UPDATE tasks SET status = 'done', stage = 'done' WHERE id = ?", (task_id,))
            return True

        with (patch.object(worker, "db", self.database),
              patch.object(worker, "restore_recheck_snapshot_with_connection", side_effect=restore),
              patch.object(worker, "reconcile_batch_character_continuity", return_value=1) as reconcile,
              patch.object(worker, "_refund_task_once") as refund,
              patch.object(worker, "record_task_event") as event):
            worker._recover_unexpected_task_exit("task", asyncio.CancelledError())

        row = self.connection.execute("SELECT status, stage FROM tasks WHERE id = 'task'").fetchone()
        self.assertEqual(tuple(row), ("done", "done"))
        refund.assert_not_called()
        reconcile.assert_called_once_with("batch")
        self.assertEqual(event.call_args.args[1], "worker_recovered")


class ModelConcurrencyTests(unittest.TestCase):
    def test_three_accounts_run_twenty_four_streams_and_twenty_fifth_waits(self):
        settings = twenty_four_lane_settings()
        request_lane = threading.BoundedSemaphore(settings["ARK_MAX_IN_FLIGHT"])
        generation_lane = threading.BoundedSemaphore(settings["ARK_MAX_IN_FLIGHT"])
        account_lanes = {key: threading.BoundedSemaphore(8) for key in ("account-1", "account-2", "account-3")}
        lock = threading.Lock()
        twenty_four_entered = threading.Event()
        all_attempted = threading.Event()
        release = threading.Event()
        entered = 0
        attempted = 0
        active = 0
        peak = 0
        completed = {"status": "completed", "output_text": "verified script"}

        @contextmanager
        def response(*args, **kwargs):
            nonlocal entered, active, peak
            with lock:
                entered += 1
                active += 1
                peak = max(peak, active)
                if entered == 24:
                    twenty_four_entered.set()
            try:
                if not release.wait(5):
                    raise TimeoutError("test did not release model streams")
                yield [b'data: {"type":"response.completed","response":{"status":"completed","output_text":"verified script"}}\n']
            finally:
                with lock:
                    active -= 1

        def request(index=0):
            nonlocal attempted
            with lock:
                attempted += 1
                if attempted == 25:
                    all_attempted.set()
            key = f"account-{index % 3 + 1}"
            return providers.ark_stream_response({"model": "test", "input": []}, key)

        with (patch.object(providers, "_ark_request_semaphore", request_lane),
              patch.object(providers, "_ark_generation_semaphore", generation_lane),
              patch.object(providers, "_ark_key_generation_semaphore", side_effect=account_lanes.get),
              patch.object(providers, "urlopen", side_effect=response),
              ThreadPoolExecutor(max_workers=25) as callers):
            futures = [callers.submit(request, index) for index in range(25)]
            try:
                self.assertTrue(all_attempted.wait(2))
                self.assertTrue(twenty_four_entered.wait(2))
                with lock:
                    self.assertEqual(entered, 24)
                    self.assertEqual(active, 24)
                self.assertTrue(all(not future.done() for future in futures))
            finally:
                release.set()
            self.assertEqual([future.result(timeout=2) for future in futures], [completed] * 25)
            self.assertEqual(peak, 24)
            self.assertEqual(entered, 25)
            # A completed request must release its capacity for subsequent calls.
            self.assertEqual(request(0), completed)


class RecognizerThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_four_generations_run_while_general_thread_is_busy(self):
        settings = twenty_four_lane_settings()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        general_started = asyncio.Event()
        all_models_started = asyncio.Event()
        release_general = threading.Event()
        release_models = threading.Event()
        lock = threading.Lock()
        request_ids = []
        script = {"characters": ["甲"], "scenes": [{"blocks": [{"type": "action", "text": "甲进门。"}]}]}
        collected = {"sources": [], "status": "unavailable"}
        video = {"api_key": "test", "file_id": "ready"}

        def general_work():
            loop.call_soon_threadsafe(general_started.set)
            if not release_general.wait(5):
                raise TimeoutError("test did not release general thread")

        def recognize(*args, **kwargs):
            with lock:
                request_ids.append(config.REQUEST_ID.get())
                if len(request_ids) == 24:
                    loop.call_soon_threadsafe(all_models_started.set)
            if not release_models.wait(5):
                raise TimeoutError("model calls could not fill twenty-four slots")
            return script, {"total_tokens": 1}

        token = config.REQUEST_ID.set("twenty-four-model-request")
        with (ThreadPoolExecutor(max_workers=settings["ARK_MAX_IN_FLIGHT"]) as model_pool,
              patch.object(processing, "_model_executor", model_pool),
              patch.object(processing, "ARK_API_KEYS", ("test",)),
              patch.object(processing, "ark_recognize", side_effect=recognize),
              patch.object(processing, "mark_task_stage"),
              patch.object(processing, "normalize_script", return_value=script),
              patch.object(processing, "_evaluate_quality", side_effect=lambda *args: ({}, True, [])) as quality,
              patch.object(processing, "_needs_automated_content_review", return_value=False)):
            general_task = asyncio.create_task(asyncio.to_thread(general_work))
            model_tasks = []
            try:
                await asyncio.wait_for(general_started.wait(), 2)
                model_tasks = [asyncio.create_task(processing._run_recognizer_with_media(
                    {"id": str(index), "title": f"第{index}集"}, Path("source.mp4"), 1,
                    collected, {"model": "test"}, video, None,
                )) for index in range(24)]
                await asyncio.wait_for(all_models_started.wait(), 2)
                self.assertFalse(general_task.done())
                self.assertTrue(all(not task.done() for task in model_tasks))
            finally:
                release_models.set()
                release_general.set()
                results = await asyncio.gather(*model_tasks, return_exceptions=True)
                await general_task
                config.REQUEST_ID.reset(token)
            self.assertEqual(request_ids, ["twenty-four-model-request"] * 24)
            self.assertEqual(quality.call_count, 24)
            for result in results:
                self.assertNotIsInstance(result, BaseException)
                self.assertEqual(result[0], script)
                self.assertEqual(result[2]["model"], "test")
                self.assertEqual(result[3], collected)

    async def test_busy_evidence_does_not_starve_video_preparation(self):
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        release_evidence = threading.Event()
        prepared = asyncio.Event()
        request_ids = []
        collected = {"sources": [], "status": "unavailable"}
        video = {"api_key": "test", "file_id": "ready"}

        def collect(*args):
            request_ids.append(config.REQUEST_ID.get())
            if not release_evidence.wait(5):
                raise TimeoutError("video preparation was starved")
            return collected

        def prepare(*args, **kwargs):
            loop.call_soon_threadsafe(prepared.set)
            return video

        row = {"id": "task", "title": "第1集", "stored_path": "original.mp4", "duration_sec": 1}
        token = config.REQUEST_ID.set("batch-request")
        with (patch.object(processing, "recognition_cache", return_value=None),
              patch.object(processing, "materialize_media", return_value=(Path("original.mp4"), None)),
              patch.object(processing, "probe_duration", return_value=None),
              patch.object(processing, "_collect_task_evidence", side_effect=collect),
              patch.object(processing, "ark_prepare_video", side_effect=prepare),
              patch.object(processing, "ARK_API_KEYS", ("test",)),
              patch.object(processing, "mark_task_stage"),
              patch.object(processing, "_media_progress_callback"),
              patch.object(processing, "batch_character_context", return_value=None),
              patch.object(processing, "cleanup_materialized"),
              patch.object(processing, "_run_recognizer_with_media", AsyncMock(return_value="complete")) as recognize):
            running = asyncio.create_task(processing.run_recognizer(row))
            try:
                await asyncio.wait_for(prepared.wait(), 2)
                self.assertFalse(running.done())
            finally:
                release_evidence.set()
                result = await asyncio.wait_for(running, 3)
                config.REQUEST_ID.reset(token)
            self.assertEqual(result, "complete")
            self.assertEqual(request_ids, ["batch-request"])
            self.assertEqual(recognize.call_args.args[3], {**collected, "durationSec": 1})
            self.assertEqual(recognize.call_args.args[5], video)


class EvidencePipelineTests(unittest.TestCase):
    def test_next_episode_ocr_proceeds_while_previous_audio_is_busy(self):
        audio_started = threading.Event()
        release_audio = threading.Event()
        ocr_ready = {name: threading.Event() for name in ("1", "2", "3")}
        sources = {}

        def audio(path, duration, **kwargs):
            audio_started.set()
            if not release_audio.wait(5):
                raise TimeoutError("test did not release audio")
            return evidence._source("audio", "available", items=[
                {"text": "啊", "startSec": .1, "endSec": .3, "words": [{"text": "啊", "startSec": .1, "endSec": .3}]},
                {"text": "啊", "startSec": .8, "endSec": 1},
            ])

        def ocr(path, duration):
            source = evidence._source("ocr", "available", items=[
                {"text": "我", "startSec": .1, "endSec": .3},
                {"text": "我", "startSec": .8, "endSec": 1},
            ])
            sources[path.stem] = source
            ocr_ready[path.stem].set()
            return source

        frames = evidence._source("keyframes", "available", items=[{"startSec": 0, "endSec": 0}])
        with ExitStack() as stack:
            audio_pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
            ocr_pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
            callers = stack.enter_context(ThreadPoolExecutor(max_workers=3))
            stack.enter_context(patch.object(evidence, "_audio_executor", audio_pool))
            stack.enter_context(patch.object(evidence, "_ocr_executor", ocr_pool))
            stack.enter_context(patch.object(evidence, "_audio_evidence", side_effect=audio))
            stack.enter_context(patch.object(evidence, "_ocr_evidence", side_effect=ocr))
            stack.enter_context(patch.object(evidence, "_keyframe_evidence", return_value=frames))
            first = callers.submit(evidence.collect_evidence, Path("1.mp4"), "第1集", 1)
            try:
                self.assertTrue(audio_started.wait(2))
                self.assertTrue(ocr_ready["1"].wait(2))
                second = callers.submit(evidence.collect_evidence, Path("2.mp4"), "第2集", 1)
                self.assertTrue(ocr_ready["2"].wait(2), "OCR waited for unrelated audio")
                third = callers.submit(evidence.collect_evidence, Path("3.mp4"), "第3集", 1)
                self.assertTrue(ocr_ready["3"].wait(2), "later OCR waited for unrelated audio")
                self.assertFalse(first.done())
                self.assertFalse(second.done())
                self.assertFalse(third.done())
            finally:
                release_audio.set()
            for name, future in (("1", first), ("2", second), ("3", third)):
                result = future.result(timeout=2)
                self.assertEqual(result["sources"], [audio(Path(name), 1), sources[name], frames])
                self.assertEqual(result["availableSources"], ["audio", "ocr", "keyframes"])
                self.assertIn("啊", result["summary"])
                self.assertIn("我", result["summary"])
                self.assertGreaterEqual(result["timingsMs"]["audioQueue"], 0)

    def test_failure_waits_for_other_reader_before_releasing_video(self):
        audio_failed = threading.Event()
        ocr_started = threading.Event()
        release_ocr = threading.Event()

        def fail_audio(*args, **kwargs):
            self.assertTrue(ocr_started.wait(2))
            audio_failed.set()
            raise RuntimeError("failed audio reader")

        def ocr(*args):
            ocr_started.set()
            if not release_ocr.wait(5):
                raise TimeoutError("test did not release OCR")
            return evidence._source("ocr", "available")

        with (ThreadPoolExecutor(max_workers=1) as caller,
              patch.object(evidence, "_audio_evidence", side_effect=fail_audio),
              patch.object(evidence, "_ocr_evidence", side_effect=ocr),
              patch.object(evidence, "_keyframe_evidence", return_value=evidence._source("keyframes", "available"))):
            result = caller.submit(evidence.collect_evidence, Path("original.mp4"), "test", 1)
            try:
                self.assertTrue(ocr_started.wait(2))
                self.assertTrue(audio_failed.wait(2))
                self.assertFalse(result.done())
            finally:
                release_ocr.set()
            with self.assertRaisesRegex(RuntimeError, "failed audio reader"):
                result.result(timeout=2)


if __name__ == "__main__":
    unittest.main()
