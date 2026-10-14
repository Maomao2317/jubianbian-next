"""Resource isolation, fairness and recovery regressions using real locks."""
import io
import multiprocessing
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from app import providers, resource_limits, worker
from app.resource_limits import ProviderBudget, capacity
from app.errors import ArkError


def hold_slot(directory, ready):
    with capacity("shared-test", 1, directory=Path(directory)):
        ready.set()
        time.sleep(30)


class SharedCapacityTests(unittest.TestCase):
    def test_other_process_waits_and_crashed_owner_releases_lock(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as root:
            ready = context.Event()
            process = context.Process(target=hold_slot, args=(root, ready))
            process.start()
            try:
                self.assertTrue(ready.wait(8))
                with self.assertRaises(TimeoutError):
                    with capacity("shared-test", 1, directory=Path(root), timeout=.1):
                        self.fail("admitted another process")
                process.terminate()
                process.join(5)
                self.assertFalse(process.is_alive())
                with capacity("shared-test", 1, directory=Path(root), timeout=1):
                    pass
            finally:
                if process.is_alive():
                    process.terminate()
                process.join(5)

    def test_exception_releases_capacity(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                with capacity("error-test", 1, directory=Path(root)):
                    raise ValueError("reader failed")
            with capacity("error-test", 1, directory=Path(root), timeout=.1):
                pass

    def test_shared_upload_wait_obeys_total_upload_deadline(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(resource_limits, "RESOURCE_DIR", Path(root)),
            patch.object(providers, "ARK_UPLOAD_MAX_IN_FLIGHT", 1),
            capacity("media-upload", 1, directory=Path(root)),
            patch.object(providers, "urlopen") as request,
        ):
            with self.assertRaises(ArkError) as caught:
                providers.ark_http("POST", "/files", api_key="key", upload=True,
                                   upload_deadline=time.monotonic() + .1)
            self.assertEqual(caught.exception.provider_code, "video_upload_retry_exhausted")
            request.assert_not_called()

    def test_stream_recovery_keeps_shared_slot_until_poll_finishes(self):
        in_poll, release, second_attempted, second_entered = (threading.Event() for _ in range(4))
        calls = 0
        lock = threading.Lock()

        @contextmanager
        def response(*args, **kwargs):
            nonlocal calls
            with lock:
                calls += 1
                index = calls
            if index == 1:
                yield [b'data: {"response":{"id":"recover","status":"in_progress"}}\n']
            else:
                second_entered.set()
                yield [b'data: {"response":{"status":"completed","output_text":"verified"}}\n']

        def poll(*args):
            in_poll.set()
            if not release.wait(5):
                raise TimeoutError("test did not release recovery")
            return {"status": "completed", "output_text": "verified"}

        def second():
            second_attempted.set()
            return providers.ark_stream_response({"model": "test"}, "key")

        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(resource_limits, "RESOURCE_DIR", Path(root)),
            patch.object(providers, "provider_budget", ProviderBudget(Path(root))),
            patch.object(providers, "SHARED_MODEL_CONCURRENCY", 1),
            patch.object(providers, "urlopen", side_effect=response),
            patch.object(providers, "_ark_poll_response", side_effect=poll),
            ThreadPoolExecutor(max_workers=2) as callers,
        ):
            first = callers.submit(providers.ark_stream_response, {"model": "test"}, "key")
            try:
                self.assertTrue(in_poll.wait(2))
                next_request = callers.submit(second)
                self.assertTrue(second_attempted.wait(2))
                self.assertFalse(second_entered.wait(.2))
            finally:
                release.set()
            self.assertEqual(first.result(3)["status"], "completed")
            self.assertEqual(next_request.result(3)["status"], "completed")
            self.assertEqual(calls, 2)


class ProviderBudgetTests(unittest.TestCase):
    def test_rpm_and_tokens_are_shared_and_actual_usage_releases_estimate(self):
        with tempfile.TemporaryDirectory() as root:
            first, other = ProviderBudget(Path(root)), ProviderBudget(Path(root))
            identity = first.reserve("account", rpm=1, tpm=100, tokens=80)
            with self.assertRaises(TimeoutError):
                other.reserve("account", rpm=1, tokens=1, timeout=.05)
            with self.assertRaises(TimeoutError):
                other.reserve("account", tpm=100, tokens=21, timeout=.05)
            first.finish(identity, 10)
            other.finish(other.reserve("account", tpm=100, tokens=90), 20)
            future = time.time() + 61
            with patch.object(resource_limits.time, "time", return_value=future):
                other.reserve("account", rpm=1, tpm=100, tokens=100)

    def test_429_backoff_is_visible_to_another_budget_instance(self):
        with tempfile.TemporaryDirectory() as root:
            first, other = ProviderBudget(Path(root)), ProviderBudget(Path(root))
            with patch.object(providers, "provider_budget", first):
                error = providers._ark_http_error(HTTPError("https://test", 429, "limited", {"Retry-After": "1"}, io.BytesIO(b"{}")), "private-key")
            self.assertEqual(error.status_code, 429)
            with self.assertRaises(TimeoutError):
                other.reserve(providers._ark_quota_scope(), timeout=.05)
            with self.assertRaises(TimeoutError):
                other.wait_cooldown(providers._ark_quota_scope(), timeout=.05)
            with patch.object(resource_limits.time, "time", return_value=time.time() + 2):
                other.wait_cooldown(providers._ark_quota_scope(), timeout=.05)
                other.reserve(providers._ark_quota_scope(), timeout=.05)

    def test_impossible_token_budget_fails_without_hanging(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                ProviderBudget(Path(root)).reserve("account", tpm=10, tokens=11)


class FairSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, user_id TEXT, status TEXT, stage TEXT, created_at TEXT, updated_at TEXT)")
        self.addCleanup(self.connection.close)

    def enqueue(self, user, count, stage="queued"):
        self.connection.executemany("INSERT INTO tasks VALUES (?, ?, 'queued', ?, ?, '')",
                                    [(f"{user}-{i:02d}", user, stage, f"{user}-{i:02d}") for i in range(count)])
        self.connection.commit()

    def claim(self, active, last):
        with self.connection:
            return worker._claim_next_task(self.connection, active, last)

    def test_two_ready_users_get_eight_each_and_keep_episode_order(self):
        self.enqueue("a", 20)
        self.enqueue("b", 20)
        self.enqueue("unfinished", 1, "uploading")
        active, last, started = Counter(), None, []
        for _ in range(16):
            task, last = self.claim(active, last)
            started.append(task)
            active[last] += 1
        self.assertEqual(active, Counter(a=8, b=8))
        self.assertEqual([task for task in started if task.startswith("a-")], [f"a-{i:02d}" for i in range(8)])
        self.assertEqual(self.connection.execute("SELECT status FROM tasks WHERE user_id='unfinished'").fetchone()[0], "queued")

    def test_single_user_can_fill_all_slots_then_new_user_gets_released_slots(self):
        self.enqueue("a", 25)
        active, last = Counter(), None
        for _ in range(16):
            _, last = self.claim(active, last)
            active[last] += 1
        self.assertEqual(active["a"], 16)
        self.enqueue("b", 20)
        for _ in range(8):
            active["a"] -= 1
            _, last = self.claim(active, last)
            self.assertEqual(last, "b")
            active[last] += 1
        self.assertEqual(active, Counter(a=8, b=8))

    def test_three_users_differ_by_at_most_one_slot(self):
        for user in ("a", "b", "c"):
            self.enqueue(user, 20)
        active, last = Counter(), None
        for _ in range(16):
            _, last = self.claim(active, last)
            active[last] += 1
        self.assertEqual(sorted(active.values()), [5, 5, 6])


if __name__ == "__main__":
    unittest.main()
