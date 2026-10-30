from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import processing, providers
from app.errors import ArkError
from app.recognition_cache import RecognitionCache


class ArkCostControlTests(unittest.TestCase):
    def fixture(self):
        script = {"characters": ["甲", "乙"], "scenes": [{"heading": "1-1 日 内 客厅", "blocks": [
            {"type": "action", "text": "甲走进客厅。", "startSec": 0, "endSec": 10},
            {"type": "dialogue", "speaker": "甲", "text": "我会回来。", "startSec": 40, "endSec": 44,
             "privateMetadata": "preserve"},
            {"type": "dialogue", "speaker": "乙", "text": "我等你。", "startSec": 80, "endSec": 84},
        ]}]}
        context = processing._automated_review_context(script, [
            {"severity": "P0", "tag": "台词归属错", "startSec": 41, "endSec": 42},
        ])
        return script, context

    def test_clip_rebases_only_patches_and_preserves_original_indices_and_metadata(self):
        script, context = self.fixture()
        before = deepcopy(context)
        bounds = providers._review_clip_bounds(context, 100)
        self.assertEqual(bounds, (38, 46))
        local = providers._local_review_context(context, *bounds, 100)
        block = local["draft"]["scenes"][0]["blocks"][0]
        self.assertEqual((block["blockIndex"], block["startSec"], block["endSec"]), (1, 2, 6))
        payload = {"output_text": json.dumps({"corrections": [{"sceneIndex": 0, "blockIndex": 1,
            "targetText": block["text"], "operation": "replace", "block": {**block, "speaker": "乙"}}]})}
        result = providers._apply_ark_review(payload, local, 8)
        self.assertEqual(result["scenes"][0]["blocks"][0], script["scenes"][0]["blocks"][0])
        self.assertEqual(result["scenes"][0]["blocks"][2], script["scenes"][0]["blocks"][2])
        repaired = result["scenes"][0]["blocks"][1]
        self.assertEqual((repaired["startSec"], repaired["endSec"], repaired["speaker"]), (40, 44, "乙"))
        self.assertEqual(repaired["privateMetadata"], "preserve")
        self.assertEqual(context, before)
        for start, end in ((40, 44), (-1, 2), (2, 9), (float("nan"), 3)):
            bad = {"output_text": json.dumps({"corrections": [{"sceneIndex": 0, "blockIndex": 1,
                "operation": "replace", "block": {**block, "startSec": start, "endSec": end}}]})}
            with self.subTest(start=start), self.assertRaises(ArkError):
                providers._apply_ark_review(bad, local, 8)

    def test_real_review_path_sends_one_physical_clip_and_uses_dense_sampling(self):
        script, context = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.mp4"
            path.touch()
            with (patch.object(providers, "DATA_DIR", Path(directory)),
                  patch.object(providers.shutil, "which", return_value="ffmpeg"),
                  patch.object(providers.subprocess, "run") as encode,
                  patch.object(providers, "ARK_REVIEW_VIDEO_FPS", 2),
                  patch.object(providers, "ark_prepare_video", return_value={"api_key": "key", "video_url": "https://test/clip"}) as upload,
                  patch.object(providers, "ark_stream_response", return_value={"output_text": '{"corrections":[]}',
                      "usage": {"input_tokens": 100, "output_tokens": 10}}) as create):
                _, usage = providers.ark_recognize(path, "episode", 100, model="doubao-seed-2-1-turbo-260628",
                    review_context=context, prepared_video={"api_key": "key", "video_url": "https://test/full"})
        self.assertEqual(encode.call_count, 1)
        self.assertEqual(float(encode.call_args.args[0][5]), 38)
        self.assertEqual(upload.call_args.args[1], 8)
        self.assertEqual(create.call_count, 1)
        content = create.call_args.args[0]["input"][0]["content"]
        self.assertEqual(content[0]["video_url"], "https://test/clip")
        self.assertEqual(content[0]["fps"], 2)
        self.assertIn("切片内秒数", content[1]["text"])
        self.assertNotIn("privateMetadata", content[1]["text"])
        self.assertNotIn("我等你", content[1]["text"])
        self.assertEqual(usage["calls"][0]["video_duration_sec"], 8)

    def test_full_or_wide_review_reuses_full_video(self):
        _, context = self.fixture()
        for changed in ({**context, "scope": "full_video_with_issue_ranges"},
                        {**context, "ranges": [{"startSec": 1, "endSec": 98}]}):
            self.assertIsNone(providers._review_clip_bounds(changed, 100))

    def test_paid_clip_failure_does_not_start_a_full_video_review(self):
        _, context = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.mp4"
            path.touch()
            error = ArkError("model unavailable", status_code=503)
            with (patch.object(providers, "_recognize_review_clip", side_effect=error) as clip,
                  patch.object(providers, "ark_stream_response") as create):
                with self.assertRaises(ArkError) as raised:
                    providers.ark_recognize(path, "episode", 100, review_context=context,
                        prepared_video={"api_key": "key", "video_url": "https://test/full"})
            self.assertIs(raised.exception, error)
            clip.assert_called_once()
            create.assert_not_called()

    def test_files_fallback_keeps_dense_clip_sampling_and_isolates_delivery_cache(self):
        _, context = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.mp4"
            path.write_bytes(b"video")
            cache = object.__new__(RecognitionCache)
            cache.directory, cache.identity = Path(directory) / "cache", "source"
            cache.save("delivery", {"uri": "source-only"})
            uploads = []

            def upload(fields, *args):
                uploads.append(json.loads(fields["preprocess_configs"]))
                return b"body", "boundary"

            def encode(command, **kwargs):
                Path(command[-1]).write_bytes(b"clip")

            with (patch.object(providers, "DATA_DIR", Path(directory)),
                  patch.object(providers.shutil, "which", return_value="ffmpeg"),
                  patch.object(providers.subprocess, "run", side_effect=encode),
                  patch.object(providers, "ARK_REVIEW_VIDEO_FPS", 2),
                  patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "files"),
                  patch.object(providers, "ark_account_key", return_value="key"),
                  patch.object(providers, "multipart_body", side_effect=upload),
                  patch.object(providers, "ark_http", return_value={"id": "file-clip"}),
                  patch.object(providers, "ark_wait_for_file"),
                  patch.object(providers, "ark_stream_response", return_value={"output_text": '{"corrections":[]}'})):
                providers.ark_recognize(path, "episode", 100, review_context=context,
                    prepared_video={"_cache": cache, "api_key": "key", "video_url": "https://test/full"})
            self.assertEqual(uploads, [{"video": {"fps": 2}}])
            self.assertEqual(cache.load("delivery"), {"uri": "source-only"})
            self.assertIsNone(cache.proxy())
            self.assertEqual(len(list((cache.directory / "segments").glob("*/proxy.mp4"))), 1)

    def test_only_p0_issues_trigger_paid_content_review(self):
        for tag in ("疑似漏台词片段", "疑似漏画面文字片段", "音频对白需核对"):
            issue = {"tag": tag, "severity": "P1", "startSec": 1, "endSec": 2}
            self.assertFalse(processing._needs_automated_content_review({"issues": [issue]}))
            self.assertEqual(processing._automated_review_issues({"issues": [issue]}), [])
            for evidence in ({"audioSupported": True}, {"ocrConfirmed": True}):
                advisory = {**issue, **evidence}
                quality = {"issues": [advisory]}
                self.assertFalse(processing._needs_automated_content_review(quality))
                self.assertEqual(processing._automated_review_issues(quality), [])
                self.assertIn(advisory, quality["issues"])
            blocking = {**issue, "severity": "P0"}
            quality = {"issues": [blocking]}
            self.assertTrue(processing._needs_automated_content_review(quality))
            self.assertEqual(processing._automated_review_issues(quality), [blocking])
        self.assertFalse(processing._needs_automated_content_review({"tailGapSec": 8, "issues": []}))

    def test_cost_estimates_follow_model_and_input_tier_and_provider_cost_wins(self):
        with (patch.object(providers, "ARK_INPUT_TOKEN_PRICE_RMB_PER_MILLION", 0),
              patch.object(providers, "ARK_OUTPUT_TOKEN_PRICE_RMB_PER_MILLION", 0)):
            small = {"usage": {"input_tokens": 10000, "output_tokens": 1000}}
            self.assertEqual(providers.ark_usage(small, "doubao-seed-2-0-lite-260215")["api_cost_rmb"], .0096)
            self.assertEqual(providers.ark_usage(small, "doubao-seed-2-1-lite-260915")["api_cost_rmb"], .0107)
            self.assertEqual(providers.ark_usage(small, "doubao-seed-2-1-turbo-260628")["api_cost_rmb"], .045)
            large = {"usage": {"input_tokens": 40000, "output_tokens": 1000}}
            self.assertEqual(providers.ark_usage(large, "doubao-seed-2-0-lite-260215")["api_cost_rmb"], .0414)
            self.assertEqual(providers.ark_usage({**small, "cost_rmb": .123}, "unknown")["api_cost_rmb"], .123)
            self.assertEqual(providers.ark_usage(small, "unknown")["cost_source"], "unpriced")

    def test_lite_model_not_open_switches_url_to_another_account_and_audits_slots(self):
        script, _ = self.fixture()
        unavailable = ArkError("model is not active", status_code=404, provider_code="ModelNotOpen")
        completed = {"output_text": json.dumps(script), "usage": {
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        }}
        prepared = {"api_key": "key-a", "video_url": "https://test/video"}
        with (
            patch.object(providers, "ARK_API_KEYS", ("key-a", "key-b")),
            patch.dict(providers._ark_cooldowns, {}, clear=True),
            patch.dict(providers._ark_model_cooldowns, {}, clear=True),
            patch.object(providers, "_ark_response_with_url_retry", side_effect=[unavailable, completed]) as create,
        ):
            _, usage = providers.ark_recognize(
                Path("source.mp4"), "episode", 100,
                model="doubao-seed-2-1-lite-260915", prepared_video=prepared,
            )
        self.assertEqual(create.call_count, 2)
        self.assertEqual(prepared["api_key"], "key-b")
        self.assertEqual([call["account"] for call in usage["calls"]], ["account-1", "account-2"])
        self.assertEqual([call["status"] for call in usage["calls"]], ["failed", "success"])
        self.assertNotIn("key-a", json.dumps(usage))
        self.assertNotIn("key-b", json.dumps(usage))


if __name__ == "__main__":
    unittest.main()
