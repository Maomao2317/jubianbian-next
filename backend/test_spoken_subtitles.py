"""Speech must survive generation, stored-result reads and exports as speech."""
from copy import deepcopy
import json
import unittest

from app import processing, providers
from app.script import normalize_script, script_to_markdown
from app.task_store import row_to_task


class SpokenSubtitleTests(unittest.TestCase):
    def fixture(self, **fields):
        raw = {"characters": ["周青"], "scenes": [{"heading": "1-1 日 外 台阶", "location": "台阶",
            "blocks": [{"type": "screen_text", "screenType": "subtitle", "text": "请还钱",
                        "startSec": 10, "endSec": 11, **fields}]}]}
        evidence = {"sources": [{"kind": "audio", "status": "available", "items": [
            {"type": "transcript_segment", "text": "请还钱", "startSec": 10, "endSec": 11}]}]}
        return raw, evidence

    def test_visible_speech_and_phone_voice_are_classified_without_guessing(self):
        for fields, kind, speaker in [({"mouthMoving": True, "visibleSpeaker": "周青"}, "dialogue", "周青"),
                                     ({"voiceSource": "phone", "speaker": "陌生男声"}, "vo", "陌生男声"),
                                     ({}, "vo", "未知说话人")]:
            raw, evidence = self.fixture(**fields)
            original = deepcopy(raw)
            script = normalize_script(raw, "测试", evidence)
            block = script["scenes"][0]["blocks"][0]
            self.assertEqual((block["type"], block["speaker"], block["text"]), (kind, speaker, "请还钱。"))
            if fields.get("voiceSource"):
                self.assertEqual(block["voKind"], "phone")
            self.assertEqual(raw, original)
            self.assertEqual(normalize_script(script, "测试", evidence), script)

    def test_real_screen_text_ocr_and_wrong_time_never_become_speech(self):
        for fields, source in [({"screenType": "other"}, "audio"), ({"screenType": "system"}, "audio"),
                               ({"screenType": "title_card"}, "audio"), ({}, "ocr")]:
            raw, evidence = self.fixture(**fields)
            evidence["sources"][0]["kind"] = source
            self.assertEqual(normalize_script(raw, "测试", evidence)["scenes"][0]["blocks"][0]["type"], "screen_text")
        for changed in ({"startSec": 20, "endSec": 21}, {"confidence": "low"}, {"text": "请还款"}):
            raw, evidence = self.fixture()
            evidence["sources"][0]["items"][0].update(changed)
            self.assertEqual(normalize_script(raw, "测试", evidence)["scenes"][0]["blocks"][0]["type"], "screen_text")

    def test_repeated_short_lines_keep_occurrences_but_not_duplicate_caption_frames(self):
        raw, evidence = self.fixture(text="还钱")
        block = raw["scenes"][0]["blocks"][0]
        raw["scenes"][0]["blocks"] += [dict(block), {**block, "startSec": 12, "endSec": 13}]
        evidence["sources"][0]["items"] = [
            {"type": "transcript_segment", "text": "还钱", "startSec": n, "endSec": n+1} for n in (10, 12)]
        script = normalize_script(raw, "测试", evidence)
        shown = [b for b in script["scenes"][0]["blocks"] if not b.get("suppressInOutput")]
        self.assertEqual([b["type"] for b in shown], ["vo", "vo"])
        self.assertEqual([b["startSec"] for b in shown], [10, 12])

    def test_existing_dialogue_caption_remains_internal(self):
        raw, evidence = self.fixture()
        raw["scenes"][0]["blocks"].insert(0, {"type": "dialogue", "speaker": "周青", "text": "请还钱",
                                                        "startSec": 10, "endSec": 11})
        script = normalize_script(raw, "测试", evidence)
        self.assertTrue(script["scenes"][0]["blocks"][1].get("suppressInOutput"))
        self.assertEqual(script_to_markdown({"title": "测试", "result": script}).count("请还钱"), 1)

    def test_unknown_voice_flag_alone_and_reset_episode_clocks_do_not_prove_speech(self):
        raw, _ = self.fixture(voKind="unknown")
        self.assertEqual(normalize_script(raw, "测试")["scenes"][0]["blocks"][0]["type"], "screen_text")
        raw, evidence = self.fixture()
        raw["scenes"].append({**deepcopy(raw["scenes"][0]), "heading": "2-1 日 外 台阶"})
        self.assertTrue(all(s["blocks"][0]["type"] == "screen_text" for s in normalize_script(raw, "测试", evidence)["scenes"]))

    def test_clear_different_spoken_words_are_not_duplicated_from_captions(self):
        raw, evidence = self.fixture()
        raw["scenes"][0]["blocks"].insert(0, {"type": "dialogue", "speaker": "周青", "text": "把钱还我",
                                                        "startSec": 10, "endSec": 11})
        script = normalize_script(raw, "测试", evidence)
        self.assertEqual(sum(b["type"] in {"vo", "dialogue"} for b in script["scenes"][0]["blocks"]), 1)
        self.assertTrue(script["scenes"][0]["blocks"][1].get("suppressInOutput"))

    def test_generation_quality_historical_api_and_standalone_export_share_repair(self):
        raw, evidence = self.fixture()
        script = normalize_script(raw, "测试")
        processing._evaluate_quality(script, "test", evidence, {"band": "simple", "score": 0, "reasons": []})
        self.assertEqual(script["scenes"][0]["blocks"][0]["type"], "vo")
        row = {"id": "fixture", "title": "测试", "result_json": json.dumps(raw),
               "evidence_json": json.dumps(evidence)}
        task = row_to_task(row)
        self.assertEqual(task["result"]["scenes"][0]["blocks"][0]["type"], "vo")
        for task in (task, {"title": "测试", "result": raw, "evidence": evidence}):
            exported = script_to_markdown(task)
            self.assertIn("VO：", exported)
            self.assertNotIn("【字幕", exported)
            self.assertIn("请还钱", exported)

    def test_main_and_review_prompts_require_speech_and_distinguish_messages(self):
        for context in (None, {"scope": "issue_patches", "draft": {}, "ranges": [], "issues": []}):
            prompt = providers.ark_prompt("测试", 30, {}, context)
            self.assertIn("不能只输出screen_text", prompt)
            self.assertIn("手机短信", prompt)


if __name__ == "__main__":
    unittest.main()
