"""Real OCR boundary contamination must never become spoken dialogue."""

from copy import deepcopy
import unittest

from app.script import normalize_script, restore_evidence_boundary_segments, script_to_markdown, script_quality


class OcrDialogueIntegrityTests(unittest.TestCase):
    def fixture(self, clean, ocr, *, caption=None, voice=False):
        blocks = [{"type": "vo" if voice else "dialogue", "speaker": "甲",
                   "text": clean, "startSec": 1, "endSec": 3, "voKind": "offscreen"}]
        if caption is not None:
            blocks.append({"type": "screen_text", "screenType": "subtitle", "text": caption,
                           "startSec": 1, "endSec": 3, "confidence": "high"})
        script = normalize_script({"characters": ["甲"], "scenes": [{
            "heading": "1-1 日 内 客厅", "location": "客厅", "blocks": blocks,
        }]}, "第1集")
        evidence = {"sources": [{"kind": "ocr", "status": "available", "items": [{
            "type": "screen_text", "text": ocr, "startSec": 1.5, "endSec": 1.75,
            "region": "subtitle_band",
        }]}]}
        return script, evidence

    def test_real_examples_do_not_overwrite_clean_dialogue_or_vo(self):
        examples = [
            ("水生你最近真不一样了", ">> la 水生你最近真不一样了。"),
            ("不说就算了", "有NS a～～不说就算了Vh；。"),
            ("十几块的", "Sal a | b % ce — =。= m。! 时 十几块的。"),
            ("以前摸了点", "a Ne ht 以前摸了点 1机。"),
        ]
        for voice in (False, True):
            for clean, ocr in examples:
                with self.subTest(clean=clean, voice=voice):
                    script, evidence = self.fixture(clean, ocr, voice=voice)
                    original, original_evidence = deepcopy(script), deepcopy(evidence)
                    self.assertEqual(restore_evidence_boundary_segments(script, evidence)["segments"], 0)
                    self.assertEqual(script, original)
                    self.assertEqual(evidence, original_evidence)

    def test_unclassified_or_partial_ocr_is_not_authority_even_when_chinese(self):
        for caption in (None, "今天决定离开", "我今天决定离开"):
            script, evidence = self.fixture("今天决定离开", "我今天决定离开还有事情", caption=caption)
            restore_evidence_boundary_segments(script, evidence)
            self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "今天决定离开。")

    def test_symbols_dropped_by_comparison_cannot_authorize_full_ocr_copy(self):
        script, evidence = self.fixture("今天决定离开", ">>> 我今天决定离开 || %", caption="我今天决定离开")
        restore_evidence_boundary_segments(script, evidence)
        self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "今天决定离开。")

    def test_bad_caption_type_confidence_or_time_cannot_authorize_recovery(self):
        for fields in ({"screenType": "title_card"}, {"uncertain": True}, {"confidence": "low"},
                       {"startSec": 5, "endSec": 6}):
            script, evidence = self.fixture("今天决定离开", "我今天决定离开", caption="我今天决定离开")
            script["scenes"][0]["blocks"][1].update(fields)
            restore_evidence_boundary_segments(script, evidence)
            self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "今天决定离开。")

    def test_confirmed_english_brands_prices_and_symbols_are_preserved(self):
        for clean, complete in [("用iPhone 16付款￥19.99", "我用iPhone 16付款￥19.99"),
                                ("say Hello", "Please say Hello"), ("公式是a+b=3", "这个公式是a+b=3")]:
            script, evidence = self.fixture(clean, complete, caption=complete)
            original = script["scenes"][0]["blocks"][0]["text"]
            self.assertEqual(restore_evidence_boundary_segments(script, evidence)["segments"], 1)
            block = script["scenes"][0]["blocks"][0]
            self.assertEqual(block["evidenceRecoveryOriginal"]["text"], original)
            again = normalize_script(script, "第1集")
            self.assertEqual(again["scenes"][0]["blocks"][0]["text"], block["text"])
            self.assertTrue(again["scenes"][0]["blocks"][0]["subtitleRecovered"])
            self.assertIn(block["text"], script_to_markdown({"title": "第1集", "result": again}))

    def test_asr_boundary_recovery_still_restores_original_words(self):
        script, evidence = self.fixture("今天决定离开", "irrelevant")
        evidence["sources"] = [{"kind": "audio", "status": "available", "items": [{
            "type": "transcript_segment", "text": "我今天决定离开", "startSec": 1.1, "endSec": 2.8,
        }]}]
        self.assertEqual(restore_evidence_boundary_segments(script, evidence)["segments"], 1)
        self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "我今天决定离开。")

    def test_only_exact_timed_asr_supported_subtitle_avoids_duplicate_text_warning(self):
        script, evidence = self.fixture("我今天决定离开", "我今天决定离开")
        evidence["sources"].append({"kind": "audio", "status": "available", "items": [{
            "type": "transcript_segment", "text": "我今天决定离开", "startSec": 1, "endSec": 3}]})
        before = deepcopy(evidence)
        quality = script_quality(script, "test", evidence)
        self.assertEqual(quality["ocrCoverage"], 100)
        self.assertFalse(any(issue["tag"] == "疑似漏画面文字片段" for issue in quality["issues"]))
        self.assertEqual(evidence, before)
        for override, audio in (({"screenType": "name_tag"}, {}),
                                ({"region": "full"}, {}),
                                ({"text": "我今天决定离开姓名条"}, {}),
                                ({}, {"startSec": 7, "endSec": 8}),
                                ({"text": "甲"}, {"text": "甲"})):
            changed = deepcopy(evidence)
            changed["sources"][0]["items"][0].update(override)
            changed["sources"][1]["items"][0].update(audio)
            with self.subTest(override=override, audio=audio):
                self.assertTrue(any(issue["tag"] == "疑似漏画面文字片段"
                                    for issue in script_quality(script, "test", changed)["issues"]))

    def test_repeated_subtitle_later_still_flags_missing_occurrence(self):
        script, evidence = self.fixture("我今天决定离开", "我今天决定离开")
        evidence["sources"][0]["items"].append({
            **evidence["sources"][0]["items"][0], "startSec": 7, "endSec": 8})
        evidence["sources"].append({"kind": "audio", "status": "available", "items": [{
            "type": "transcript_segment", "text": "我今天决定离开", "startSec": 1, "endSec": 3}]})
        missing = [issue for issue in script_quality(script, "test", evidence)["issues"]
                   if issue["tag"] == "疑似漏画面文字片段"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["startSec"], 7)


if __name__ == "__main__":
    unittest.main()
