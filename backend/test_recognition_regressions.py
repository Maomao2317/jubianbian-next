"""Focused regressions for speaker ownership, cast identity and short evidence."""

import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from app import evidence, processing, providers, task_store
from app.errors import ArkError
from app.script import apply_batch_character_continuity, normalize_script, script_quality


def episode(characters, blocks, profiles=None):
    return normalize_script({
        "characters": characters,
        "characterProfiles": profiles or [],
        "scenes": [{"heading": "1-1 日 内 客厅", "location": "客厅", "blocks": blocks}],
    }, "第1集")


class RecognitionRegressions(unittest.TestCase):
    def test_boolean_lip_speaker_cannot_become_true_character(self):
        script = episode(["甲", True], [{"type": "dialogue", "speaker": "甲", "visibleSpeaker": True,
                                         "mouthMoving": True, "text": "我来了"}],
                         [{"name": "甲"}, {"name": True}])
        block = script["scenes"][0]["blocks"][0]
        self.assertNotIn("True", script["characters"])
        self.assertNotIn("visibleSpeaker", block)
        self.assertNotIn("True", str(script_quality(script, "test")["issues"]))

    def test_cut_to_listener_keeps_ongoing_speaker_and_not_os(self):
        script = episode(["甲", "乙"], [
            {"type": "dialogue", "speaker": "甲", "text": "我还没有说完", "startSec": 0, "endSec": 2},
            {"type": "dialogue", "speaker": "甲", "visibleSpeaker": "乙", "mouthMoving": False,
             "speechContinuity": True, "text": "你别打断我", "startSec": 2, "endSec": 3},
        ])
        block = script["scenes"][0]["blocks"][1]
        self.assertEqual((block["type"], block["speaker"]), ("dialogue", "甲"))
        self.assertFalse(any(issue["tag"] in {"OS漏标", "台词归属错"}
                             for issue in script_quality(script, "test")["issues"]))
        isolated = episode(["甲"], [{"type": "dialogue", "speaker": "甲", "mouthMoving": False,
                                     "text": "我在门外"}])
        self.assertEqual(isolated["scenes"][0]["blocks"][0]["type"], "dialogue")
        outside = episode(["甲"], [{"type": "dialogue", "speaker": "甲", "voKind": "offscreen",
                                    "text": "我在门外"}])
        self.assertEqual(outside["scenes"][0]["blocks"][0]["voKind"], "offscreen")

    def test_first_established_name_only_changes_cast_and_speaker_labels(self):
        first = episode(["周向东", "周守田"], [
            {"type": "dialogue", "speaker": "周守田", "text": "向东，过来"}],
            [{"name": "周向东"}, {"name": "周守田", "relationships": [{"target": "周向东", "relation": "父亲"}]}])
        second = episode(["向东爸", "丁大旺"], [
            {"type": "action", "text": "向东爸递出杯子给丁大旺"},
            {"type": "dialogue", "speaker": "向东爸", "text": "你爸来了"},
            {"type": "dialogue", "speaker": "丁大旺", "text": "爸？"}],
            [{"name": "向东爸"}, {"name": "丁大旺"}])
        third = episode(["周向东父亲", "丁大旺"], [
            {"type": "dialogue", "speaker": "周向东父亲", "text": "我在这"},
            {"type": "dialogue", "speaker": "丁大旺", "text": "父亲在这"}],
            [{"name": "周向东父亲"}, {"name": "丁大旺"}])
        apply_batch_character_continuity([first, second, third])
        self.assertEqual(second["scenes"][0]["characters"], ["周守田", "丁大旺"])
        self.assertEqual(second["scenes"][0]["blocks"][1]["speaker"], "周守田")
        self.assertEqual(second["scenes"][0]["blocks"][1]["text"], "你爸来了。")
        self.assertIn("向东爸", second["scenes"][0]["blocks"][0]["text"])
        self.assertEqual(third["scenes"][0]["blocks"][0]["speaker"], "周守田")
        self.assertEqual(third["scenes"][0]["blocks"][1]["speaker"], "丁大旺")
        self.assertIn("向东爸", normalize_script(second, "第2集")["scenes"][0]["blocks"][0]["text"])

    def test_relational_label_never_resolves_to_child_without_relation(self):
        first = episode(["周向东"], [{"type": "dialogue", "speaker": "周向东", "text": "你好"}])
        second = episode(["周向东父亲", "周向东哥哥"], [
            {"type": "dialogue", "speaker": "周向东父亲", "text": "你好"},
            {"type": "dialogue", "speaker": "周向东哥哥", "text": "你好"},
        ])
        apply_batch_character_continuity([first, second])
        self.assertEqual(second["scenes"][0]["blocks"][0]["speaker"], "周向东父亲")
        self.assertEqual(second["scenes"][0]["blocks"][1]["speaker"], "周向东哥哥")

    def test_named_relationship_owner_resolves_later_bare_kinship_label(self):
        first = episode(["林婉怡", "王浩"], [
            {"type": "dialogue", "speaker": "林婉怡", "text": "好"},
            {"type": "dialogue", "speaker": "王浩", "text": "婉怡，回家"},
        ], [
            {"name": "林婉怡"},
            {"name": "王浩", "relationships": [{"target": "林婉怡", "relation": "丈夫"}]},
        ])
        second = episode(["林婉怡", "丈夫"], [
            {"type": "dialogue", "speaker": "林婉怡", "text": "好"},
            {"type": "dialogue", "speaker": "丈夫", "text": "我在这"},
        ], [
            {"name": "林婉怡"},
            {"name": "丈夫", "relationships": [{"target": "林婉怡", "relation": "丈夫"}]},
        ])
        apply_batch_character_continuity([first, second])
        self.assertEqual(second["characters"], ["林婉怡", "王浩"])
        self.assertEqual(second["scenes"][0]["blocks"][1]["speaker"], "王浩")
        self.assertEqual(second["scenes"][0]["blocks"][1]["text"], "我在这。")

    def test_bare_kinship_label_requires_same_named_counterpart_and_role(self):
        first = episode(["林婉怡", "王浩", "李芸"], [
            {"type": "dialogue", "speaker": name, "text": "好"}
            for name in ("林婉怡", "王浩", "李芸")
        ], [{"name": "王浩", "relationships": [{"target": "林婉怡", "relation": "丈夫"}]}])
        second = episode(["李芸", "丈夫"], [
            {"type": "dialogue", "speaker": "李芸", "text": "别走"},
            {"type": "dialogue", "speaker": "丈夫", "text": "好"},
        ], [{"name": "丈夫", "relationships": [{"target": "李芸", "relation": "丈夫"}]}])
        apply_batch_character_continuity([first, second])
        self.assertEqual(second["scenes"][0]["blocks"][1]["speaker"], "丈夫")

    def test_blocking_review_includes_only_corroborated_missing_line_advisories(self):
        blocking = {"tag": "台词归属错", "severity": "P0", "scene": 1, "block": 1}
        supported = {"tag": "疑似漏台词片段", "severity": "P1", "ocrConfirmed": True}
        unsupported = {"tag": "疑似漏台词片段", "severity": "P1"}
        quality = {"issues": [blocking, supported, unsupported]}
        self.assertEqual(processing._automated_review_issues(quality), [blocking, supported])

    def test_name_first_established_in_second_episode_is_used_in_third(self):
        first = episode(["周向东"], [{"type": "dialogue", "speaker": "周向东", "text": "走"}])
        second = episode(["丁大旺"], [{"type": "dialogue", "speaker": "丁大旺", "text": "来了"}],
                         [{"name": "丁大旺", "aliases": ["大旺"]}])
        third = episode(["大旺"], [{"type": "dialogue", "speaker": "大旺", "text": "向东，等等"}])
        apply_batch_character_continuity([first, second, third])
        self.assertEqual(third["scenes"][0]["characters"], ["丁大旺"])
        self.assertEqual(third["scenes"][0]["blocks"][0]["speaker"], "丁大旺")
        self.assertEqual(third["scenes"][0]["blocks"][0]["text"], "向东，等等。")

    def test_short_asr_is_checked_at_its_own_occurrence(self):
        script = episode(["甲"], [{"type": "dialogue", "speaker": "甲", "text": "啊", "startSec": 0, "endSec": 0.2}])
        captured = {"sources": [{"kind": "audio", "status": "available", "items": [
            {"type": "transcript_segment", "text": "啊", "startSec": 5, "endSec": 5.2},
        ]}]}
        issues = [item for item in script_quality(script, "test", captured)["issues"]
                  if item["tag"] == "疑似漏台词片段"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["startSec"], 5)

    def test_animal_subtitle_with_speech_cue_needs_dialogue_review(self):
        script = episode(["老虎"], [
            {"type": "action", "text": "老虎开口说道", "startSec": 1, "endSec": 2},
            {"type": "screen_text", "screenType": "subtitle", "text": "啊", "startSec": 1.1, "endSec": 1.4},
        ], [{"name": "老虎", "species": "动画动物"}])
        issues = script_quality(script, "test")["issues"]
        self.assertIn("字幕对白遗漏", [issue["tag"] for issue in issues])
        self.assertTrue(processing._needs_automated_content_review({"issues": issues}))
        title = episode([], [{"type": "screen_text", "screenType": "title_card", "text": "老虎", "startSec": 1, "endSec": 2}])
        self.assertNotIn("字幕对白遗漏", [issue["tag"] for issue in script_quality(title, "test")["issues"]])

    def test_short_ocr_repeated_later_is_checked_at_its_own_time(self):
        script = episode([], [{"type": "screen_text", "text": "呵", "startSec": 0.3, "endSec": 0.6}])
        captured = {"sources": [{"kind": "ocr", "status": "available", "items": [
            {"text": "呵", "startSec": 0.3, "endSec": 0.6, "region": "subtitle_band"},
            {"text": "呵", "startSec": 0.5, "endSec": 0.7, "region": "subtitle_band"},
            {"text": "呵", "startSec": 3.0, "endSec": 3.2, "region": "subtitle_band"},
        ]}]}
        issues = [issue for issue in script_quality(script, "test", captured)["issues"]
                  if issue["tag"] == "疑似漏画面文字片段"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["startSec"], 3.0)

    def test_action_and_emotion_do_not_require_absent_metadata(self):
        script = episode(["甲", "乙"], [
            {"type": "action", "text": "甲把杯子递给乙", "emotionIntensity": 4,
             "emotionEvidence": "动作急促", "startSec": 1, "endSec": 2},
            {"type": "dialogue", "speaker": "乙", "text": "谢谢", "emotionIntensity": 1,
             "emotionEvidence": "语调平缓", "startSec": 2, "endSec": 3},
        ])
        tags = {issue["tag"] for issue in script_quality(script, "test")["issues"]}
        self.assertFalse(tags & {"道具断裂", "动作细节", "连续动作未拆分", "情绪变化丢失"})

    def test_dense_sampling_crop_and_repeated_observation(self):
        times = evidence._adaptive_ocr_times(1)
        self.assertGreaterEqual(len(times), 4)
        with patch.object(Path, "read_bytes", return_value=b"P5\n4 4\n255\n" + bytes(range(16))), \
                patch.object(Path, "write_bytes") as write:
            path = Path("frame.pgm")
            crop, pixels = evidence._subtitle_band(path)
            self.assertEqual(pixels, bytes(range(8, 16)))
            self.assertEqual(crop, Path("frame-subtitle.pgm"))
            write.assert_called_once_with(b"P5\n4 2\n255\n" + pixels)
        items = []
        previous = evidence._append_ocr_sample(items, "啊", 0, .25, None, region="subtitle_band")
        previous = evidence._append_ocr_sample(items, "啊", .25, .5, previous, region="subtitle_band")
        previous = evidence._append_ocr_sample(items, "", .5, .75, previous, region="subtitle_band")
        evidence._append_ocr_sample(items, "啊", .75, 1, previous, region="subtitle_band")
        self.assertEqual([(item["startSec"], item["endSec"]) for item in items], [(0, .5), (.75, 1)])

    def test_completed_prior_cast_context_includes_second_episode_first_name(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE tasks(id, user_id, batch_id, batch_index, created_at, title, status, result_json)")
        payloads = [episode(["周守田"], [{"type": "dialogue", "speaker": "周守田", "text": "好"}],
                            [{"name": "周守田"}]),
                    episode(["丁大旺"], [{"type": "dialogue", "speaker": "丁大旺", "text": "好"}],
                            [{"name": "丁大旺", "species": "动画动物"}])]
        connection.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)", [
            ("one", "owner", "batch", 1, "1", "第一集", "done", json.dumps(payloads[0])),
            ("two", "owner", "batch", 2, "2", "第二集", "done", json.dumps(payloads[1])),
            ("three", "owner", "batch", 3, "3", "第三集", "running", None),
        ])
        with patch.object(task_store, "db", return_value=connection):
            context = task_store.batch_character_context({"id": "three", "user_id": "owner", "batch_id": "batch"})
        self.assertEqual([p["name"] for p in context["characterProfiles"]], ["周守田", "丁大旺"])
        connection.close()

    def test_review_keeps_patch_in_issue_and_preserves_ocr_coverage(self):
        script = episode(["甲"], [{"type": "dialogue", "speaker": "甲", "text": "好", "startSec": 1, "endSec": 2},
                                   {"type": "dialogue", "speaker": "甲", "text": "来", "startSec": 7, "endSec": 8}])
        context = processing._automated_review_context(script, [{"scene": 1, "block": 1, "tag": "漏台词"}])
        self.assertEqual((context["ranges"][0]["startSec"], context["ranges"][0]["contextStartSec"]), (1, 0))
        repair = {"type": "dialogue", "speaker": "甲", "text": "过来", "startSec": 7, "endSec": 8}
        response = {"corrections": [{"sceneIndex": 0, "blockIndex": 1, "operation": "replace", "block": repair}]}
        with self.assertRaises(ArkError):
            providers._apply_ark_review({"output_text": json.dumps(response)}, context, 10)
        self.assertEqual(processing._review_regression_reason({"ocrCoverage": 100, "issues": []},
                                                             {"ocrCoverage": 50, "issues": []}), "lower_ocrCoverage")


if __name__ == "__main__":
    unittest.main()
