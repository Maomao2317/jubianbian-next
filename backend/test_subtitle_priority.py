from copy import deepcopy
import unittest

from app import processing, providers
from app.script import (normalize_script, restore_evidence_boundary_segments,
                        restore_short_transcript_segments, restore_transcript_gap_segments,
                        script_quality, script_to_markdown, subtitle_aligned_evidence)


class SubtitlePriorityTests(unittest.TestCase):
    def fixture(self, audio="这个人是畜生", subtitle="这个人是坏人", dialogue=None, **fields):
        raw = {"characters": ["甲"], "scenes": [{
            "heading": "1-1 日 内 客厅", "location": "客厅", "characters": ["甲"], "blocks": [
                {"type": "dialogue", "speaker": "甲", "text": dialogue or audio,
                 "performance": "愤怒", "startSec": 1, "endSec": 3, **fields},
                {"type": "screen_text", "screenType": "subtitle", "text": subtitle,
                 "confidence": "high", "startSec": 1, "endSec": 3},
            ]}]}
        evidence = {"sources": [
            {"kind": "audio", "status": "available", "items": [
                {"type": "transcript_segment", "text": audio, "startSec": 1, "endSec": 3,
                 "words": [{"text": "啊", "startSec": 2, "endSec": 2.1}]},
            ]},
            {"kind": "ocr", "status": "available", "items": [
                {"type": "screen_text", "text": subtitle, "startSec": 1.5, "endSec": 1.75,
                 "region": "subtitle_band"},
            ]},
        ]}
        return normalize_script(raw, "第1集"), evidence

    def evaluate(self, script, evidence):
        return processing._evaluate_quality(script, "test", evidence,
                                            {"band": "simple", "score": 0, "reasons": []})[0]

    def test_user_example_changes_words_and_keeps_speaker_emotion_and_evidence(self):
        script, evidence = self.fixture()
        original = deepcopy(evidence)
        quality = self.evaluate(script, evidence)
        block = script["scenes"][0]["blocks"][0]
        self.assertEqual(block["text"], "这个人是坏人。")
        self.assertEqual(block["speaker"], "甲")
        self.assertEqual(block["performance"], "愤怒")
        self.assertEqual(block["textSource"], "subtitle")
        self.assertEqual(quality["subtitlePrioritySegments"], 1)
        self.assertEqual(evidence, original)
        renormalized = normalize_script(script, "第1集")
        self.assertEqual(renormalized["scenes"][0]["blocks"][0]["text"], block["text"])
        exported = script_to_markdown({"title": "第1集", "result": renormalized})
        self.assertIn("这个人是坏人", exported)
        self.assertNotIn("畜生", exported)
        self.assertNotIn("textSource", exported)

    def test_audio_repairs_do_not_restore_words_omitted_by_corresponding_subtitle(self):
        for audio, subtitle in [("啊这个人是坏人", "这个人是坏人"),
                                ("这个人真的是坏人", "这个人是坏人")]:
            with self.subTest(audio=audio):
                script, evidence = self.fixture(audio, subtitle, subtitle, textSource="subtitle")
                for repair in (restore_evidence_boundary_segments, restore_transcript_gap_segments,
                               restore_short_transcript_segments):
                    repair(script, evidence)
                self.assertEqual(script["scenes"][0]["blocks"][0]["text"], subtitle + "。")

    def test_quality_accepts_confirmed_subtitle_without_waiving_other_missing_audio(self):
        script, evidence = self.fixture("这个人是畜生你千万别相信他", "这个人是坏人你千万别相信他",
                                        "这个人是坏人你千万别相信他", textSource="subtitle")
        quality = script_quality(script, "test", evidence)
        self.assertEqual(quality["transcriptCoverage"], 100)
        self.assertEqual(quality["dialogueEvidencePrecision"], 100)
        evidence["sources"][0]["items"].append({"type": "transcript_segment", "text": "赶紧离开这里",
                                               "startSec": 10, "endSec": 11})
        issues = script_quality(script, "test", evidence)["issues"]
        self.assertTrue(any(i["tag"] == "疑似漏台词片段" and i["startSec"] == 10 for i in issues))

    def test_vo_uses_subtitle_words_but_keeps_voice_classification(self):
        script, evidence = self.fixture(type="vo", voKind="os", textSource="audio")
        self.evaluate(script, evidence)
        block = script["scenes"][0]["blocks"][0]
        self.assertEqual((block["type"], block["voKind"], block["speaker"]), ("vo", "os", "甲"))
        self.assertEqual(normalize_script(script, "第1集")["scenes"][0]["blocks"][0]["text"], "这个人是坏人。")

    def test_partial_subtitle_cannot_erase_rest_of_utterance(self):
        script, evidence = self.fixture("这个人是畜生你千万别相信他", "这个人是坏人")
        script["scenes"][0]["blocks"][1]["endSec"] = 1.75
        original = script["scenes"][0]["blocks"][0]["text"]
        self.evaluate(script, evidence)
        self.assertEqual(script["scenes"][0]["blocks"][0]["text"], original)

    def test_title_name_cards_and_unreliable_captions_never_override(self):
        for fields in ({"screenType": "title_card"}, {"screenType": "other"}, {"screenType": "system"},
                       {"confidence": "low"}, {"uncertain": True}):
            with self.subTest(fields=fields):
                script, evidence = self.fixture()
                script["scenes"][0]["blocks"][1].update(fields)
                self.evaluate(script, evidence)
                self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "这个人是畜生。")

    def test_ocr_typo_or_wrong_time_cannot_authorize_override(self):
        for fields in ({"text": "这个人是怀人"}, {"startSec": 5, "endSec": 5.25}):
            script, evidence = self.fixture()
            evidence["sources"][1]["items"][0].update(fields)
            aligned, count = subtitle_aligned_evidence(script, evidence, update_script=True)
            self.assertEqual(count, 0)
            self.assertIs(aligned, evidence)
            self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "这个人是畜生。")

    def test_model_source_claim_alone_does_not_exempt_fidelity_checks(self):
        script, evidence = self.fixture("这个人是畜生你千万别相信他", "完全无关的一句台词",
                                        "完全无关的一句台词", textSource="subtitle")
        evidence["sources"][1]["items"] = []
        quality = script_quality(script, "test", evidence)
        self.assertEqual(quality["subtitlePrioritySegments"], 0)
        self.assertTrue(any(i["tag"] == "音频对白不一致" for i in quality["issues"]))

    def test_two_possible_speakers_are_left_for_review(self):
        script, evidence = self.fixture()
        script["scenes"][0]["blocks"].append({"type": "dialogue", "speaker": "乙", "text": "不要这样说",
                                              "startSec": 2, "endSec": 3})
        _, count = subtitle_aligned_evidence(script, evidence, update_script=True)
        self.assertEqual(count, 0)
        self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "这个人是畜生。")

    def test_repeated_line_at_other_time_does_not_cover_audio(self):
        script, evidence = self.fixture(dialogue="这个人是坏人", textSource="subtitle")
        evidence["sources"][0]["items"].append({"type": "transcript_segment", "text": "畜生", "startSec": 9, "endSec": 10})
        issues = script_quality(script, "test", evidence)["issues"]
        self.assertTrue(any(i["tag"] == "疑似漏台词片段" and i["startSec"] == 9 for i in issues))

    def test_short_whole_word_substitution_requires_explicit_and_confirmed_choice(self):
        script, evidence = self.fixture("畜生", "坏人", "坏人", textSource="subtitle")
        aligned, count = subtitle_aligned_evidence(script, evidence)
        self.assertEqual(count, 1)
        self.assertEqual(aligned["sources"][0]["items"][0]["text"], "坏人")

    def test_main_and_review_share_subtitle_policy(self):
        for context in (None, {"scope": "issue_patches", "draft": {}, "ranges": [], "issues": []}):
            prompt = providers.ark_prompt("第1集", 10, {}, context)
            self.assertIn(providers.SUBTITLE_PRIORITY_RULE, prompt)
        schema = providers.ark_review_format()["schema"]["properties"]["corrections"]["items"]
        self.assertIn("textSource", schema["properties"]["block"]["properties"])

    def test_review_cannot_change_confirmed_subtitle_back_to_audio(self):
        script, evidence = self.fixture(dialogue="这个人是坏人", textSource="subtitle")
        context = processing._automated_review_context(script, [{"tag": "漏词", "scene": 1, "block": 1}])
        self.assertEqual(context["draft"]["scenes"][0]["blocks"][0]["textSource"], "subtitle")
        block = script["scenes"][0]["blocks"][0]
        for key in ("text", "rawText", "sourceText", "finalText"):
            block[key] = "这个人是畜生"
        self.evaluate(script, evidence)
        self.assertEqual(block["text"], "这个人是坏人。")

    def test_only_matching_span_changes_inside_longer_dialogue(self):
        script, evidence = self.fixture(dialogue="听我说，这个人是畜生，别靠近。")
        block = script["scenes"][0]["blocks"][0]
        block.update(startSec=0, endSec=4)
        self.evaluate(script, evidence)
        self.assertEqual(block["text"], "听我说，这个人是坏人，别靠近。")

    def test_multiple_subtitle_spans_in_long_block_keep_both_corrections(self):
        script, evidence = self.fixture(dialogue="这个人是畜生，他真的该死。")
        block = script["scenes"][0]["blocks"][0]
        block["endSec"] = 5
        script["scenes"][0]["blocks"].append({"type": "screen_text", "screenType": "subtitle",
                                              "text": "他真的可恶", "startSec": 3.1, "endSec": 5})
        evidence["sources"][0]["items"].append({"type": "transcript_segment", "text": "他真的该死",
                                               "startSec": 3.1, "endSec": 5})
        evidence["sources"][1]["items"].append({"text": "他真的可恶", "startSec": 3.5, "endSec": 3.75})
        self.evaluate(script, evidence)
        self.assertEqual(block["text"], "这个人是坏人，他真的可恶。")


if __name__ == "__main__":
    unittest.main()
