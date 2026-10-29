"""GLM trial contract: preserve screenplay structure and forbid other providers."""
import asyncio
from contextlib import nullcontext
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from app import glm_provider as glm, processing, providers, evidence
from app.errors import ArkError, is_account_balance_error


def draft(start=0, end=10):
    return {"characters": ["甲"], "characterProfiles": [{"name": "甲", "aliases": []}],
            "scenes": [{"heading": "1-1 日 内 客厅", "blocks": [
                {"type": "action", "text": "甲走到桌旁。", "startSec": start, "endSec": end}]}]}


class GLMTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(glm, "GLM_API_KEY", "fixture-only"),
                        patch.object(glm, "capacity", side_effect=lambda *a, **k: nullcontext()),
                        patch.object(glm, "presigned_get", return_value="https://private.invalid/signed")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_stream_discards_reasoning_and_keeps_final_usage(self):
        events = [{"choices": [{"delta": {"reasoning_content": "private reasoning"}}]},
                  {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}]
        raw = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
        with patch.object(glm, "urlopen", return_value=io.BytesIO(raw.encode())) as request:
            result = glm.glm_stream_response({"model": "glm-5.3-flash"})
        self.assertEqual(result["output_text"], '{"ok":true}')
        self.assertNotIn("reasoning", json.dumps(result))
        self.assertEqual(result["usage"]["total_tokens"], 150)
        self.assertEqual(request.call_args.args[0].full_url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")

    def test_truncated_stream_rejected_but_usage_retained(self):
        for finish in ("length", None):
            raw = "data: " + json.dumps({"choices": [{"delta": {"content": "{}"}, "finish_reason": finish}],
                   "usage": {"completion_tokens": 12}}) + "\n\n"
            with self.subTest(finish=finish), patch.object(glm, "urlopen", return_value=io.BytesIO(raw.encode())):
                with self.assertRaises(ArkError) as caught:
                    glm.glm_stream_response({})
                self.assertEqual(caught.exception.usage_payload["usage"]["completion_tokens"], 12)

    def test_balance_failure_is_safe_and_non_retryable(self):
        error = HTTPError("https://private.invalid/secret", 400, "bad", {},
                          io.BytesIO(b'{"error":{"code":"1113","message":"private-url"}}'))
        with patch.object(glm, "urlopen", side_effect=error), self.assertRaises(ArkError) as caught:
            glm.glm_stream_response({})
        self.assertTrue(is_account_balance_error(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_max_quality_contract_and_no_key_or_url_in_audit(self):
        audit = Mock()
        payload = {"output_text": json.dumps(draft()), "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        with patch.object(glm, "glm_stream_response", return_value=payload) as request:
            script, usage = glm.glm_recognize(Path("source.mp4"), "第1集", 10,
                prepared_video={"_video_uri": "s3://test/private.mp4", "_cache": audit})
        body = request.call_args.args[0]
        self.assertEqual(body["reasoning_effort"], "max")
        self.assertEqual(body["thinking"]["type"], "enabled")
        self.assertEqual(body["max_tokens"], 131072)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("characterProfiles", body["messages"][0]["content"])
        self.assertTrue(script["scenes"][0]["blocks"])
        self.assertEqual(usage["calls"][0]["provider"], "glm")
        self.assertNotIn("private.invalid", str(audit.save.call_args))
        self.assertNotIn("fixture-only", str(audit.save.call_args))

    def test_prior_cast_context_is_sent_only_through_glm_request(self):
        cast = {"characterProfiles": [
            {"name": "晓晨", "aliases": ["小晨"], "appearance": "黑色短发"},
        ]}
        payload = {"output_text": json.dumps(draft()), "usage": {}}
        with (patch.object(glm, "glm_stream_response", return_value=payload) as request,
              patch.object(providers, "ark_stream_response") as ark):
            glm.glm_recognize(
                Path("source.mp4"), "第2集", 10,
                evidence={"batchCharacterContext": cast},
                prepared_video={"_video_uri": "s3://test/video.mp4"},
            )
        prompt = request.call_args.args[0]["messages"][1]["content"][1]["text"]
        self.assertIn("同批前集累计的人物表", prompt)
        self.assertIn("晓晨", prompt)
        self.assertIn("小晨", prompt)
        ark.assert_not_called()

    def test_invalid_result_retries_bounded_and_counts_both_calls(self):
        responses = [{"output_text": '{}', "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
                     {"output_text": json.dumps(draft()), "usage": {"prompt_tokens": 20, "completion_tokens": 4}}]
        with patch.object(glm, "GLM_RESPONSE_ATTEMPTS", 2), patch.object(glm.time, "sleep"), patch.object(glm, "glm_stream_response", side_effect=responses) as request:
            _, usage = glm.glm_recognize(Path("source.mp4"), "第1集", 10,
                prepared_video={"_video_uri": "s3://test/video.mp4"})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["output_tokens"], 6)

    def test_estimated_cost_includes_reasoning_completion_and_cache(self):
        usage = glm.glm_usage({"usage": {"prompt_tokens": 1000000, "completion_tokens": 1000000,
                              "prompt_tokens_details": {"cached_tokens": 500000}}}, "glm-5.3-flash")
        self.assertEqual(usage["api_cost_rmb"], 3.315)
        self.assertTrue(usage["cost_is_estimate"])

    def test_prepare_uses_only_storage_and_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"test-media")
            with patch.object(glm, "object_storage_enabled", return_value=True), patch.object(providers, "_delivery_video", return_value=(video, None)), patch.object(glm, "upload_recognition_proxy", return_value="s3://test/recognition/video.mp4"), patch.object(providers, "ark_prepare_video") as ark:
                prepared = glm.glm_prepare_video(video, 10)
            ark.assert_not_called()
            self.assertEqual(prepared["provider"], "glm")
            self.assertNotIn("api_key", prepared)
            self.assertNotIn("video_url", prepared)

    def test_review_and_repair_inject_glm_functions(self):
        with patch.object(providers, "ark_recognize_range", return_value=(draft(), {}, 0, 10)) as repair:
            glm.glm_recognize_range(Path("x"), "第1集", 10, {}, "glm-5.3-flash", {"startSec": 0, "endSec": 10})
        self.assertIs(repair.call_args.kwargs["prepare_video_fn"], glm.glm_prepare_video)
        self.assertIs(repair.call_args.kwargs["recognize_fn"], glm.glm_recognize)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.mp4"
            path.touch()
            with patch.object(providers, "_review_clip_bounds", return_value=(1, 5)), patch.object(providers, "_recognize_review_clip", return_value=(draft(), {})) as review:
                glm.glm_recognize(path, "第1集", 10, review_context={"scope": "issue_patches"})
        self.assertIs(review.call_args.kwargs["recognize_fn"], glm.glm_recognize)


class GLMIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_review_writes_canonical_name_and_stays_on_glm(self):
        evidence = {
            "durationSec": 30,
            "batchCharacterContext": {"characterProfiles": [
                {"name": "晓晨", "appearance": "年轻男子，黑色短发，戴帽子", "clothing": "黑色外套"},
                {"name": "赵明", "appearance": "年轻男子，黑色短发，戴帽子", "clothing": "黑色外套"},
            ]},
        }
        initial = {
            "characters": ["黑衣男子"],
            "characterProfiles": [
                {"name": "黑衣男子", "appearance": "年轻男子，黑色短发，戴帽子", "clothing": "黑色外套"},
            ],
            "scenes": [{
                "heading": "2-1 日 内 客厅", "location": "客厅",
                "blocks": [
                    {"type": "action", "text": "黑衣男子走进客厅。", "startSec": 0, "endSec": 30},
                    {"type": "dialogue", "speaker": "黑衣男子", "text": "我回来了。", "startSec": 2, "endSec": 4},
                ],
            }],
        }
        reviewed = deepcopy(initial)
        reviewed["characters"] = ["晓晨"]
        reviewed["characterProfiles"][0]["name"] = "晓晨"
        reviewed["characterProfiles"][0]["aliases"] = ["黑衣男子"]
        reviewed["scenes"][0]["blocks"][1]["speaker"] = "晓晨"
        route = {
            "provider": "glm", "model": "glm-5.3-flash", "mode": "glm",
            "band": "glm_max", "score": 0, "reasons": [],
        }

        with (patch.object(processing, "GLM_MODEL", "glm-5.3-flash"),
              patch.object(processing, "mark_task_stage"),
              patch.object(processing, "glm_recognize", side_effect=[(initial, {}), (reviewed, {})]) as recognize,
              patch.object(processing, "ark_recognize") as ark,
              patch.object(processing, "openai_script") as openai):
            result, quality, usage, _ = await processing._run_recognizer_with_media(
                {"id": "task", "title": "第2集"}, Path("unused"), 30,
                evidence, route, {"provider": "glm"}, None,
            )

        self.assertEqual(recognize.call_count, 2)
        self.assertEqual([call.args[4] for call in recognize.call_args_list], ["glm-5.3-flash"] * 2)
        self.assertIsNotNone(recognize.call_args_list[1].kwargs["review_context"])
        self.assertEqual(result["scenes"][0]["blocks"][1]["speaker"], "晓晨")
        self.assertEqual(result["scenes"][0]["blocks"][1]["text"], "我回来了。")
        action_text = result["scenes"][0]["blocks"][0]["text"]
        self.assertTrue(action_text.endswith("走进客厅。"))
        self.assertEqual(quality["modelRoute"], "glm_max_review")
        self.assertTrue(quality["automatedReview"]["accepted"])
        self.assertEqual(usage["provider"], "glm")
        ark.assert_not_called()
        openai.assert_not_called()

    async def test_gap_repair_uses_glm_despite_retained_ark_key(self):
        route = {"provider": "glm", "model": "glm-5.3-flash", "band": "glm_max", "score": 0, "reasons": []}
        with patch.object(processing, "glm_recognize_range", return_value=(draft(4, 40), {}, 0, 40)) as repair, patch.object(processing, "ark_recognize_range") as ark:
            _, quality, _ = await processing.repair_content_gaps(Path("x"), "第1集", 40, draft(0, 4), {}, route)
        self.assertGreater(repair.call_count, 0)
        self.assertEqual(repair.call_args.args[4], "glm-5.3-flash")
        self.assertEqual(quality["contentRepair"]["remainingRanges"], [])
        ark.assert_not_called()

    async def test_audio_evidence_does_not_fall_back_to_openai(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.touch()
            with (patch.object(evidence, "VIDEO_PROVIDER", "glm"), patch.object(evidence, "OPENAI_API_KEY", "retained"),
                  patch.object(evidence, "ASR_REMOTE_URL", ""), patch.object(evidence, "LOCAL_ASR_MODEL", "small"),
                  patch.object(evidence, "_local_asr_items", return_value=[]), patch.object(providers, "openai_transcribe") as openai):
                result = evidence._audio_evidence(video, 10)
            self.assertEqual(result["status"], "unavailable")
            openai.assert_not_called()

    async def test_error_and_prepare_error_never_use_ark_or_openai(self):
        route = {"provider": "glm", "model": "glm-5.3-flash", "band": "glm_max", "score": 0, "reasons": [], "mode": "glm"}
        for prepared in ({"provider": "glm"}, None):
            with (patch.object(processing, "ARK_API_KEYS", ["retained"]), patch.object(processing, "OPENAI_API_KEY", "retained"),
                  patch.object(processing, "ARK_FALLBACK_ON_ERROR", True), patch.object(processing, "mark_task_stage"),
                  patch.object(processing, "glm_recognize", side_effect=ArkError("GLM failure", status_code=503)),
                  patch.object(processing, "ark_recognize") as ark, patch.object(processing, "openai_script") as openai):
                with self.assertRaises(ArkError):
                    await processing._run_recognizer_with_media({"id": "test", "title": "第1集"}, Path("x"), 10, {}, route, prepared, ArkError("prepare failed") if prepared is None else None)
                ark.assert_not_called()
                openai.assert_not_called()

    async def test_initial_and_review_stay_on_glm_with_ark_keys_present(self):
        route = {"provider": "glm", "model": "glm-5.3-flash", "band": "glm_max", "score": 0, "reasons": [], "mode": "glm"}
        quality = {"contentGapRanges": [], "blockingIssues": [], "issues": []}
        with (patch.object(processing, "ARK_API_KEYS", ["retained"]), patch.object(processing, "mark_task_stage"),
              patch.object(processing, "glm_recognize", return_value=(draft(), {})) as glm_call,
              patch.object(processing, "ark_recognize") as ark, patch.object(processing, "openai_script") as openai,
              patch.object(processing, "_evaluate_quality", return_value=(quality, True, [])),
              patch.object(processing, "_needs_automated_content_review", return_value=True),
              patch.object(processing, "_automated_review_context", return_value={"scope": "issue_patches"}),
              patch.object(processing, "_review_regression_reason", return_value=None)):
            _, _, usage, _ = await processing._run_recognizer_with_media({"id": "test", "title": "第1集"}, Path("x"), 10, {}, route, {"provider": "glm"}, None)
        self.assertEqual(glm_call.call_count, 2)
        self.assertEqual(usage["provider"], "glm")
        self.assertEqual(glm_call.call_args.args[4], "glm-5.3-flash")
        ark.assert_not_called()
        openai.assert_not_called()

    async def test_missing_key_fails_before_media_or_other_provider_work(self):
        with patch.object(processing, "VIDEO_PROVIDER", "glm"), patch.object(processing, "GLM_API_KEY", ""), patch.object(processing, "materialize_media") as media:
            with self.assertRaises(ArkError):
                await processing.run_recognizer({})
            media.assert_not_called()


if __name__ == "__main__":
    unittest.main()
