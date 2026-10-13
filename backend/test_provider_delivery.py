import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import processing, providers, storage, task_store
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
        source = SimpleNamespace(name="source.mp4", stat=lambda: SimpleNamespace(st_size=1024))
        with (
            patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "auto"),
            patch.object(providers, "ark_account_key", return_value="key"),
            patch.object(providers, "presigned_get", return_value="https://cos.test/video") as sign,
            patch.object(providers, "ark_upload_video") as upload,
        ):
            result = providers.ark_prepare_video(source, 131, source_uri="s3://bucket/video")
        sign.assert_called_once_with("s3://bucket/video")
        upload.assert_not_called()
        self.assertEqual(result, {"api_key": "key", "video_url": "https://cos.test/video"})

    def test_object_prepare_uploads_large_object_instead_of_using_video_url(self):
        source = SimpleNamespace(name="large.mp4", stat=lambda: SimpleNamespace(st_size=50 * 1024 * 1024))
        with (
            patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "auto"),
            patch.object(providers, "ark_account_key", return_value="key"),
            patch.object(providers, "presigned_get") as sign,
            patch.object(providers, "ark_upload_video", return_value=("file-id", "key")) as upload,
            patch.object(providers, "ark_wait_for_file") as wait,
        ):
            result = providers.ark_prepare_video(source, 131, source_uri="s3://bucket/large")
        sign.assert_not_called()
        upload.assert_called_once_with(source, "key", 131)
        wait.assert_called_once_with("file-id", "key")
        self.assertEqual(result, {"api_key": "key", "file_id": "file-id"})

    def test_object_prepare_uploads_when_local_size_is_unknown(self):
        missing = SimpleNamespace(name="missing.mp4", stat=Mock(side_effect=OSError("missing")))
        with (
            patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "auto"),
            patch.object(providers, "ark_account_key", return_value="key"),
            patch.object(providers, "presigned_get") as sign,
            patch.object(providers, "ark_upload_video", return_value=("file-id", "key")) as upload,
            patch.object(providers, "ark_wait_for_file") as wait,
        ):
            result = providers.ark_prepare_video(missing, 131, source_uri="s3://bucket/unknown")
        sign.assert_not_called()
        upload.assert_called_once_with(missing, "key", 131)
        wait.assert_called_once_with("file-id", "key")
        self.assertEqual(result, {"api_key": "key", "file_id": "file-id"})

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
        response_format = request["text"]["format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertIn("scenes", response_format["schema"]["required"])
        self.assertTrue(response_format["schema"]["additionalProperties"])
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
        upload.assert_called_once_with(Path("source.mp4"), 131, force_files=True)
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
        self.assertEqual(context["draft"]["scenes"][0]["sceneIndex"], 0)
        self.assertEqual(reviewed["blockIndex"], 0)
        self.assertNotIn("rawText", reviewed)
        self.assertNotIn("finalText", reviewed)
        self.assertEqual(block["text"], "今天，我决定，我要留下。")

    def test_truncated_response_retries_without_upload_and_accounts_for_both_calls(self):
        incomplete = {"status": "completed", "output_text": '{"scenes":[',
                      "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}
        complete = self.completed()
        complete["usage"] = {"input_tokens": 20, "output_tokens": 9, "total_tokens": 29}
        with (
            patch.object(providers, "ARK_RESPONSE_CREATE_ATTEMPTS", 2),
            patch.object(providers.time, "sleep"),
            patch.object(providers, "ark_prepare_video") as upload,
            patch.object(providers, "ark_stream_response", side_effect=[incomplete, complete]) as create,
        ):
            script, usage = providers.ark_recognize(Path("source.mp4"), "第2集", 131,
                prepared_video={"api_key": "key", "video_url": "https://cos.test/video"})
        upload.assert_not_called()
        self.assertEqual(create.call_count, 2)
        self.assertEqual(usage["total_tokens"], 42)
        self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "今天，我决定，我要留下。")

    def test_review_ranges_have_both_bounds_and_private_source_never_enters_prompt(self):
        script = json.loads(self.completed()["output_text"])
        script["privateAuditMarker"] = "source-only-marker"
        context = processing._automated_review_context(script, [{"startSec": 1}, {"endSec": 8}])
        self.assertEqual([(r["startSec"], r["endSec"]) for r in context["ranges"]], [(1, 4), (5, 8)])
        context = processing._automated_review_context(script, [{"tag": "漏台词"}])
        self.assertNotIn("source-only-marker", providers.ark_prompt("第2集", 10, review_context=context))

    def test_empty_or_incomplete_body_is_never_accepted_as_success(self):
        for value in ({"scenes": []}, {"scenes": [{"blocks": []}]},
                      {"scenes": [{"blocks": [{"type": "dialogue", "text": ""}]}]}, []):
            with self.subTest(value=value), self.assertRaises(ArkError):
                providers._parse_ark_script({"status": "completed", "output_text": json.dumps(value)})

    def test_review_that_loses_words_keeps_draft_but_good_review_can_replace_it(self):
        first = {"transcriptCoverage": 97, "dialogueEvidencePrecision": 96, "issues": []}
        worse = {**first, "transcriptCoverage": 88}
        self.assertEqual(processing._review_regression_reason(first, worse), "lower_transcriptCoverage")
        self.assertEqual(processing._review_regression_reason(first, {**first, "issues": [{"severity": "P0"}]}),
                         "more_blocking_issues")
        self.assertIsNone(processing._review_regression_reason(first, {**first, "transcriptCoverage": 99}))

    def test_review_repairs_blockers_before_advisory_asr_disagreements(self):
        missing = {"severity": "P0", "tag": "疑似漏台词片段", "startSec": 43, "endSec": 44.4}
        advisory = {"severity": "P1", "tag": "疑似漏台词片段", "startSec": 112, "endSec": 114}
        heuristic = {"severity": "P1", "tag": "画面文字需核对"}
        self.assertEqual(processing._automated_review_issues({"issues": [advisory, missing, heuristic]}), [missing])
        self.assertEqual(processing._automated_review_issues({"issues": [advisory, heuristic]}), [advisory])

    def test_first_episode_context_is_owner_scoped_and_does_not_use_later_completed_episode(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE tasks(id, user_id, batch_id, batch_index, created_at, title, status, result_json)")
        cast = json.dumps({"characterProfiles": [{"name": "苏婉晴", "aliases": ["小青"], "clothing": "淡绿色汉服"}]})
        connection.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)", [
            ("one", "owner", "batch", 1, "1", "第1集", "running", None),
            ("two", "owner", "batch", 2, "2", "第2集", "done", cast),
            ("other", "other-owner", "batch", 0, "0", "其他剧", "done", cast),
        ])
        row = {"id": "two", "user_id": "owner", "batch_id": "batch"}
        with patch.object(task_store, "db", return_value=connection):
            self.assertIsNone(task_store.batch_character_context(row))
            connection.execute("UPDATE tasks SET status='done',result_json=? WHERE id='one'", (cast,))
            context = task_store.batch_character_context(row)
        self.assertEqual(context["referenceTitle"], "第1集")
        prompt = providers.ark_prompt("第2集", 100, {"batchCharacterContext": context})
        self.assertIn("苏婉晴", prompt)
        self.assertIn("原始称谓仍逐字保留", prompt)
        connection.close()

    def test_indexed_review_preserves_all_other_dialogue_and_emotion(self):
        block = {"type": "dialogue", "speaker": "苏婉晴", "text": "我决定。", "performance": "坚定",
                 "startSec": 1, "endSec": 3}
        other = {"type": "dialogue", "speaker": "苏婉晴", "text": "我。", "performance": "迟疑",
                 "startSec": 5, "endSec": 6}
        context = {"scope": "issue_patches", "ranges": [{"startSec": 1, "endSec": 3}],
                   "draft": {"scenes": [{"blocks": [block, other]}]}}
        repair = {**block, "text": "今天，我决定。"}
        response = {"corrections": [{"sceneIndex": 0, "blockIndex": 0, "operation": "replace", "block": repair}]}
        reviewed = providers._apply_ark_review({"output_text": json.dumps(response)}, context, 10)
        self.assertEqual(reviewed["scenes"][0]["blocks"], [repair, other])
        self.assertEqual(context["draft"]["scenes"][0]["blocks"][0]["text"], "我决定。")
        for correction in (
            {**response["corrections"][0], "blockIndex": -1},
            {**response["corrections"][0], "operation": "delete"},
            {**response["corrections"][0], "block": {**repair, "startSec": 8, "endSec": 9}},
        ):
            with self.subTest(correction=correction), self.assertRaises(ArkError):
                providers._apply_ark_review({"output_text": json.dumps({"corrections": [correction]})}, context, 10)

    def test_patch_indices_refer_to_original_draft_when_multiple_words_are_inserted(self):
        context = {"scope": "issue_patches", "ranges": [{"startSec": 0, "endSec": 4}],
                   "draft": {"scenes": [{"blocks": [
                       {"type": "dialogue", "speaker": "甲", "text": "决定", "startSec": 1, "endSec": 2},
                       {"type": "dialogue", "speaker": "甲", "text": "留下", "startSec": 3, "endSec": 4},
                   ]}]}}
        corrections = [
            {"sceneIndex": 0, "blockIndex": 0, "operation": "insert_before", "block": {
                "type": "dialogue", "speaker": "甲", "text": "今天", "startSec": 0, "endSec": 1}},
            {"sceneIndex": 0, "blockIndex": 1, "operation": "insert_before", "block": {
                "type": "dialogue", "speaker": "甲", "text": "我", "startSec": 2, "endSec": 3}},
        ]
        reviewed = providers._apply_ark_review({"output_text": json.dumps({"corrections": corrections})}, context, 10)
        self.assertEqual([b["text"] for b in reviewed["scenes"][0]["blocks"]], ["今天", "决定", "我", "留下"])

    def test_patch_retains_source_metadata_and_survives_normalization(self):
        script = providers.normalize_script(json.loads(self.completed()["output_text"]), "第2集")
        original = script["scenes"][0]["blocks"][0]
        original.update(text="我决定。", sourceText="我决定。", rawText="我决定。",
                        finalText="我决定。", asrRecovered=True)
        script["scenes"][0]["blocks"].append({**original, "text": "今天", "startSec": 6, "endSec": 7,
                                               "evidenceRecovered": "字幕"})
        context = processing._automated_review_context(script, [{"startSec": 1, "endSec": 4}])
        prompt = providers.ark_prompt("第2集", 10, review_context=context)
        self.assertNotIn("_sourceScript", prompt)
        self.assertNotIn("asrRecovered", prompt)
        repaired = providers._apply_ark_review({"output_text": json.dumps({"corrections": [{
            "sceneIndex": 0, "blockIndex": 0, "operation": "replace", "block": {
                "type": "dialogue", "speaker": "甲", "text": "今天，我决定。", "startSec": 1, "endSec": 4,
            },
        }]})}, context, 10)
        self.assertEqual(repaired["scenes"][0]["blocks"][1], script["scenes"][0]["blocks"][1])
        normalized = providers.normalize_script(repaired, "第2集")
        first = normalized["scenes"][0]["blocks"][0]
        self.assertEqual(first["text"], "今天，我决定。")
        self.assertEqual(first["performance"], "坚定")
        self.assertTrue(repaired["scenes"][0]["blocks"][0]["asrRecovered"])
        self.assertEqual(original["sourceText"], "我决定。")


if __name__ == "__main__":
    unittest.main()
