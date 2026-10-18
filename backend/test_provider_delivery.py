from copy import deepcopy
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import processing, providers, storage, task_store
from app.errors import ArkError


class ProviderDeliveryTests(unittest.TestCase):
    def test_legacy_object_uri_uses_current_bucket_after_migration(self):
        client = Mock()
        client.generate_presigned_url.return_value = "https://cos.test/video"
        with (
            patch.object(storage, "_client", return_value=client),
            patch.object(storage, "OBJECT_STORAGE_BUCKET", "new-bucket"),
            patch.object(storage, "OBJECT_STORAGE_LEGACY_BUCKET", "old-bucket"),
        ):
            storage.presigned_get("s3://old-bucket/jubianbian/uploads/task.mp4")
        self.assertEqual(client.generate_presigned_url.call_args.kwargs["Params"], {
            "Bucket": "new-bucket", "Key": "jubianbian/uploads/task.mp4",
        })

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

    def test_temporary_url_timeout_refreshes_link_and_review_reuses_it_without_upload(self):
        callback = Mock()
        prepared = {"api_key": "key", "video_url": "https://cos.test/old?signature=private",
                    "_video_uri": "s3://bucket/video", "_progress_callback": callback}
        requests = []

        def model(payload, key):
            requests.append(deepcopy(payload))
            if len(requests) == 1:
                raise ArkError("Timeout while processing video_url", status_code=400, provider_code="InvalidParameter")
            return self.completed()

        with (
            patch.object(providers, "ARK_VIDEO_URL_ATTEMPTS", 2),
            patch.object(providers, "presigned_get", side_effect=["https://cos.test/first", "https://cos.test/retry", "https://cos.test/review"]),
            patch.object(providers, "ark_stream_response", side_effect=model),
            patch.object(providers, "ark_prepare_video") as upload,
            patch.object(providers.time, "sleep") as delay,
        ):
            script, _ = providers.ark_recognize(Path("source.mp4"), "第2集", 60, prepared_video=prepared)
            providers.ark_recognize(Path("source.mp4"), "第2集", 60, prepared_video=prepared,
                                    review_context={"issues": []})
        upload.assert_not_called()
        delay.assert_called_once_with(1.0)
        self.assertEqual([r["input"][0]["content"][0]["video_url"] for r in requests],
                         ["https://cos.test/first", "https://cos.test/retry", "https://cos.test/review"])
        self.assertEqual(requests[0]["input"][0]["content"][1], requests[1]["input"][0]["content"][1])
        self.assertEqual(requests[1]["input"][0]["content"][0]["fps"], 2)
        self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "今天，我决定，我要留下。")
        callback.assert_any_call("video_read_retry", None)
        callback.assert_any_call("vision", None)

    def test_persistent_url_timeouts_use_bounded_retry_then_one_files_upload(self):
        prepared = {"api_key": "key", "video_url": "https://cos.test/video"}
        timeout = ArkError("Timeout while processing video_url", status_code=400, provider_code="InvalidParameter")
        with (
            patch.object(providers, "ARK_VIDEO_URL_ATTEMPTS", 2),
            patch.object(providers, "ark_stream_response", side_effect=[timeout, timeout, self.completed(), self.completed()]) as model,
            patch.object(providers, "ark_prepare_video", return_value={"api_key": "key", "file_id": "file"}) as upload,
            patch.object(providers.time, "sleep"),
        ):
            providers.ark_recognize(Path("source.mp4"), "第2集", 131, prepared_video=prepared)
            providers.ark_recognize(Path("source.mp4"), "第2集", 131, prepared_video=prepared, review_context={"issues": []})
        self.assertEqual(model.call_count, 4)
        upload.assert_called_once_with(Path("source.mp4"), 131, force_files=True)
        self.assertEqual(prepared, {"api_key": "key", "file_id": "file"})

    def test_permission_and_format_errors_skip_url_retries(self):
        for error in (ArkError("video_url forbidden", status_code=403),
                      ArkError("unsupported media format in video_url", status_code=400)):
            with (
                self.subTest(error=error),
                patch.object(providers, "ark_stream_response", side_effect=[error, self.completed()]) as model,
                patch.object(providers, "ark_prepare_video", return_value={"api_key": "key", "file_id": "file"}) as upload,
                patch.object(providers.time, "sleep") as delay,
            ):
                providers.ark_recognize(Path("source.mp4"), "第2集", 131,
                                        prepared_video={"api_key": "key", "video_url": "https://cos.test/video"})
            self.assertEqual(model.call_count, 2)
            upload.assert_called_once()
            delay.assert_not_called()

    def test_task_retry_remembers_url_ingestion_failure_and_prepares_files_directly(self):
        checkpoint = {}
        cache = Mock()
        cache.load.side_effect = lambda kind: checkpoint.get(kind)
        cache.save.side_effect = lambda kind, data: checkpoint.update({kind: data})
        prepared = {"api_key": "key", "video_url": "https://cos.test/video", "_cache": cache}
        timeout = ArkError("Timeout while processing video_url", status_code=400, provider_code="InvalidParameter")
        with (
            patch.object(providers, "ARK_VIDEO_URL_ATTEMPTS", 2),
            patch.object(providers, "ark_stream_response", side_effect=[timeout, timeout, self.completed()]),
            patch.object(providers, "ark_prepare_video", return_value={"api_key": "key", "file_id": "file", "_cache": cache}),
            patch.object(providers.time, "sleep"),
        ):
            providers.ark_recognize(Path("source.mp4"), "第2集", 131, prepared_video=prepared)
        self.assertEqual(checkpoint["delivery_route"], {"mode": "files", "reason": "url_ingestion_failed"})
        self.assertIn("model_initial", checkpoint)
        self.assertNotIn("api_key", checkpoint["model_initial"])
        self.assertNotIn("video_url", checkpoint["model_initial"])
        source = SimpleNamespace(name="source.mp4", stat=lambda: SimpleNamespace(st_size=1024))
        with (
            patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "auto"),
            patch.object(providers, "ark_account_key", return_value="key"),
            patch.object(providers, "presigned_get") as sign,
            patch.object(providers, "ark_upload_video", return_value=("next-file", "key")) as upload,
            patch.object(providers, "ark_wait_for_file"),
        ):
            retry = providers.ark_prepare_video(source, 131, source_uri="s3://bucket/video", cache=cache)
        sign.assert_not_called()
        upload.assert_called_once_with(source, "key", 131, cache=cache)
        self.assertEqual(retry["file_id"], "next-file")

    def test_signer_failure_keeps_existing_link_and_never_logs_signature(self):
        url = "https://cos.test/video?signature=private-secret"
        prepared = {"api_key": "key", "video_url": url, "_video_uri": "s3://bucket/video"}
        timeout = ArkError("Timeout while processing video_url", status_code=400, provider_code="InvalidParameter")
        with (
            patch.object(providers, "presigned_get", side_effect=OSError(url)),
            patch.object(providers, "ark_stream_response", side_effect=[timeout, self.completed()]) as model,
            patch.object(providers, "ark_prepare_video") as upload,
            patch.object(providers, "logger") as log,
            patch.object(providers.time, "sleep"),
        ):
            providers.ark_recognize(Path("source.mp4"), "第2集", 131, prepared_video=prepared)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(model.call_args.args[0]["input"][0]["content"][0]["video_url"], url)
        self.assertNotIn("private-secret", str(log.mock_calls))
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

    def test_review_prompt_keeps_evidence_near_issue_ranges(self):
        evidence = {"sources": [
            {"kind": "audio", "status": "available", "items": [
                {"text": "near-audio", "startSec": 1, "endSec": 2},
                {"text": "far-audio", "startSec": 100, "endSec": 101},
            ]},
            {"kind": "ocr", "status": "available", "items": [
                {"text": "near-subtitle", "startSec": 3, "endSec": 4},
                {"text": "far-subtitle", "startSec": 200, "endSec": 201},
            ]},
        ]}
        context = {"scope": "issue_patches", "ranges": [{"startSec": 1, "endSec": 2,
                   "contextStartSec": 0, "contextEndSec": 4}], "draft": {}, "issues": []}
        prompt = providers.ark_prompt("episode", 210, evidence, context)
        self.assertIn("near-audio", prompt)
        self.assertIn("near-subtitle", prompt)
        self.assertNotIn("far-audio", prompt)
        self.assertNotIn("far-subtitle", prompt)
        full_prompt = providers.ark_prompt("episode", 210, evidence)
        self.assertIn("far-audio", full_prompt)
        self.assertIn("far-subtitle", full_prompt)
        self.assertEqual(len(evidence["sources"][0]["items"]), 2)

    def test_review_prompt_preserves_spanning_and_untimed_evidence(self):
        evidence = {"sources": [{"kind": "audio", "status": "available", "items": [
            {"text": "whole-transcript", "startSec": 0, "endSec": 300},
            {"text": "untimed-name"},
        ]}]}
        context = {"scope": "issue_patches", "ranges": [{"contextStartSec": 250, "contextEndSec": 255}],
                   "draft": {}, "issues": []}
        prompt = providers.ark_prompt("episode", 300, evidence, context)
        self.assertIn("whole-transcript", prompt)
        self.assertIn("untimed-name", prompt)

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

    def test_missing_episode_tail_uses_full_review_and_keeps_final_evidence(self):
        script = {"characters": ["甲"], "scenes": [{"heading": "1-1 日 内 客厅", "blocks": [{
            "type": "dialogue", "speaker": "甲", "text": "五百万。", "startSec": 21, "endSec": 23,
        }, {"type": "action", "text": "房门紧闭。", "startSec": 28, "endSec": 30}]}]}
        missing = [{"severity": "P0", "tag": "疑似漏台词片段", "startSec": 35 + i * 2,
                    "endSec": 36 + i * 2} for i in range(55)]
        context = processing._automated_review_context(script, missing, quality={"transcriptCoverage": 14})
        self.assertEqual(context["scope"], "full_video_with_issue_ranges")
        self.assertEqual(context["reviewReason"], "extensive_content_loss")
        self.assertEqual(context["ranges"][-1]["endSec"], 144)
        self.assertEqual(len(context["issues"]), 55)
        evidence = {"sources": [{"kind": "audio", "status": "available", "items": [{
            "type": "transcript_segment", "startSec": 142, "endSec": 144, "text": "挣了八万。",
        }]}]}
        prompt = providers.ark_prompt("第1集", 148, evidence, context)
        self.assertIn("挣了八万", prompt)
        self.assertIn("必须重新查看完整视频", prompt)
        self.assertNotIn("只返回合法JSON corrections数组", prompt)

    def test_isolated_missing_line_retains_local_review(self):
        script = {"scenes": [{"blocks": [{"type": "dialogue", "speaker": "甲", "text": "我。",
                                           "startSec": 1, "endSec": 2}]}]}
        issues = [{"severity": "P0", "tag": "疑似漏台词片段", "startSec": 45, "endSec": 46}]
        context = processing._automated_review_context(script, issues, quality={"transcriptCoverage": 90})
        self.assertEqual(context["scope"], "issue_patches")
        self.assertNotIn("reviewReason", context)

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

    def test_review_allows_full_sentence_across_adjacent_asr_ranges_but_not_a_gap(self):
        context = {"ranges": [{"startSec": 137.37, "endSec": 138.13},
                              {"startSec": 138.13, "endSec": 139.73}],
                   "draft": {"scenes": [{"blocks": [
                       {"type": "dialogue", "speaker": "甲", "text": "张主任。", "startSec": 138, "endSec": 139.5},
                   ]}]}}
        correction = {"sceneIndex": 0, "blockIndex": 0, "operation": "insert_before", "block": {
            "type": "dialogue", "speaker": "乙", "text": "还聊啥，要不换个地方。",
            "startSec": 137.37, "endSec": 139.73,
        }}
        payload = {"output_text": json.dumps({"corrections": [correction]})}
        result = providers._apply_ark_review(payload, context, 200)
        self.assertEqual(result["scenes"][0]["blocks"][0]["text"], correction["block"]["text"])
        separated = {**context, "ranges": [{"startSec": 137, "endSec": 138}, {"startSec": 140, "endSec": 141}]}
        with self.assertRaises(ArkError):
            providers._apply_ark_review(payload, separated, 200)

    def test_review_can_repair_a_word_inside_original_sentence_without_touching_neighbors(self):
        first = {"type": "dialogue", "speaker": "甲", "text": "我决定。", "performance": "坚定",
                 "startSec": 118, "endSec": 125.5}
        other = {"type": "dialogue", "speaker": "乙", "text": "好。", "startSec": 126, "endSec": 127}
        context = {"ranges": [{"startSec": 122.87, "endSec": 124.11}],
                   "draft": {"scenes": [{"blocks": [first, other]}]}}
        corrected = {**first, "text": "今天，我决定。"}
        correction = {"sceneIndex": 0, "blockIndex": 0, "operation": "replace", "block": corrected}
        result = providers._apply_ark_review({"output_text": json.dumps({"corrections": [correction]})}, context, 200)
        self.assertEqual(result["scenes"][0]["blocks"], [corrected, other])
        self.assertEqual(context["draft"]["scenes"][0]["blocks"], [first, other])
        for invalid in ({**correction, "blockIndex": 1},
                        {**correction, "block": {**corrected, "endSec": 128}},
                        {**correction, "operation": "insert_after"}):
            with self.subTest(invalid=invalid), self.assertRaises(ArkError):
                providers._apply_ark_review({"output_text": json.dumps({"corrections": [invalid]})}, context, 200)

    def test_review_reports_all_invalid_patches_atomically_without_model_text_in_errors(self):
        context = {"ranges": [{"startSec": 1, "endSec": 3}],
                   "draft": {"scenes": [{"blocks": [{"type": "dialogue", "speaker": "甲", "text": "我。",
                                                     "startSec": 1, "endSec": 3}]}]}}
        correction = {"sceneIndex": 0, "blockIndex": 0, "operation": "replace", "block": {
            "type": "dialogue", "speaker": "甲", "text": "private-signature-secret", "startSec": 1, "endSec": 3,
        }}
        corrections = [correction, correction, {**correction, "block": {**correction["block"], "visibleSpeaker": True}}]
        with self.assertRaises(ArkError) as caught:
            providers._apply_ark_review({"output_text": json.dumps({"corrections": corrections})}, context, 10)
        self.assertIn("corrections[1]: duplicate replacement", str(caught.exception))
        self.assertIn("corrections[2]: invalid visible speaker", str(caught.exception))
        self.assertNotIn("private-signature-secret", str(caught.exception))
        self.assertEqual(context["draft"]["scenes"][0]["blocks"][0]["text"], "我。")

    def test_many_review_ranges_retain_full_output_allowance(self):
        script = json.loads(self.completed()["output_text"])
        context = {"scope": "issue_patches", "ranges": [
            {"startSec": index, "endSec": index + .1} for index in range(20)
        ], "draft": script}
        with (patch.object(providers, "ARK_MAX_OUTPUT_TOKENS", 32768),
              patch.object(providers, "ARK_REVIEW_MAX_OUTPUT_TOKENS", 8192),
              patch.object(providers, "ark_stream_response", return_value={"output_text": '{"corrections": []}'}) as create):
            providers.ark_recognize(Path("source.mp4"), "episode", 30, review_context=context,
                                    prepared_video={"api_key": "key", "file_id": "file"})
        self.assertEqual(create.call_args.args[0]["max_output_tokens"], 32768)

    def test_review_retries_with_full_budget_only_after_output_limit(self):
        script = json.loads(self.completed()["output_text"])
        context = {"scope": "issue_patches", "ranges": [{"startSec": 1, "endSec": 3}],
                   "draft": script}
        requests = []

        def create(payload, key):
            requests.append(deepcopy(payload))
            if len(requests) == 1:
                raise providers._ark_incomplete_error({
                    "status": "incomplete", "incomplete_details": {"reason": "length"}})
            return {"output_text": '{"corrections": []}'}

        with (patch.object(providers, "ark_stream_response", side_effect=create),
              patch.object(providers, "ARK_RESPONSE_CREATE_ATTEMPTS", 2),
              patch.object(providers, "ARK_MAX_OUTPUT_TOKENS", 32768),
              patch.object(providers, "ARK_REVIEW_MAX_OUTPUT_TOKENS", 8192)):
            result, _ = providers.ark_recognize(Path("source.mp4"), "episode", 30,
                                               review_context=context,
                                               prepared_video={"api_key": "key", "file_id": "file"})
        self.assertEqual([request["max_output_tokens"] for request in requests], [8192, 32768])
        self.assertEqual(result["scenes"][0]["blocks"][0]["text"], "今天，我决定，我要留下。")

    def test_review_retry_receives_prior_patches_and_precise_error_without_reupload(self):
        block = {"type": "dialogue", "speaker": "甲", "text": "今天，我决定。", "startSec": 1, "endSec": 3}
        context = {"scope": "issue_patches", "ranges": [{"startSec": 1, "endSec": 3}],
                   "draft": {"scenes": [{"blocks": [block]}]}}
        repair = {"sceneIndex": 0, "blockIndex": 0, "operation": "replace", "block": block}
        invalid = {"output_text": json.dumps({"corrections": [{**repair, "block": {**block, "startSec": 8, "endSec": 9}}]})}
        valid = {"output_text": json.dumps({"corrections": [repair]})}
        requests = []

        def create(payload, key):
            requests.append(deepcopy(payload))
            return invalid if len(requests) == 1 else valid

        with (patch.object(providers, "ark_stream_response", side_effect=create),
              patch.object(providers, "ARK_RESPONSE_CREATE_ATTEMPTS", 2),
              patch.object(providers, "ARK_REVIEW_MAX_OUTPUT_TOKENS", 4096),
              patch.object(providers.time, "sleep"), patch.object(providers, "ark_prepare_video") as upload):
            script, _ = providers.ark_recognize(Path("source.mp4"), "第2集", 10, review_context=context,
                                              prepared_video={"api_key": "key", "video_url": "https://cos.test/video"})
        upload.assert_not_called()
        self.assertEqual(len(requests[1]["input"]), 1)
        self.assertEqual(requests[1]["input"][0]["role"], "user")
        self.assertEqual(requests[0]["max_output_tokens"], 4096)
        self.assertEqual(requests[1]["max_output_tokens"], 4096)
        feedback = requests[1]["input"][0]["content"][1]["text"]
        self.assertIn(invalid["output_text"], feedback)
        self.assertIn("corrections[0]: replacement outside target block", feedback)
        required = requests[0]["text"]["format"]["schema"]["properties"]["corrections"]["items"]["properties"]["block"]["required"]
        self.assertTrue({"startSec", "endSec"} <= set(required))
        self.assertEqual(script["scenes"][0]["blocks"][0]["text"], block["text"])

    def test_review_reanchors_only_valid_timed_insertion_inside_same_scene(self):
        blocks = [
            {"type": "dialogue", "speaker": "甲", "text": "你听。", "startSec": 77.5, "endSec": 79},
            {"type": "dialogue", "speaker": "乙", "text": "凭什么。", "startSec": 77.5, "endSec": 80.5},
            {"type": "action", "text": "转身。", "startSec": 81, "endSec": 84},
        ]
        context = {"ranges": [{"startSec": 79.11, "endSec": 79.71}], "draft": {"scenes": [{"blocks": blocks}]}}
        correction = {"sceneIndex": 0, "blockIndex": 1, "operation": "insert_after", "block": {
            "type": "dialogue", "speaker": "甲", "text": "我问你。", "startSec": 79.11, "endSec": 79.71,
        }}
        result = providers._apply_ark_review({"output_text": json.dumps({"corrections": [correction]})}, context, 200)
        self.assertEqual([block["text"] for block in result["scenes"][0]["blocks"]], ["你听。", "我问你。", "凭什么。", "转身。"])
        self.assertEqual(context["draft"]["scenes"][0]["blocks"], blocks)
        distant = {**correction, "block": {**correction["block"], "startSec": 70, "endSec": 71}}
        distant_context = {**context, "ranges": [{"startSec": 70, "endSec": 71}],
                           "draft": {"scenes": [{"blocks": blocks}, {"blocks": [{**blocks[0], "startSec": 70, "endSec": 71}]}]}}
        self.assertIsNone(providers._ark_review_insertion_anchor(distant, distant_context["draft"], 200))
        for bad_correction, bad_context in ((distant, distant_context),
                                            ({**correction, "block": {**correction["block"], "startSec": 78}}, context),
                                            ({**correction, "block": {**correction["block"], "visibleSpeaker": True}}, context)):
            with self.subTest(correction=bad_correction), self.assertRaises(ArkError):
                providers._apply_ark_review({"output_text": json.dumps({"corrections": [bad_correction]})}, bad_context, 200)

    def test_review_context_provides_original_sentence_and_insertion_anchors(self):
        script = json.loads(self.completed()["output_text"])
        script["scenes"][0]["blocks"].append({"type": "dialogue", "speaker": "乙", "text": "好。",
                                               "startSec": 6, "endSec": 7})
        context = processing._automated_review_context(script, [{"startSec": 2, "endSec": 2.5}])
        target = context["ranges"][0]["replacementTargets"][0]
        self.assertEqual(target, {"sceneIndex": 0, "blockIndex": 0, "startSec": 1, "endSec": 4,
                                  "text": script["scenes"][0]["blocks"][0]["text"], "speaker": "甲"})
        for anchor in context["ranges"][0]["insertionAnchors"]:
            block = script["scenes"][anchor["sceneIndex"]]["blocks"][anchor["blockIndex"]]
            if anchor["operation"] == "insert_before":
                self.assertLessEqual(2, block["startSec"] + 0.5)
            else:
                self.assertGreaterEqual(2.5, block["endSec"] - 0.5)

    def test_exact_original_text_can_repair_wrong_index_without_changing_neighbors(self):
        blocks = [
            {"type": "dialogue", "speaker": "甲", "text": "保留这一句。", "startSec": 1, "endSec": 2},
            {"type": "dialogue", "speaker": "乙", "text": "我要留下。", "sourceText": "我、我要留下。",
             "startSec": 3, "endSec": 4},
        ]
        original = {"scenes": [{"blocks": blocks}]}
        before = deepcopy(original)
        context = processing._automated_review_context(original, [{"startSec": 3, "endSec": 4}])
        anchor = context["ranges"][0]["replacementTargets"][0]["text"]
        self.assertEqual(anchor, context["draft"]["scenes"][0]["blocks"][1]["text"])
        correction = {"sceneIndex": 4, "blockIndex": 99, "operation": "replace", "targetText": anchor,
                      "block": {**blocks[1], "speaker": "甲", "text": "我、我要留下。"}}
        result = providers._apply_ark_review({"output_text": json.dumps({"corrections": [correction]})}, context, 10)
        self.assertEqual(result["scenes"][0]["blocks"][0], blocks[0])
        self.assertEqual(result["scenes"][0]["blocks"][1]["speaker"], "甲")
        self.assertEqual(original, before)
        for bad_blocks, text, window in (
            ([*blocks, deepcopy(blocks[1])], anchor, {"startSec": 3, "endSec": 4}),
            (blocks, "不存在的台词", {"startSec": 3, "endSec": 4}),
            (blocks, anchor, {"startSec": 7, "endSec": 8}),
        ):
            bad_context = {"draft": {"scenes": [{"blocks": bad_blocks}]}, "ranges": [window]}
            with self.subTest(text=text, count=len(bad_blocks)), self.assertRaises(ArkError):
                providers._apply_ark_review({"output_text": json.dumps({"corrections": [
                    {**correction, "targetText": text}]} )}, bad_context, 10)

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
