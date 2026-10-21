"""Overlapping action/speech scenes and suppressed caption-only shells."""
from collections import Counter
from copy import deepcopy
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.content_coverage import merge_gap_repair, normalize_additive_repair
from app.script import merge_continuous_scenes, normalize_script, screenplay_output_scenes, script_to_markdown
from app import recognition_cache


def scene(index, start, end, *, kind="dialogue", text=None, **fields):
    return {"heading": f"1-{index} 日 内 {fields.get('location', '客厅')}",
            "location": "客厅", "timeOfDay": "日", "interiorExterior": "内", "segmentType": "main",
            "startSec": start, "endSec": end,
            "blocks": [{"type": kind, "speaker": "甲", "text": text or f"这是第{index}句。",
                        "startSec": start, "endSec": end}], **fields}


class SceneConsolidationTests(unittest.TestCase):
    def test_two_minute_forty_beats_keep_every_line_and_real_setting_changes(self):
        fragments = [scene(i + 1, i * 3, (i + 1) * 3,
                           location="客厅" if i < 20 else "厨房") for i in range(40)]
        result = normalize_script({"scenes": fragments}, "第1集")
        self.assertEqual(len(result["scenes"]), 2)
        self.assertEqual([b["text"] for s in result["scenes"] for b in s["blocks"]],
                         [b["text"] for s in fragments for b in s["blocks"]])
        self.assertEqual([s["heading"] for s in result["scenes"]], ["1-1 日 内 客厅", "1-2 日 内 厨房"])

    def test_nested_and_overlapping_beats_form_one_scene_in_stable_time_order(self):
        raw = {"scenes": [scene(1, 66, 85, kind="action", text="甲把杯子递给乙。"),
                          scene(2, 79, 82), scene(3, 80, 88), scene(4, 85.93, 88.65),
                          scene(5, 86, 97), scene(6, 88.65, 91.67)]}
        result = normalize_script(raw, "第1集")
        self.assertEqual(len(result["scenes"]), 1)
        merged = result["scenes"][0]
        self.assertEqual((merged["startSec"], merged["endSec"]), (66, 97))
        self.assertEqual([b["text"] for b in merged["blocks"]],
                         [b["text"] for s in raw["scenes"] for b in s["blocks"]])
        self.assertEqual(script_to_markdown({"result": result}),
                         script_to_markdown({"result": normalize_script(result, "第1集")}))

    def test_interleaved_streams_are_sorted_without_rewriting_or_mutating_blocks(self):
        scenes = [scene(1, 0, 20), scene(2, 12, 13), scene(3, 2, 3), scene(4, 2, 4)]
        before = deepcopy(scenes)
        merged = merge_continuous_scenes(scenes)
        self.assertEqual(scenes, before)
        self.assertEqual([b["text"] for b in merged[0]["blocks"]],
                         [scenes[i]["blocks"][0]["text"] for i in [0, 2, 3, 1]])
        self.assertEqual(Counter(b["text"] for s in scenes for b in s["blocks"]),
                         Counter(b["text"] for b in merged[0]["blocks"]))

    def test_real_boundaries_and_invalid_times_remain_separate_despite_overlap(self):
        for changes in ({"location": "卧室"}, {"timeOfDay": "夜"}, {"interiorExterior": "外"},
                        {"segmentType": "flashback"}, {"heading": "2-1 日 内 客厅"},
                        {"startSec": 0, "endSec": 2}, {"startSec": 40, "endSec": 42},
                        {"startSec": True}, {"endSec": float("inf")},
                        {"startSec": 16, "endSec": 15}, {"startSec": None}):
            with self.subTest(changes=changes):
                self.assertEqual(len(merge_continuous_scenes([
                    scene(1, 10, 30), scene(2, 15, 18, **changes)])), 2)
        for side in (0, 1):
            pair = [scene(1, 10, 30), scene(2, 15, 18)]
            pair[side]["blocks"].insert(0, {"type": "transition", "text": "闪回", "transitionType": "flashback"})
            self.assertEqual(len(merge_continuous_scenes(pair)), 2)

    def test_unknown_settings_and_partial_block_times_remain_conservative(self):
        for field in ("location", "timeOfDay", "interiorExterior"):
            pair = [scene(1, 0, 20, **{field: "不明"}), scene(2, 2, 3, **{field: "不明"})]
            self.assertEqual(len(merge_continuous_scenes(pair)), 2)
        pair = [scene(1, 0, 20), scene(2, 2, 3)]
        pair[0]["blocks"][0].pop("startSec")
        self.assertEqual(merge_continuous_scenes(pair)[0]["blocks"], pair[0]["blocks"] + pair[1]["blocks"])

    def test_hidden_shells_are_skipped_and_numbering_resets_per_episode_without_lost_evidence(self):
        hidden = scene(2, 1, 2, kind="screen_text", text="我们出发。", location="门口")
        hidden["blocks"][0]["suppressInOutput"] = True
        script = {"title": "合集", "scenes": [scene(1, 0, 1), hidden,
                  scene(3, 2, 3, location="厨房"),
                  {**deepcopy(hidden), "heading": "2-1 日 外 门口"},
                  {**scene(5, 0, 1), "heading": "2-2 日 内 客厅"}]}
        before = deepcopy(script)
        visible = screenplay_output_scenes(script)
        self.assertEqual([s["heading"] for s in visible],
                         ["1-1 日 内 客厅", "1-2 日 内 厨房", "2-1 日 内 客厅"])
        markdown = script_to_markdown({"result": script})
        self.assertNotIn("门口", markdown)
        self.assertNotIn("1-3", markdown)
        self.assertIn("2-1 日 内 客厅", markdown)
        self.assertEqual(script, before)

    def test_hidden_only_caption_scene_does_not_remove_a_title_card_or_transition(self):
        raw = {"scenes": [scene(1, 0, 3, text="我们出发。"),
                          scene(2, 1, 2, kind="screen_text", text="我们出发。", location="门口"),
                          scene(3, 3, 4, kind="title_card", text="三年后", location="街道"),
                          scene(4, 4, 5, kind="transition", text="闪回", location="街道")]}
        result = normalize_script(raw, "第1集")
        self.assertEqual(len(result["scenes"]), 4)
        self.assertTrue(result["scenes"][1]["blocks"][0]["suppressInOutput"])
        visible = screenplay_output_scenes(result)
        self.assertEqual(len(visible), 3)
        self.assertEqual(visible[-1]["heading"], "1-3 日 内 街道")
        markdown = script_to_markdown({"result": result})
        self.assertIn("三年后", markdown)
        self.assertIn("闪回", markdown)
        self.assertNotIn("门口", markdown)

    def test_additive_repair_consolidates_after_restoring_protected_prose(self):
        original = normalize_script({"scenes": [scene(1, 0, 20), scene(2, 50, 60)]}, "第1集")
        clip = {"scenes": [scene(1, 0, 40, kind="action", text="甲把杯子递给乙。"),
                           scene(2, 5, 10), scene(3, 15, 18)]}
        candidate, _ = merge_gap_repair(original, clip, {"startSec": 20, "endSec": 50}, 20, 40)
        result = normalize_additive_repair(original, candidate, "第1集")
        self.assertEqual(len(result["scenes"]), 1)
        blocks = result["scenes"][0]["blocks"]
        for source in original["scenes"]:
            self.assertIn(source["blocks"][0], blocks)
        self.assertEqual([b["startSec"] for b in blocks], [0, 20, 25, 35, 50])
        self.assertFalse(any("contentRepair" in b["text"] for b in blocks))

    def test_repair_keeps_unknown_setting_parent_without_combining_distinct_parents(self):
        original = normalize_script({"scenes": [scene(1, 45, 50, timeOfDay="不明") ]}, "第1集")
        parent = original["scenes"][0]
        parent["blocks"].extend([deepcopy(parent["blocks"][0]), deepcopy(parent["blocks"][0])])
        for i, block in enumerate(parent["blocks"]):
            block.update(text=f"保留原句{i}。", rawText=f"保留原句{i}。", finalText=f"保留原句{i}。",
                         startSec=45 + i * 2, endSec=47 + i * 2)
        clip = {"scenes": [scene(1, 0, 8, timeOfDay="不明"),
                           scene(2, 8, 16, timeOfDay="不明")]}
        before = deepcopy(original)
        candidate, _ = merge_gap_repair(original, clip, {"startSec": 0, "endSec": 20}, 0, 20)
        result = normalize_additive_repair(original, candidate, "第1集")
        self.assertEqual(len(result["scenes"]), 3)
        self.assertEqual(result["scenes"][-1]["blocks"], parent["blocks"])
        self.assertEqual(original, before)
        again = normalize_script(result, "第1集")
        self.assertEqual(len(again["scenes"]), 3)
        self.assertEqual(script_to_markdown({"result": result}), script_to_markdown({"result": again}))

    def test_legacy_parent_recovery_requires_exact_audit_and_keeps_all_blocks(self):
        parent = scene(1, 45, 90, timeOfDay="不明")
        parent["blocks"] = [scene(i, 45 + i, 46 + i)["blocks"][0] for i in range(1, 25)]
        fragments = [{**deepcopy(parent), "heading": f"1-{i+1} 不明 内 客厅",
                      "startSec": b["startSec"], "endSec": b["endSec"], "blocks": [deepcopy(b)]}
                     for i, b in enumerate(parent["blocks"])]
        raw = {"scenes": fragments}
        before = deepcopy(raw)
        with tempfile.TemporaryDirectory() as tmp, patch.object(recognition_cache.config, "DATA_DIR", Path(tmp)):
            directory = recognition_cache.cache_directory("legacy")
            directory.mkdir(parents=True)
            (directory / "model_initial.json").write_text(json.dumps({"data": {"normalizedScript": {"scenes": [parent]}}}), encoding="utf-8")
            grouped = recognition_cache.restore_cached_scene_groups("legacy", raw)
            result = normalize_script(grouped, "第1集")
            self.assertEqual(len(result["scenes"]), 1)
            self.assertEqual(Counter(b["text"] for b in parent["blocks"]),
                             Counter(b["text"] for b in result["scenes"][0]["blocks"]))
            changed = deepcopy(raw)
            changed["scenes"][1]["blocks"][0]["text"] = "不在原始审计中的文字"
            restored = recognition_cache.restore_cached_scene_groups("legacy", changed)
            self.assertNotIn("continuityGroup", restored["scenes"][1])
            self.assertEqual(recognition_cache.restore_cached_scene_groups("missing", raw), raw)
        self.assertEqual(raw, before)


if __name__ == "__main__":
    unittest.main()
