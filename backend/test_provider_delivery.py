import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import processing, providers, storage
from app.errors import ArkError


class ProviderDeliveryTests(unittest.TestCase):
    def completed(self):
        return {"status": "completed", "output_text": json.dumps({
            "characters": ["甲"],
            "scenes": [{"heading": "1-1 日 内 客厅", "location": "客厅", "blocks": [{
                "type": "dialogue", "speaker": "甲", "visibleSpeaker": "甲", "mouthMoving": True,
                "speakerEvidence": "口型同步", "text": "今天，我决定，我要留下。",
                "performance": "坚定", "emotionType": "坚定", "emotionEvidence": "语气坚定",
                "startSec": 1, "endSec": 4,
            }]}],
        }, ensure_ascii=False)}

    def test_object_read_signature_is_scoped_to_one_decoded_key(self):
        client = Mock()
        client.generate_presigned_url.return_value = "https://cos.test/video?signature=private"
        with patch.object(storage, "_client", return_value=client), patch.object(storage, "OBJECT_STORAGE_PRESIGN_SECONDS", 300):
            storage.presigned_get("s3://private-bucket/path/%E7%AC%AC2%E9%9B%86.mp4")
        client.generate_presigned_url.assert_called_once_with(
            "get_object", Params={"Bucket": "private-bucket", "Key": "path/第2集.mp4"},
            ExpiresIn=3600, HttpMethod="GET",
        )

    def test_object_prepare_skips_proxy_and_upload(self):
        with (
            patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "auto"),
            patch.object(providers, "ark_account_key", return_value="key"),
            patch.object(providers, "presigned_get", return_value="https://cos.test/video") as sign,
            patch.object(providers, "ark_upload_video") as upload,
        ):
            result = providers.ark_prepare_video(Path("source.mp4"), 131, source_uri="s3://bucket/video")
        sign.assert_called_once_with("s3://bucket/video")
        upload.assert_not_called()
        self.assertEqual(result, {"api_key": "key", "video_url": "https://cos.test/video"})

    def test_direct_input_retains_sampling_short_words_and_emotion(self):
        with (
            patch.object(providers, "ARK_VIDEO_FPS", 1),
            patch.object(providers, "ARK_MAX_OUTPUT_TOKENS", 32768),
            patch.object(providers, "ark_prepare_video") as prepare,
            patch.object(providers, "ark_stream_response", return_value=self.completed()) as create,
        ):
            script, _ = providers.ark_recognize(Path("source.mp4"), "第2集", 60,
                prepared_video={"api_key": "key", "video_url": "https://cos.test/video"})
        prepare.assert_not_called()
        request = create.call_args.args[0]
        self.assertEqual(request["max_output_tokens"], 32768)
        self.assertEqual(request["text"], {"format": {"type": "json_object"}})
        self.assertEqual(request["input"][0]["content"][0], {
            "type": "input_video", "video_url": "https://cos.test/video", "fps": 2,
        })
        block = script["scenes"][0]["blocks"][0]
        self.assertEqual(block["text"], "今天，我决定，我要留下。")
        self.assertEqual(block["performance"], "坚定")
        self.assertEqual(block["speaker"], "甲")

    def test_url_failure_uploads_once_and_review_reuses_file(self):
        prepared = {"api_key": "key", "video_url": "https://cos.test/video"}
        with (
            patch.object(providers, "ark_prepare_video", return_value={"api_key": "key", "file_id": "file"}) as upload,
            patch.object(providers, "ark_stream_response", side_effect=[
                ArkError("cannot download video_url", status_code=400), self.completed(), self.completed(),
            ]) as create,
        ):
            providers.ark_recognize(Path("source.mp4"), "第2集", 131, prepared_video=prepared)
            providers.ark_recognize(Path("source.mp4"), "第2集", 131, prepared_video=prepared, review_context={"issues": []})
        upload.assert_called_once_with(Path("source.mp4"), 131)
        self.assertEqual(prepared, {"api_key": "key", "file_id": "file"})
        self.assertEqual(create.call_args.args[0]["input"][0]["content"][0], {"type": "input_video", "file_id": "file"})

    def test_model_error_does_not_trigger_another_upload(self):
        with (
            patch.object(providers, "ark_prepare_video") as upload,
            patch.object(providers, "ark_stream_response", side_effect=ArkError("ModelNotOpen", status_code=400)),
        ):
            with self.assertRaises(ArkError):
                providers.ark_recognize(Path("source.mp4"), "第2集", 131,
                    prepared_video={"api_key": "key", "video_url": "https://cos.test/video"})
        upload.assert_not_called()

    def test_upload_does_not_acquire_model_capacity(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"id":"file"}'
        model_lane = Mock()
        model_lane.__enter__ = Mock(side_effect=AssertionError("upload blocked model slot"))
        model_lane.__exit__ = Mock(return_value=False)
        with patch.object(providers, "_ark_request_semaphore", model_lane), patch.object(providers, "urlopen", return_value=response):
            self.assertEqual(providers.ark_http("POST", "/files", b"video", api_key="key", upload=True), {"id": "file"})
        model_lane.__enter__.assert_not_called()

    def test_compact_review_keeps_verbatim_line_and_fidelity_evidence(self):
        script = providers.normalize_script(json.loads(self.completed()["output_text"]), "第2集")
        block = script["scenes"][0]["blocks"][0]
        block["rawText"] = "今天，我决定，我要留下"
        context = processing._automated_review_context(script, [{"tag": "漏台词", "startSec": 1, "endSec": 4}])
        reviewed = context["draft"]["scenes"][0]["blocks"][0]
        self.assertEqual(reviewed["text"], "今天，我决定，我要留下")
        self.assertEqual(reviewed["speakerEvidence"], "口型同步")
        self.assertEqual(reviewed["emotionEvidence"], "语气坚定")
        self.assertEqual(reviewed["startSec"], 1)
        self.assertNotIn("rawText", reviewed)
        self.assertNotIn("finalText", reviewed)
        self.assertEqual(block["text"], "今天，我决定，我要留下。")


if __name__ == "__main__":
    unittest.main()
