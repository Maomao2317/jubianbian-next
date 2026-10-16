"""Regressions for the October batch's missing beginnings, middles and tails."""

import asyncio
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from app.content_coverage import content_gaps, repair_windows, slice_evidence, merge_gap_repair, normalize_additive_repair
from app import processing, providers
from app.errors import ArkError
from app.script import normalize_script, script_quality, quality_gate


def action(start, end, text="甲走进房间。"):
    return {"type": "action", "text": text, "startSec": start, "endSec": end}


def script(*blocks):
    return {"title": "第11集", "characters": ["甲"], "scenes": [
        {"heading": "11-1 日 内 客厅", "location": "客厅", "timeOfDay": "日",
         "interiorExterior": "内", "blocks": list(blocks)}]}


class CoverageTests(unittest.TestCase):
    def test_reported_batch_cases(self):
        for duration, blocks, expected in [
            (170.432, [action(111, 170)], (0, 111)),
            (184.598, [action(0, 80.4)], (80.4, 184.598)),
            (114.432, [action(99, 114)], (0, 99)),
            (114.923, [], (0, 114.923)),
            (158.571, [action(0, 21)], (21, 158.571)),
            (110, [action(0, 61), action(104, 110)], (61, 104)),
        ]:
            with self.subTest(duration=duration):
                self.assertEqual(content_gaps(script(*blocks), duration), [
                    dict(zip(("startSec", "endSec"), expected))])

    def test_silent_action_counts_but_caption_and_scene_bounds_do_not(self):
        self.assertEqual(content_gaps(script(action(0, 120)), 120), [])
        caption = {**action(0, 120), "type": "screen_text"}
        draft = script(caption)
        draft["scenes"][0].update(startSec=0, endSec=120)
        quality = script_quality(draft, "ark", {"durationSec": 120})
        self.assertEqual(quality["contentGapRanges"], [{"startSec": 0, "endSec": 120}])
        self.assertFalse(quality_gate(quality)[0])

    def test_invalid_times_do_not_mask_gap(self):
        for start, end in [(True, 120), (0, float("inf")), (0, 130), (100, 1)]:
            self.assertEqual(content_gaps(script(action(start, end)), 120), [
                {"startSec": 0, "endSec": 120}])

    def test_equal_bounded_windows(self):
        windows = repair_windows([{"startSec": 0, "endSec": 111}])
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0]["startSec"], 0)
        self.assertEqual(windows[-1]["endSec"], 111)
        self.assertTrue(all(x["endSec"] - x["startSec"] <= 50 for x in windows))

    def test_slicing_keeps_source_immutable_and_rebases_words(self):
        evidence = {"summary": "whole film", "sources": [{"kind": "audio", "items": [
            {"startSec": 8, "endSec": 13, "text": "原话", "words": [
                {"startSec": 8, "endSec": 9}, {"startSec": 11, "endSec": 12}]}]},
            {"kind": "keyframes", "items": [{"startSec": 11, "endSec": None}]}]}
        before = deepcopy(evidence)
        result = slice_evidence(evidence, 10, 20)
        self.assertEqual(evidence, before)
        self.assertNotIn("summary", result)
        item = result["sources"][0]["items"][0]
        self.assertEqual((item["startSec"], item["endSec"]), (0, 3))
        self.assertEqual(item["words"], [{"startSec": 1, "endSec": 2}])
        self.assertEqual(result["sources"][1]["items"][0]["startSec"], 1)

    def test_merge_preserves_words_and_orders_inside_a_scene(self):
        old = normalize_script(script(action(0, 2, "甲开门。"), action(75, 80, "甲关门。")), "第11集")
        clip = normalize_script(script(action(0, 49, "甲坐下。")), "第11集")
        merged, added = merge_gap_repair(old, clip, {"startSec": 2, "endSec": 75}, 2, 50)
        result = normalize_script(merged, "第11集")
        blocks = [b for s in result["scenes"] for b in s["blocks"]]
        self.assertEqual([b["text"] for b in blocks], ["甲开门。", "甲坐下。", "甲关门。"])
        self.assertEqual([b["startSec"] for b in blocks], [0, 2, 75])
        self.assertEqual(added, 1)
        self.assertTrue(result["scenes"][0]["heading"].startswith("11-1"))
        self.assertEqual(old["scenes"][0]["blocks"][-1]["text"], "甲关门。")

    def test_context_overlap_does_not_duplicate_existing_block(self):
        old = script(action(48, 52, "甲关门。"))
        merged, added = merge_gap_repair(old, script(action(0, 4, "甲关门。")),
                                          {"startSec": 50, "endSec": 90}, 48, 44)
        self.assertEqual(added, 0)
        self.assertEqual(sum(len(s["blocks"]) for s in merged["scenes"]), 1)

    def test_new_alias_profiles_cannot_rewrite_existing_actions(self):
        old = normalize_script(script(action(80, 90, "甲关门。")), "第11集")
        original_block = deepcopy(old["scenes"][0]["blocks"][0])
        clip = script(action(0, 40, "张明坐下。"))
        clip["characters"] = ["张明"]
        clip["characterProfiles"] = [{"name": "张明", "aliases": ["甲"], "appearance": "短发", "clothing": "蓝色外套"}]
        candidate, _ = merge_gap_repair(old, clip, {"startSec": 0, "endSec": 80}, 0, 50)
        result = normalize_additive_repair(old, candidate, "第11集")
        blocks = [b for s in result["scenes"] for b in s["blocks"]]
        self.assertIn(original_block, blocks)
        self.assertFalse(any("contentRepair" in b["text"] for b in blocks))
        self.assertEqual([b["startSec"] for b in blocks], [0, 80])

    def test_misclassified_recaps_cannot_erase_observed_content(self):
        raw = script({**action(0, 20), "isNonPlot": True},
                     {"type": "dialogue", "speaker": "甲", "text": "我们回去。", "startSec": 20, "endSec": 22})
        raw["scenes"][0]["segmentType"] = "recap"
        result = normalize_script(raw, "第11集")
        self.assertEqual([b["text"] for b in result["scenes"][0]["blocks"]], ["甲走进房间。", "我们回去。"])


class RepairFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_stop_prevents_new_calls(self):
        stop = asyncio.Event()
        stop.set()
        model = AsyncMock()
        with patch.object(processing, "_run_model_call", model):
            _, quality, _ = await processing.repair_content_gaps(
                Path("unused"), "第11集", 120, script(action(110, 120)), {},
                {"band": "simple", "score": 0, "reasons": []}, stop_event=stop)
        model.assert_not_awaited()
        self.assertTrue(quality["contentRepair"]["remainingRanges"])

    async def test_repair_quality_does_not_run_mutating_evidence_recovery(self):
        with (patch.object(processing, "restore_evidence_boundary_segments") as recover,
              patch.object(processing, "suppress_redundant_subtitles") as suppress):
            await processing.repair_content_gaps(Path("unused"), "第11集", 120,
                script(action(0, 120)), {}, {"band": "simple", "score": 0, "reasons": []})
        recover.assert_not_called()
        suppress.assert_not_called()

    async def test_missing_beginning_uses_physical_ranges_without_whole_video_review(self):
        original = script(action(110, 120, "甲关门。"))
        def recognize_range(_path, _title, duration, evidence, _model, window, **kwargs):
            start, end = window["startSec"], window["endSec"]
            return script(action(0, end - start, "甲走动。")), {"total_tokens": 5}, start, end - start
        with (patch.object(processing, "mark_task_stage"),
              patch.object(processing, "ARK_API_KEYS", ("test",)),
              patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
              patch.object(processing, "ark_recognize", return_value=(original, {"total_tokens": 10})) as whole,
              patch.object(processing, "ark_recognize_range", side_effect=recognize_range) as ranges):
            result, quality, usage, _ = await processing._run_recognizer_with_media(
                {"id": "task", "title": "第11集"}, Path("unused"), 120, {"durationSec": 120},
                {"model": "lite", "mode": "complexity", "band": "simple", "score": 0, "reasons": []}, {}, None)
        self.assertEqual(whole.call_count, 1)
        self.assertEqual(ranges.call_count, 3)
        self.assertEqual(content_gaps(result, 120), [])
        self.assertEqual(quality["automatedReview"]["reviewScope"], "missing_video_segments")
        self.assertEqual(quality["automatedReview"]["attemptCount"], 4)
        self.assertEqual(usage["total_tokens"], 25)

    async def test_balance_failure_stops_calls_and_keeps_completed_clip(self):
        original = script(action(110, 120, "保留原稿。"))
        before = deepcopy(original)
        failure = ArkError("account balance insufficient", status_code=403, provider_code="InsufficientBalance")
        model = AsyncMock(side_effect=[(script(action(0, 38, "甲打开门。")), {"total_tokens": 20}, 0, 39), failure])
        stop = asyncio.Event()
        with patch.object(processing, "_run_model_call", model):
            repaired, quality, usage = await processing.repair_content_gaps(
                Path("unused.mp4"), "第11集", 120, original, {},
                {"band": "simple", "score": 0, "reasons": []}, stop_event=stop)
        self.assertEqual(model.await_count, 2)
        self.assertEqual(original, before)
        text = [b["text"] for s in repaired["scenes"] for b in s["blocks"]]
        self.assertIn("保留原稿。", text)
        self.assertIn("甲打开门。", text)
        self.assertEqual(usage["total_tokens"], 20)
        self.assertTrue(quality["contentRepair"]["errors"][0]["balanceError"])
        self.assertTrue(stop.is_set())
        self.assertTrue(quality["contentRepair"]["remainingRanges"])
        self.assertFalse(quality_gate(quality)[0])

    async def test_no_gap_never_calls_model(self):
        model = AsyncMock()
        with patch.object(processing, "_run_model_call", model):
            await processing.repair_content_gaps(Path("unused"), "第11集", 120,
                script(action(0, 120)), {}, {"band": "simple", "score": 0, "reasons": []})
        model.assert_not_awaited()


class CheckpointTests(unittest.TestCase):
    def test_cached_segment_does_not_upload_or_charge_again(self):
        class Cache:
            def load(self, kind):
                return {"protocol": 1, "model": "turbo", "script": script(action(0, 20))}
        with patch.object(providers, "ark_prepare_video") as prepare:
            _script, usage, offset, duration = providers.ark_recognize_range(
                Path("unused"), "第11集", 120, {}, "turbo", {"startSec": 0, "endSec": 20}, cache=Cache())
        prepare.assert_not_called()
        self.assertEqual((usage, offset, duration), ({}, 0, 22))


if __name__ == "__main__":
    unittest.main()
