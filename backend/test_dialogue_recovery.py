import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import dialogue_recovery as recovery, evidence as collector, processing, providers
from app.errors import ArkError


def block(kind, text, start, end, **extra):
    return dict(type=kind, text=text, startSec=start, endSec=end, **extra)


def script(*blocks):
    return {"title": "test", "scenes": [{"blocks": list(blocks)}]}


def evidence(*items):
    return {"sources": [{"kind": "audio", "status": "available", "items": list(items)}]}


def quality(*issues, coverage=80):
    return {"blockingIssues": list(issues), "issues": list(issues), "transcriptCoverage": coverage}


def issue(tag="疑似漏台词片段", start=10, end=12):
    return dict(tag=tag, severity="P0", startSec=start, endSec=end)


class MemoryCache:
    def __init__(self):
        self.data = {}

    def load(self, key):
        return copy.deepcopy(self.data.get(key))

    def save(self, key, value):
        self.data[key] = copy.deepcopy(value)


class EvidenceRiskTests(unittest.TestCase):
    def captions(self):
        return [block("screen_text", text, start, start + 2, screenType="subtitle") for start, text in (
            (116, "上一世我的直属上司"), (118, "靠着裙带关系当上主管"), (120, "心胸狭窄处处打压我"))]

    def test_actions_cover_video_but_not_the_audio_blind_spot(self):
        data = script(block("action", "走进房间", 0, 300), block("dialogue", "我回来了", 4, 6))
        found = recovery.dialogue_evidence_issues(data, evidence(block("transcript_segment", "其他声音", 115, 120)))
        self.assertEqual(found[0]["tag"], "语音证据盲区")
        self.assertEqual(found[0]["severity"], "P1")

    def test_sentence_sequence_requires_listening_not_automatic_conversion(self):
        data = script(*self.captions())
        before = copy.deepcopy(data)
        found = recovery.dialogue_evidence_issues(data, evidence(block("transcript_segment", "含混原声", 115, 123)))
        self.assertEqual([row["tag"] for row in found], ["字幕语音待核实"])
        self.assertEqual((found[0]["startSec"], found[0]["endSec"]), (116, 122))
        self.assertEqual(data, before)

    def test_titles_names_sms_repeated_caption_and_existing_speech_do_not_trigger(self):
        audio = evidence(block("transcript_segment", "其他声音", 110, 130))
        for kind in ("other", "title_card", "sms"):
            captions = self.captions()
            for caption in captions:
                caption["screenType"] = kind
            self.assertEqual(recovery.dialogue_evidence_issues(script(*captions), audio), [])
        captions = self.captions()
        for caption in captions:
            caption["text"] = "重复的一句画面字幕"
        self.assertEqual(recovery.dialogue_evidence_issues(script(*captions), audio), [])
        spoken = [dict(row, type="vo") for row in self.captions()]
        self.assertEqual(recovery.dialogue_evidence_issues(script(*self.captions(), *spoken), audio), [])

    def test_windows_are_bounded_coalesced_and_prioritize_subtitle_risk(self):
        issues = [issue(start=i * 30, end=i * 30 + 2) for i in range(8)]
        issues += [issue("字幕语音待核实", 180, 187), issue(start=181, end=188)]
        windows = recovery.recovery_windows(issues, 300)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0], {"startSec": 178, "endSec": 190})
        self.assertLessEqual(sum(row["endSec"] - row["startSec"] for row in windows), 72)


class AudioRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "source.mp4"
        self.path.touch()
        self.script = script(block("vo", "这是一句之前漏识别的台词", 10, 12))
        self.evidence = evidence(block("transcript_segment", "原本台词", 80, 82))
        self.cache = MemoryCache()

    def run_retry(self, response=None, error=None):
        with (patch.object(collector, "ASR_REMOTE_URL", "private-worker"),
              patch.object(collector, "_decode_audio_pcm", return_value=b"pcm") as decode,
              patch("app.remote_asr.request_asr", return_value=response, side_effect=error) as remote):
            result = recovery.recover_audio_blind_spots(self.path, 100, self.script, self.evidence, self.cache)
        return result, decode, remote

    def test_clip_timestamps_words_and_raw_evidence_are_preserved(self):
        before = copy.deepcopy(self.evidence)
        item = block("transcript_segment", "这是一句之前漏识别的台词", 2, 4,
                     words=[dict(text="这是一句之前漏识别的台词", startSec=2, endSec=4)])
        result, decode, remote = self.run_retry({"items": [item]})
        decode.assert_called_once_with(self.path, start=8, duration=6)
        remote.assert_called_once_with(b"pcm", 6)
        recovered = result["sources"][0]["items"][0]
        self.assertEqual((recovered["startSec"], recovered["endSec"]), (10, 12))
        self.assertEqual(recovered["words"][0]["startSec"], 10)
        self.assertEqual(self.evidence, before)
        again, _, remote = self.run_retry({"items": []})
        remote.assert_not_called()
        self.assertEqual(again["sources"], result["sources"])

    def test_failure_and_empty_output_are_cached_not_fabricated(self):
        for error in (RuntimeError("unavailable"), None):
            self.cache = MemoryCache()
            result, _, remote = self.run_retry({"items": []}, error)
            self.assertEqual(result["sources"], self.evidence["sources"])
            self.run_retry({"items": []})[2].assert_not_called()
            self.assertEqual(remote.call_count, 1)

    def test_task_wide_limit_survives_changed_suspect_ranges(self):
        self.cache.save("dialogue_recovery_state_v1", {"attempts": [
            {"startSec": start, "endSec": start + 5} for start in (20, 40, 60)]})
        self.run_retry({"items": []})[2].assert_not_called()

    def test_interrupted_admission_is_not_repeated(self):
        self.cache.save("dialogue_recovery_state_v1", {"attempts": [{"startSec": 8, "endSec": 14}]})
        result, _, remote = self.run_retry({"items": []})
        remote.assert_not_called()
        self.assertEqual(result["dialogueRecovery"]["attempts"][0]["status"], "interrupted")

    def test_unconfirmed_overlap_and_out_of_clip_items_are_not_added(self):
        self.evidence["sources"][0]["items"].append(block("transcript_segment", "原始录音证据", 10, 12))
        self.script = script(*EvidenceRiskTests().captions())
        self.evidence["sources"][0]["items"].append(block("transcript_segment", "原始录音证据", 114, 124))
        with (patch.object(collector, "ASR_REMOTE_URL", "private-worker"),
              patch.object(collector, "_decode_audio_pcm", return_value=b"pcm"),
              patch("app.remote_asr.request_asr", return_value={"items": [
                  block("transcript_segment", "未经字幕确认的冲突", 2, 4),
                  block("transcript_segment", "超出切片", 0, 1000)]})):
            result = recovery.recover_audio_blind_spots(self.path, 300, self.script, self.evidence, self.cache)
        self.assertCountEqual(result["sources"][0]["items"], self.evidence["sources"][0]["items"])

    def test_no_remote_means_no_new_paid_fallback(self):
        with patch.object(collector, "ASR_REMOTE_URL", ""), patch.object(collector, "_decode_audio_pcm") as decode:
            self.assertIs(recovery.recover_audio_blind_spots(self.path, 100, self.script, self.evidence), self.evidence)
        decode.assert_not_called()

    def test_overlapping_asr_may_confirm_a_spoken_subtitle_without_changing_old_words(self):
        self.script = script(*EvidenceRiskTests().captions())
        self.evidence = evidence(block("transcript_segment", "含混原始记录", 114, 124))
        with (patch.object(collector, "ASR_REMOTE_URL", "private-worker"),
              patch.object(collector, "_decode_audio_pcm", return_value=b"pcm"),
              patch("app.remote_asr.request_asr", return_value={"items": [
                  block("transcript_segment", "上一世我的直属上司", 2, 4)]})):
            result = recovery.recover_audio_blind_spots(self.path, 300, self.script, self.evidence, self.cache)
        self.assertEqual(len(result["sources"][0]["items"]), 2)
        self.assertIn(self.evidence["sources"][0]["items"][0], result["sources"][0]["items"])
        candidate = copy.deepcopy(self.script)
        self.assertEqual(processing.restore_spoken_subtitles(candidate, result), 1)
        self.assertEqual(candidate["scenes"][0]["blocks"][0]["type"], "vo")


class ReviewProgressTests(unittest.TestCase):
    def test_no_op_and_cosmetic_edits_do_not_count_as_repairs(self):
        q = quality(issue())
        self.assertEqual(processing._review_acceptance_reason(q, q, {}, {}), "no_effective_change")
        self.assertEqual(processing._review_acceptance_reason(q, q, {}, {"title": "new"}), "no_quality_progress")
        shifted = quality({**issue(), "block": 17, "description": "rewritten"})
        self.assertEqual(processing._review_acceptance_reason(q, shifted, {}, {"title": "new"}), "no_quality_progress")
        moved = quality(issue(start=11, end=13))
        self.assertEqual(processing._review_acceptance_reason(q, moved, {}, {"title": "new"}), "no_quality_progress")

    def test_real_progress_is_accepted_but_regressions_are_not(self):
        before = quality(issue(), issue(start=40, end=42))
        after = quality(issue())
        self.assertIsNone(processing._review_acceptance_reason(before, after, {}, {"changed": True}))
        self.assertIsNone(processing._review_acceptance_reason(before, quality(coverage=90), {}, {"changed": True}))
        self.assertEqual(processing._review_acceptance_reason(before, quality(issue(), coverage=50), {}, {}), "lower_transcriptCoverage")

    def test_budget_uses_physical_envelope_not_sum_of_disjoint_ranges(self):
        context = {"scope": "issue_patches", "ranges": [issue(start=10, end=12), issue(start=150, end=152)]}
        self.assertEqual(processing._review_scope_seconds(context, 300), 146)

    def test_required_clip_never_falls_back_to_full_paid_video(self):
        context = {"scope": "issue_patches", "requirePhysicalClip": True, "ranges": [issue()]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.mp4"
            path.touch()
            with (patch.object(providers, "_recognize_review_clip", side_effect=ArkError("encoding", provider_code="review_clip_unavailable")),
                  patch.object(providers, "ark_prepare_video") as prepare):
                with self.assertRaises(ArkError):
                    providers.ark_recognize(path, "test", 300, {}, "turbo", review_context=context)
                prepare.assert_not_called()

    def test_encoded_required_clip_is_accepted_without_recursive_clipping_or_paid_retry(self):
        draft = script(block("dialogue", "原始台词", 10, 12, speaker="甲"))
        context = processing._automated_review_context(draft, [issue()])
        context["requirePhysicalClip"] = True
        local = providers._local_review_context(context, 8, 14, 300)
        payload = {"output_text": json.dumps({"corrections": []}), "usage": {"total_tokens": 10}}
        for error in (None, ArkError("invalid", provider_code="invalid_script_response")):
            with (self.subTest(error=error),
                  patch.object(providers, "_ark_response_with_url_retry", return_value=payload, side_effect=error) as response,
                  patch.object(providers, "_recognize_review_clip") as encode,
                  patch.object(providers, "ARK_RESPONSE_CREATE_ATTEMPTS", 3)):
                args = (Path("slice.mp4"), "test", 6, {}, "turbo")
                kwargs = dict(review_context=local, prepared_video={"api_key": "fixture", "file_id": "fixture"})
                if error:
                    with self.assertRaises(ArkError):
                        providers.ark_recognize(*args, **kwargs)
                else:
                    result, _ = providers.ark_recognize(*args, **kwargs)
                    self.assertEqual(result["scenes"][0]["blocks"][0]["text"], "原始台词。")
                response.assert_called_once()
                encode.assert_not_called()


class FocusedReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_op_is_bounded_and_all_paid_attempts_are_accounted(self):
        draft = script(block("dialogue", "原始台词", 10, 12))
        q = quality(issue(), issue(start=100, end=102), issue(start=200, end=202))
        calls = []

        def recognize(*args, **kwargs):
            calls.append(kwargs["review_context"])
            return draft, {"api_cost_rmb": .01, "calls": [{"purpose": "test"}]}

        with (patch.object(processing, "_evaluate_quality", return_value=(q, False, q["issues"])),
              patch.object(processing, "normalize_script", side_effect=lambda value, _: value)):
            result, final_q, usage, attempts = await processing._focused_dialogue_review(
                {"title": "test", "credits_used": 10}, Path("unused"), 300, draft, q, {},
                {"band": "complex", "score": 0, "reasons": []}, {}, "turbo", {}, recognize)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["requirePhysicalClip"] for call in calls))
        self.assertTrue(all(not attempt["accepted"] for attempt in attempts))
        self.assertEqual(usage["api_cost_rmb"], .02)
        self.assertEqual(len(usage["calls"]), 2)
        self.assertEqual(result, draft)
        self.assertEqual(final_q, q)

    async def test_budget_exhaustion_prevents_any_model_call(self):
        with patch.object(processing, "_run_model_call") as call:
            _, _, _, attempts = await processing._focused_dialogue_review(
                {"title": "test", "credits_used": 1}, Path("unused"), 300, {}, quality(issue()), {}, {},
                {"api_cost_rmb": 100}, "turbo", {}, None)
        call.assert_not_called()
        self.assertEqual(attempts[0]["reason"], "minimum_margin_would_be_breached")


if __name__ == "__main__":
    unittest.main()
