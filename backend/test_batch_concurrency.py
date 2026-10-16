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


def sixteen_lane_settings():
    with patch.dict(os.environ, {"JBB_WORKER_CONCURRENCY": "16", "JBB_ARK_MAX_IN_FLIGHT": "16"}):
        return runpy.run_path(config.__file__)


class WorkerSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixteen_episodes_run_and_seventeenth_waits_for_a_free_slot(self):
        settings = sixteen_lane_settings()
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, stage TEXT, created_at TEXT, updated_at TEXT, progress_percent INTEGER, user_id TEXT)")
        names = [f"{index:02d}" for index in range(1, 18)]
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
                await asyncio.wait_for(asyncio.gather(*(started[name].wait() for name in names[:16])), 2)
                self.assertEqual(len(active), 16)
                self.assertFalse(started["17"].is_set())
                self.assertEqual(connection.execute("SELECT status FROM tasks WHERE id = '17'").fetchone()[0], "queued")
                release["01"].set()
                await asyncio.wait_for(started["17"].wait(), 2)
                self.assertEqual(len(active), 16)
                self.assertEqual(peak, 16)
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


class ModelConcurrencyTests(unittest.TestCase):
    def test_sixteen_streams_run_and_seventeenth_waits_then_capacity_recovers(self):
        settings = sixteen_lane_settings()
        lane = threading.BoundedSemaphore(settings["ARK_MAX_IN_FLIGHT"])
        lock = threading.Lock()
        sixteen_entered = threading.Event()
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
                if entered == 16:
                    sixteen_entered.set()
            try:
                if not release.wait(5):
                    raise TimeoutError("test did not release model streams")
                yield [b'data: {"type":"response.completed","response":{"status":"completed","output_text":"verified script"}}\n']
            finally:
                with lock:
                    active -= 1

        def request():
            nonlocal attempted
            with lock:
                attempted += 1
                if attempted == 17:
                    all_attempted.set()
            return providers.ark_stream_response({"model": "test", "input": []}, "test")

        with (patch.object(providers, "_ark_request_semaphore", lane),
              patch.object(providers, "urlopen", side_effect=response),
              ThreadPoolExecutor(max_workers=17) as callers):
            futures = [callers.submit(request) for _ in range(17)]
            try:
                self.assertTrue(all_attempted.wait(2))
                self.assertTrue(sixteen_entered.wait(2))
                with lock:
                    self.assertEqual(entered, 16)
                    self.assertEqual(active, 16)
                self.assertTrue(all(not future.done() for future in futures))
            finally:
                release.set()
            self.assertEqual([future.result(timeout=2) for future in futures], [completed] * 17)
            self.assertEqual(peak, 16)
            self.assertEqual(entered, 17)
            # A completed request must release its capacity for subsequent calls.
            self.assertEqual(request(), completed)


class RecognizerThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixteen_generations_run_while_general_thread_is_busy(self):
        settings = sixteen_lane_settings()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        general_started = asyncio.Event()
        all_models_started = asyncio.Event()
        release_general = threading.Event()
        release_models = threading.Event()
        lock = threading.Lock()
        request_ids = []
        script = {"characters": ["甲"], "scenes": []}
        collected = {"sources": [], "status": "unavailable"}
        video = {"api_key": "test", "file_id": "ready"}

        def general_work():
            loop.call_soon_threadsafe(general_started.set)
            if not release_general.wait(5):
                raise TimeoutError("test did not release general thread")

        def recognize(*args, **kwargs):
            with lock:
                request_ids.append(config.REQUEST_ID.get())
                if len(request_ids) == 16:
                    loop.call_soon_threadsafe(all_models_started.set)
            if not release_models.wait(5):
                raise TimeoutError("model calls could not fill sixteen slots")
            return script, {"total_tokens": 1}

        token = config.REQUEST_ID.set("sixteen-model-request")
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
                )) for index in range(16)]
                await asyncio.wait_for(all_models_started.wait(), 2)
                self.assertFalse(general_task.done())
                self.assertTrue(all(not task.done() for task in model_tasks))
            finally:
                release_models.set()
                release_general.set()
                results = await asyncio.gather(*model_tasks, return_exceptions=True)
                await general_task
                config.REQUEST_ID.reset(token)
            self.assertEqual(request_ids, ["sixteen-model-request"] * 16)
            self.assertEqual(quality.call_count, 16)
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
