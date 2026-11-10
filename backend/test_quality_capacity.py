import copy
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import evidence, processing, remote_circuit as remote
from app.errors import ArkError
from app.script import repair_dialogue_speakers, normalize_script, script_quality


class EvidencePromptTests(unittest.TestCase):
    def test_long_audio_and_late_ocr_survive_without_mutating_raw_sources(self):
        data = {"sources": [
            {"kind": "audio", "status": "available", "items": [{"text": "音" * 11980 + "最后一句对白", "startSec": 0, "endSec": 360}]},
            {"kind": "ocr", "status": "available", "items": [{"text": f"字幕{i:04d}" + "字" * 25, "startSec": i / 4, "endSec": i / 4 + .2} for i in range(1440)]},
            {"kind": "keyframes", "status": "available", "items": []}]}
        before = copy.deepcopy(data)
        summary = evidence.evidence_summary(data)
        self.assertIn("最后一句对白", summary)
        self.assertIn("字幕1439", summary)
        self.assertIn("字幕0000", summary)
        self.assertLessEqual(len(summary), 64000)
        self.assertEqual(data, before)
        cached = evidence.collect_evidence(Path("not-opened.mp4"), "test", 360, cached_evidence={**data, "summary": "old truncated summary"})
        self.assertEqual(cached["summary"], summary)

    def test_oversized_sources_keep_whole_entries_at_both_ends(self):
        summary = evidence.evidence_summary({"sources": [{"kind": "ocr", "status": "available", "items": [
            {"text": f"第{i}条完整字幕" + "甲" * 1400, "startSec": i, "endSec": i + 1} for i in range(1440)]}]})
        self.assertLessEqual(len(summary), 64000)
        self.assertIn("第0条完整字幕", summary)
        self.assertIn("第1439条完整字幕", summary)
        self.assertIn("均匀抽样", summary)


class SpeakerSafetyTests(unittest.TestCase):
    def test_known_speaker_conflict_is_not_silently_overwritten_and_resolves(self):
        block = {"type": "dialogue", "speaker": "乙", "visibleSpeaker": "甲", "mouthMoving": True, "text": "别走。"}
        repair_dialogue_speakers([block], ["甲", "乙"])
        self.assertEqual(block["speaker"], "乙")
        self.assertEqual(block["speakerConflict"], "甲")
        block["visibleSpeaker"] = "乙"
        repair_dialogue_speakers([block], ["甲", "乙"])
        self.assertNotIn("speakerConflict", block)
        block["speaker"] = "未知说话人"
        repair_dialogue_speakers([block], ["甲", "乙"])
        self.assertEqual(block["speaker"], "乙")

    def test_inner_monologue_with_contradictory_lip_sync_requires_review(self):
        script = normalize_script({"characters": ["甲"], "scenes": [{"location": "客厅", "blocks": [
            {"type": "vo", "speaker": "甲", "voKind": "os", "visibleSpeaker": "甲", "mouthMoving": True, "text": "我一定会回来。"}]}]}, "test")
        self.assertTrue(any(i["tag"] == "OS/VO混淆" and i["severity"] == "P0" for i in script_quality(script, "test")["issues"]))


class MarginTests(unittest.TestCase):
    def test_cash_discount_and_infrastructure_are_reserved_before_review(self):
        with (patch.object(processing, "ARK_MIN_GROSS_MARGIN_RATIO", .65),
              patch.object(processing, "ARK_REVENUE_REALIZATION_RATIO", .8),
              patch.object(processing, "ARK_INFRA_RESERVE_RATIO", .05)):
            budget = processing._quality_call_budget({"credits_used": 10}, 600, 10, {"api_cost_rmb": .1}, "lite")
        self.assertAlmostEqual(budget["revenueRmb"], 3.125)
        self.assertAlmostEqual(budget["baseCostCeilingRmb"], .9375)
        self.assertAlmostEqual(budget["costCeilingRmb"], .965625)
        self.assertTrue(budget["allowed"])
        self.assertGreaterEqual(1 - (budget["projectedCostRmb"] + budget["infraReserveRmb"]) / budget["revenueRmb"], .65)


    def test_three_percent_is_a_hard_limit_on_budget_allowance(self):
        with (patch.object(processing, "ARK_MIN_GROSS_MARGIN_RATIO", .65),
              patch.object(processing, "ARK_REVENUE_REALIZATION_RATIO", .8),
              patch.object(processing, "ARK_INFRA_RESERVE_RATIO", .05),
              patch.object(processing, "ARK_QUALITY_BUDGET_OVERRUN_RATIO", .50)):
            # Base ceiling .9375; the estimated short Lite call costs .0375.
            inside = processing._quality_call_budget({"credits_used": 10}, 600, 10, {"api_cost_rmb": .92}, "lite")
            outside = processing._quality_call_budget({"credits_used": 10}, 600, 10, {"api_cost_rmb": .93}, "lite")
        self.assertEqual(inside["budgetOverrunRatio"], .03)
        self.assertTrue(inside["allowed"])
        self.assertFalse(outside["allowed"])


class MarginDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_budget_preserves_draft_and_findings_for_owner_review(self):
        draft = {"characters": ["甲"], "scenes": [{"location": "客厅", "blocks": [
            {"type": "action", "text": "甲走进房间。", "startSec": 50, "endSec": 60}]}]}
        with (patch.object(processing, "mark_task_stage"),
              patch.object(processing, "ARK_API_KEYS", ("test",)),
              patch.object(processing, "ARK_LITE_MODEL", "lite"),
              patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
              patch.object(processing, "ARK_MIN_GROSS_MARGIN_RATIO", .65),
              patch.object(processing, "ARK_REVENUE_REALIZATION_RATIO", .8),
              patch.object(processing, "ARK_INFRA_RESERVE_RATIO", .05),
              patch.object(processing, "ark_recognize", return_value=(draft, {"api_cost_rmb": .1})) as whole,
              patch.object(processing, "ark_recognize_range") as ranges):
            result, quality, usage, _ = await processing._run_recognizer_with_media(
                {"id": "task", "title": "第1集", "credits_used": 1}, Path("unused"), 60, {"durationSec": 60},
                {"model": "lite", "mode": "complexity", "band": "simple", "score": 0, "reasons": []}, {}, None)
        whole.assert_called_once()
        ranges.assert_not_called()
        self.assertEqual(result["scenes"][0]["blocks"][0]["text"], "甲走进房间。")
        self.assertEqual(quality["deliveryStatus"], "review_required")
        self.assertTrue(quality["contentGapRanges"])
        self.assertTrue(all(not guard["allowed"] for guard in quality["profitGuards"]))
        self.assertEqual(usage["api_cost_rmb"], .1)


class ProbeSharingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.gate = remote.RemoteCircuit(Path(temp.name))

    def test_healthy_probe_is_shared_without_cpu_fallback(self):
        entered, release, pending, in_work, finish_work = (threading.Event() for _ in range(5))
        calls = []
        admit = self.gate.admit
        def tracked_admit(*args):
            try:
                return admit(*args)
            except remote.ProbePending:
                pending.set()
                raise
        def probe(*args):
            entered.set()
            self.assertTrue(release.wait(2))
        def work():
            with remote.remote_attempt("asr", "http://gpu", "token", 60):
                calls.append("gpu")
                if len(calls) == 1:
                    in_work.set()
                    self.assertTrue(finish_work.wait(2))
                else:
                    finish_work.set()
        with (patch.object(remote, "circuit", self.gate), patch.object(remote, "_probe", side_effect=probe) as check,
              patch.object(self.gate, "admit", side_effect=tracked_admit),
              patch.object(remote.config, "GPU_ASR_MAX_IN_FLIGHT", 2), ThreadPoolExecutor(max_workers=2) as pool):
            first = pool.submit(work)
            self.assertTrue(entered.wait(1))
            second = pool.submit(work)
            self.assertTrue(pending.wait(1))
            release.set()
            self.assertTrue(in_work.wait(1))
            first.result(3)
            second.result(3)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(calls, ["gpu", "gpu"])

    def test_abandoned_probe_wait_is_bounded(self):
        self.gate.admit(remote._scope("asr", "http://gpu", "token"), 60)
        with (patch.object(remote, "circuit", self.gate),
              patch.object(remote.time, "monotonic", side_effect=[0, 0, 5]), patch.object(remote, "_probe") as check):
            with self.assertRaises(remote.CircuitOpen), remote.remote_attempt("asr", "http://gpu", "token", 60):
                self.fail("should fall back after bounded wait")
        check.assert_not_called()
