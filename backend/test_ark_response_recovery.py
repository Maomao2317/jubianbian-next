import asyncio
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from app import processing, providers
from app.errors import ArkAcceptedResponseError


COMPLETED = {
    "id": "resp-test", "status": "completed",
    "usage": {"total_tokens": 4, "input_tokens": 3, "output_tokens": 1},
    "output_text": json.dumps({"scenes": [{
        "heading": "1-1 日 内 客厅", "location": "客厅",
        "blocks": [{"type": "dialogue", "speaker": "甲", "text": "我今天决定留下。"}],
    }]}, ensure_ascii=False),
}
CREATED = {"id": "resp-test", "status": "in_progress"}
MISSING = b'{"error":{"code":"NotFound","message":"response not found"}}'


def event(kind, response):
    return b"data: " + json.dumps({"type": kind, "response": response}).encode() + b"\n"


class ArkResponseRecoveryTests(unittest.TestCase):
    def recognize(self):
        return providers.ark_recognize(
            Path("episode.mp4"), "测试", 60, model="lite",
            prepared_video={"api_key": "test-key", "video_url": "https://video.invalid/source"},
        )

    def run_transport(self, finish_at):
        now = 0.0
        methods = []
        gets = []
        creates = []

        def advance(seconds):
            nonlocal now
            now += seconds

        def transport(request, timeout):
            methods.append(request.get_method())
            if request.get_method() == "POST":
                creates.append(json.loads(request.data))
                events = event("response.created", CREATED)
                if finish_at == 0:
                    events += event("response.completed", COMPLETED)
                return io.BytesIO(events)
            gets.append((now, timeout))
            self.assertTrue(request.full_url.endswith("/responses/resp-test"))
            if finish_at is not None and now >= finish_at:
                return io.BytesIO(json.dumps(COMPLETED).encode())
            raise HTTPError(request.full_url, 404, "Not Found", None, io.BytesIO(MISSING))

        with (
            patch.object(providers.time, "monotonic", side_effect=lambda: now),
            patch.object(providers.time, "sleep", side_effect=advance),
            patch.object(providers, "ARK_RESPONSE_POLL_SECONDS", 5),
            patch.object(providers, "ARK_RESPONSE_POLL_TIMEOUT_SECONDS", 900),
            patch.object(providers, "ARK_RESPONSE_CREATE_ATTEMPTS", 2),
            patch.object(providers, "urlopen", side_effect=transport),
            patch.object(providers, "ark_prepare_video") as prepare,
            patch.object(providers, "ark_model_account_key") as account,
            patch.object(providers.provider_budget, "reserve"),
            patch.object(providers.provider_budget, "finish"),
            patch.object(providers.provider_budget, "wait_cooldown"),
        ):
            if finish_at is None:
                with self.assertRaises(ArkAcceptedResponseError) as caught:
                    self.recognize()
                error = caught.exception
                self.assertEqual(error.provider_code, "ark_response_recovery_timeout")
                self.assertTrue(error.unresolved)
                self.assertTrue(processing._is_non_retryable_provider_error(error))
                self.assertFalse(processing._is_lite_model_unavailable(error))
                self.assertEqual(now, 900)
                self.assertEqual(gets[-1], (895, 5))
                result = None
            else:
                result = self.recognize()
            prepare.assert_not_called()
            account.assert_not_called()
        self.assertEqual(methods.count("POST"), 1)
        return result, creates, gets

    def test_slow_recovery_matches_normal_output_and_request(self):
        normal, normal_creates, normal_gets = self.run_transport(0)
        recovered, creates, gets = self.run_transport(890)
        self.assertEqual(normal_gets, [])
        self.assertEqual(creates, normal_creates)
        self.assertEqual(recovered, normal)
        self.assertEqual(gets[-1], (890, 10))

    def test_ambiguous_404_expires_without_new_generation(self):
        self.run_transport(None)

    def test_remaining_window_bounds_cooldown_and_request_slot(self):
        capacity = Mock()
        capacity.acquire.return_value = False
        for cooldown_blocked in (True, False):
            with (
                self.subTest(cooldown_blocked=cooldown_blocked),
                patch.object(providers.time, "monotonic", return_value=8.0),
                patch.object(providers.provider_budget, "wait_cooldown",
                             side_effect=TimeoutError if cooldown_blocked else None) as wait,
                patch.object(providers, "_ark_request_semaphore", capacity),
                patch.object(providers, "urlopen") as request,
                self.assertRaises(ArkAcceptedResponseError) as caught,
            ):
                providers.ark_http("GET", "/responses/resp-test", api_key="test-key", recovery_deadline=10)
            self.assertEqual(caught.exception.provider_code, "ark_response_recovery_timeout")
            self.assertEqual(wait.call_args.kwargs["timeout"], 2.0)
            request.assert_not_called()
        capacity.acquire.assert_called_once_with(timeout=2.0)
        capacity.release.assert_not_called()

    def test_unknown_accepted_result_never_downgrades_or_switches_model(self):
        error = providers._ark_recovery_timeout()
        with (
            patch.object(processing, "VIDEO_PROVIDER", "ark"),
            patch.object(processing, "ARK_API_KEYS", ("test-key",)),
            patch.object(processing, "ARK_FALLBACK_ON_ERROR", True),
            patch.object(processing, "OPENAI_API_KEY", "test-key"),
            patch.object(processing, "ARK_LITE_MODEL", "lite"),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "mark_task_stage"),
            patch.object(processing, "ark_recognize", side_effect=error) as recognize,
            patch.object(processing, "openai_script") as fallback,
            self.assertRaises(ArkAcceptedResponseError) as caught,
        ):
            asyncio.run(processing._run_recognizer_with_media(
                {"id": "test", "title": "测试"}, Path("episode.mp4"), 60, {},
                {"model": "lite"}, {"api_key": "test-key", "file_id": "file-test"}, None,
            ))
        self.assertIs(caught.exception, error)
        recognize.assert_called_once()
        fallback.assert_not_called()

    def test_only_confirmed_zero_usage_terminal_failure_recreates_once(self):
        failed = {"id": "resp-test", "status": "failed", "error": {"code": "ServiceUnavailable"},
                  "usage": {"total_tokens": 0}}
        cases = (
            (failed, 2, True),
            (failed, 2, False),
            ({**failed, "usage": {}}, 1, False),
            ({**failed, "usage": {"total_tokens": 0, "prompt_tokens": 1}}, 1, False),
            ({**failed, "api_cost_rmb": 0.1}, 1, False),
            ({**failed, "usage": {"total_tokens": 0, "api_cost_rmb": 0.1}}, 1, False),
            ({**failed, "usage": {"total_tokens": 0, "cost": None}}, 1, False),
            ({**failed, "error": {"code": "InvalidParameter"}}, 1, False),
            ({**failed, "status": "cancelled"}, 1, False),
        )
        for terminal, expected_posts, succeeds in cases:
            requests = []

            def transport(request, timeout):
                self.assertEqual(request.get_method(), "POST")
                requests.append(json.loads(request.data))
                if succeeds and len(requests) == 2:
                    return io.BytesIO(event("response.completed", COMPLETED))
                return io.BytesIO(event("response." + terminal["status"], terminal))

            with (
                self.subTest(terminal=terminal, succeeds=succeeds),
                patch.object(providers, "urlopen", side_effect=transport),
                patch.object(providers, "ARK_RESPONSE_CREATE_ATTEMPTS", 3),
                patch.object(providers, "ark_prepare_video") as prepare,
                patch.object(providers.provider_budget, "reserve"),
                patch.object(providers.provider_budget, "finish"),
            ):
                if succeeds:
                    script, usage = self.recognize()
                    self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "我今天决定留下。")
                    self.assertEqual(usage["total_tokens"], 4)
                else:
                    with self.assertRaises(ArkAcceptedResponseError):
                        self.recognize()
                self.assertEqual(len(requests), expected_posts)
                self.assertTrue(all(item == requests[0] for item in requests))
                prepare.assert_not_called()
