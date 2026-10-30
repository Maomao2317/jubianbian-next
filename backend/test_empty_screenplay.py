"""Empty outputs fail closed without deleting valid silent action scenes."""
import asyncio
import json
import unittest
from unittest.mock import patch
from pathlib import Path
from fastapi import HTTPException
from app import processing, providers, routes
from app.errors import ArkError
from app.script import has_screenplay_body, normalize_script, script_quality, quality_gate
from app.content_coverage import content_gaps


class EmptyScreenplayTests(unittest.TestCase):
    def test_empty_or_caption_only_response_recovers_inside_creation_budget(self):
        valid = {"scenes": [{"heading": "1-1 日 内 客厅", "blocks": [
            {"type": "action", "text": "甲推开房门。", "startSec": 0, "endSec": 5}]}]}
        for empty in ({"scenes": []}, {"scenes": [{"blocks": [
                {"type": "screen_text", "text": "第1集"}]}]}):
            with self.subTest(empty=empty):
                outputs = [{"output_text": json.dumps(raw), "usage": {"input_tokens": 10, "output_tokens": 5,
                           "total_tokens": 15}} for raw in (empty, valid)]
                with (patch.object(providers, "ARK_RESPONSE_CREATE_ATTEMPTS", 2),
                      patch.object(providers, "_ark_response_with_url_retry", side_effect=outputs) as request,
                      patch.object(providers, "ark_prepare_video") as prepare,
                      patch.object(providers.time, "sleep")):
                    script, usage = providers.ark_recognize(Path("original.mp4"), "第1集", 5,
                        prepared_video={"api_key": "fixture", "file_id": "prepared"})
                self.assertTrue(has_screenplay_body(script))
                self.assertEqual(request.call_count, 2)
                prepare.assert_not_called()
                self.assertEqual(usage["total_tokens"], 30)

    def test_repeated_empty_response_stops_at_budget_without_task_level_retry(self):
        with (patch.object(providers, "ARK_RESPONSE_CREATE_ATTEMPTS", 2),
              patch.object(providers, "_ark_response_with_url_retry", return_value={"output_text": '{"scenes":[]}'}) as request,
              patch.object(providers.time, "sleep")):
            with self.assertRaises(ArkError) as caught:
                providers.ark_recognize(Path("original.mp4"), "第1集", 5,
                    prepared_video={"api_key": "fixture", "file_id": "prepared"})
        self.assertEqual(request.call_count, 2)
        self.assertTrue(processing._is_non_retryable_provider_error(caught.exception))

    def test_exhausted_empty_result_cannot_restart_the_whole_paid_task(self):
        for operation in (lambda: providers._parse_ark_script({"output_text": '{"scenes":[]}'}),
                          lambda: normalize_script({"scenes": []}, "test")):
            with self.assertRaises(ArkError) as caught:
                operation()
            self.assertEqual(caught.exception.provider_code, "empty_screenplay")
            self.assertTrue(processing._is_non_retryable_provider_error(caught.exception))

    def test_headings_captions_and_hidden_text_are_not_a_screenplay(self):
        for blocks in ([], [{"type": "dialogue", "text": "  "}],
                       [{"type": "screen_text", "text": "第1集"}],
                       [{"type": "action", "text": "甲开门。", "suppressInOutput": True}]):
            with self.subTest(blocks=blocks):
                script = {"scenes": [{"heading": "1-1 日 内 客厅", "blocks": blocks}]}
                self.assertFalse(has_screenplay_body(script))
                self.assertFalse(quality_gate(script_quality(script, "test"))[0])
                with self.assertRaises(HTTPException) as caught:
                    routes._download_markdown({"title": "第1集", "result": script})
                self.assertEqual(caught.exception.status_code, 409)

    def test_silent_continuous_action_remains_deliverable(self):
        script = {"scenes": [{"blocks": [{"type": "action", "text": "甲沿走廊走进客厅。",
                    "startSec": 0, "endSec": 90}]}]}
        self.assertTrue(has_screenplay_body(script))
        self.assertEqual(content_gaps(script, 90), [])
        self.assertIn("甲沿走廊", routes._download_markdown({"title": "第1集", "result": script}))

    def test_hidden_or_empty_block_cannot_cover_missing_time(self):
        for text, suppressed in (("", False), ("甲开门。", True)):
            script = {"scenes": [{"blocks": [{"type": "action", "text": text,
                      "suppressInOutput": suppressed, "startSec": 0, "endSec": 90}]}]}
            self.assertEqual(content_gaps(script, 90), [{"startSec": 0, "endSec": 90}])

    def test_empty_result_never_reaches_result_persistence_or_paid_retry(self):
        row = {"title": "test", "file_name": "test.mp4", "attempts": 0, "user_id": "u", "credits_used": 1}
        with (patch.object(processing, "task_row", return_value=row),
              patch.object(processing, "run_recognizer", return_value=({"scenes": []}, {}, {}, {})),
              patch.object(processing, "mark_task_stage"), patch.object(processing, "record_task_event"),
              patch.object(processing, "has_pending_recheck", return_value=False),
              patch.object(processing, "_refund_task_once") as refund,
              patch.object(processing, "update_task") as update):
            asyncio.run(processing.process_task("task"))
        refund.assert_not_called()
        self.assertEqual(update.call_args.kwargs["status"], "failed")
        self.assertEqual(update.call_args.kwargs["stage"], "quality_blocked")
        self.assertTrue(all("result_json" not in call.kwargs for call in update.call_args_list))

    def test_admin_cannot_approve_an_empty_review_candidate(self):
        with (patch.object(routes, "_require_admin", return_value={"id": "admin"}),
              patch.object(routes, "task_row", return_value={"status": "review", "quality_json": "{}", "result_json": "{}"})):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(routes.admin_approve_task(None, "task"))
        self.assertEqual(caught.exception.status_code, 409)
