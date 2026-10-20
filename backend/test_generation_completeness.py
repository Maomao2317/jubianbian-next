"""User regressions: full episodes, stable cast, scenes and caption output."""

from copy import deepcopy
import unittest

from app import processing
from app.script import (apply_batch_character_continuity, normalize_script, quality_gate,
                        script_quality, script_to_markdown, suppress_redundant_subtitles)


def line(text, start, end, **fields):
    return {"type": "dialogue", "speaker": "甲", "text": text,
            "startSec": start, "endSec": end, **fields}


def scene(number, blocks, location="客厅", time="日", interior="内", **fields):
    return {"heading": f"{number} {time} {interior} {location}", "location": location,
            "timeOfDay": time, "interiorExterior": interior, "blocks": blocks, **fields}


def audio_evidence(blocks):
    return {"sources": [{"kind": "audio", "status": "available", "items": [
        {"type": "transcript_segment", "text": block["text"],
         "startSec": block["startSec"], "endSec": block["endSec"]} for block in blocks
    ]}]}


class GenerationCompletenessTests(unittest.TestCase):
    def test_repeated_line_elsewhere_cannot_cover_an_omitted_occurrence(self):
        repeated = "这个秘密千万不要告诉别人"
        speech = [line(repeated, 1, 3), line("我先出去看看情况", 15, 18),
                  line(repeated, 50, 53), line("你一定要平安回来", 70, 73)]
        script = normalize_script({"scenes": [scene("1-1", [speech[0], speech[1], speech[3]])]}, "第1集")
        quality = script_quality(script, "test", audio_evidence(speech))
        missing = [issue for issue in quality["issues"] if issue["tag"] == "疑似漏台词片段"]
        self.assertTrue(any(issue.get("startSec") == 50 and issue["severity"] == "P0" for issue in missing))
        self.assertFalse(quality_gate(quality)[0])
        self.assertTrue(processing._needs_automated_content_review(quality))

    def test_unique_line_with_timestamp_drift_is_not_a_false_omission(self):
        speech = [line("这个秘密千万不要告诉别人", 1, 3), line("你一定要平安回来", 50, 53)]
        script = normalize_script({"scenes": [scene("1-1", [
            {**speech[0], "startSec": 8, "endSec": 10}, speech[1]])]}, "第1集")
        quality = script_quality(script, "test", audio_evidence(speech))
        self.assertNotIn("疑似漏台词片段", [issue["tag"] for issue in quality["issues"]])

    def test_every_repeated_occurrence_with_timing_drift_can_still_be_matched(self):
        speech = [line("这个秘密千万不要告诉别人", 1, 3), line("这个秘密千万不要告诉别人", 50, 53)]
        script = normalize_script({"scenes": [scene("1-1", [
            {**speech[0], "startSec": 8, "endSec": 10}, speech[1]])]}, "第1集")
        quality = script_quality(script, "test", audio_evidence(speech))
        self.assertNotIn("疑似漏台词片段", [issue["tag"] for issue in quality["issues"]])

    def test_five_minute_seventeen_camera_beats_become_five_scenes_without_lost_lines(self):
        locations = ["客厅"] * 4 + ["厨房"] * 4 + ["院子"] * 3 + ["街道"] * 3 + ["卧室"] * 3
        raw = {"characters": ["甲"], "scenes": [
            scene(f"1-{index + 1}", [
                {"type": "action", "text": f"甲拿起第{index + 1}个杯子。",
                 "startSec": index * 17, "endSec": index * 17 + 1},
                line(f"这是第{index + 1}句话。", index * 17 + 1, (index + 1) * 17),
            ], location=location)
            for index, location in enumerate(locations)
        ]}
        script = normalize_script(raw, "第1集")
        self.assertEqual(len(script["scenes"]), 5)
        blocks = [b for s in script["scenes"] for b in s["blocks"]]
        self.assertEqual([b["text"] for b in blocks], [b["text"] for s in raw["scenes"] for b in s["blocks"]])
        self.assertEqual(script["scenes"][-1]["heading"], "1-5 日 内 卧室")
        renormalized = normalize_script(script, "第1集")
        self.assertEqual(len(renormalized["scenes"]), 5)
        self.assertEqual(script_to_markdown({"result": renormalized}), script_to_markdown({"result": script}))

    def test_different_place_time_interior_and_flashback_remain_separate(self):
        left = scene("1-1", [line("第一句话", 0, 2)])
        for change in ({"location": "卧室"}, {"timeOfDay": "夜"}, {"interiorExterior": "外"},
                       {"segmentType": "flashback"},
                       {"blocks": [{"type": "transition", "text": "闪回开始"}, line("第二句话", 2, 4)]}):
            with self.subTest(change=change):
                right = {**scene("1-2", [line("第二句话", 2, 4)]), **change}
                script = normalize_script({"scenes": [left, right]}, "第1集")
                self.assertEqual(len(script["scenes"]), 2)

    def test_scene_bounds_come_from_blocks_and_do_not_bridge_a_long_gap(self):
        script = normalize_script({"scenes": [
            scene("1-1", [line("第一句话", 0, 2)]),
            scene("1-2", [line("第二句话", 60, 62)]),
        ]}, "第1集")
        self.assertEqual(len(script["scenes"]), 2)
        self.assertEqual(script["scenes"][0]["endSec"], 2)

    def test_header_metadata_prevents_merging_day_night_when_fields_are_missing(self):
        script = normalize_script({"scenes": [
            {"heading": "1-1 日 内 客厅", "blocks": [line("白天的台词", 0, 2)]},
            {"heading": "1-2 夜 内 客厅", "blocks": [line("晚上的台词", 2, 4)]},
        ]}, "第1集")
        self.assertEqual(len(script["scenes"]), 2)
        self.assertEqual(script["scenes"][1]["timeOfDay"], "夜")

    def test_unknown_locations_and_untimed_scenes_do_not_merge(self):
        for location, timed in [("待补充", True), ("客厅", False)]:
            with self.subTest(location=location, timed=timed):
                first, second = line("第一句", 0, 2), line("第二句", 2, 4)
                if not timed:
                    for block in (first, second):
                        block.pop("startSec")
                        block.pop("endSec")
                script = normalize_script({"scenes": [scene("1-1", [first], location=location),
                                                       scene("1-2", [second], location=location)]}, "第1集")
                self.assertEqual(len(script["scenes"]), 2)

    def test_episode_boundary_with_reset_timestamps_stays_separate(self):
        script = normalize_script({"scenes": [scene("1-1", [line("第一集", 0, 2)]),
                                               scene("2-1", [line("第二集", 0, 2)])]}, "合集")
        self.assertEqual([s["heading"] for s in script["scenes"]], ["1-1 日 内 客厅", "2-1 日 内 客厅"])

    def test_caption_spanning_two_uncertain_speakers_is_printed_as_dialogue_only(self):
        caption = {"type": "screen_text", "screenType": "subtitle", "text": "走吧，好的。",
                   "startSec": 1, "endSec": 5}
        script = normalize_script({"scenes": [scene("1-1", [
            line("走吧", 1, 3, speaker="未知说话人", confidence="low", uncertain=True),
            line("好的", 3, 5, textSource="subtitle", confidence="low"), caption,
            {**caption, "screenType": "title_card", "text": "三年之后"},
            {**caption, "screenType": "other", "text": "甲·项目负责人"},
            {**caption, "screenType": "system", "text": "任务完成"},
        ])]}, "第1集")
        subtitles = [b for b in script["scenes"][0]["blocks"] if b["type"] == "screen_text"]
        self.assertTrue(subtitles[0].get("suppressInOutput"))
        self.assertFalse(any(b.get("suppressInOutput") for b in subtitles[1:]))
        exported = script_to_markdown({"result": script})
        self.assertNotIn("【字幕：走吧", exported)
        self.assertIn("三年之后", exported)
        self.assertIn("项目负责人", exported)
        self.assertIn("任务完成", exported)

    def test_caption_crosses_adjacent_scenes_but_not_different_episodes(self):
        caption = {"type": "screen_text", "screenType": "subtitle", "text": "跟我走，到了。", "startSec": 1, "endSec": 5}
        script = normalize_script({"scenes": [scene("1-1", [line("跟我走", 1, 3), caption]),
                                               scene("1-2", [line("到了", 3, 5)], location="院子")]}, "第1集")
        self.assertTrue(script["scenes"][0]["blocks"][1].get("suppressInOutput"))
        other_episode = {"scenes": [scene("1-1", [deepcopy(caption)]), scene("2-1", [line("跟我走，到了", 1, 5)])]}
        suppress_redundant_subtitles(other_episode)
        self.assertFalse(other_episode["scenes"][0]["blocks"][0].get("suppressInOutput"))

    def test_unrelated_caption_touching_speech_edge_and_unheard_placeholder_remain_visible(self):
        for dialogue in (line("另外一句话", 3, 5), line("[听不清]", 1, 3)):
            with self.subTest(dialogue=dialogue):
                script = {"scenes": [scene("1-1", [dialogue, {
                    "type": "screen_text", "screenType": "subtitle", "text": "稍后到达",
                    "startSec": 1, "endSec": 3,
                }])]}
                suppress_redundant_subtitles(script)
                self.assertFalse(script["scenes"][0]["blocks"][1].get("suppressInOutput"))

    def test_subtitle_visibility_is_recalculated_after_evidence_recovery(self):
        script = normalize_script({"scenes": [scene("1-1", [line("我们走吧", 1, 3), {
            "type": "screen_text", "screenType": "subtitle", "text": "我们走吧", "startSec": 1, "endSec": 3,
        }])]}, "第1集")
        subtitle = script["scenes"][0]["blocks"][1]
        subtitle.pop("suppressInOutput", None)
        processing._evaluate_quality(script, "test", audio_evidence([line("我们走吧", 1, 3)]),
                                     {"band": "simple", "score": 0, "reasons": []})
        self.assertTrue(subtitle.get("suppressInOutput"))

    def test_thirty_second_draft_of_two_minute_episode_requires_full_video_review(self):
        speech = [line("我们已经商量好了", 1, 3), line("先去看看那个人", 28, 30), line("最后还挣了八万", 142, 145)]
        script = normalize_script({"scenes": [scene("1-1", speech[:2])]}, "第3集")
        quality = script_quality(script, "test", audio_evidence(speech))
        tail = next(issue for issue in quality["issues"] if issue["tag"] == "台词尾部截断")
        self.assertEqual((tail["severity"], quality["tailGapSec"]), ("P0", 115))
        self.assertFalse(quality_gate(quality)[0])
        self.assertTrue(processing._needs_automated_content_review(quality))
        context = processing._automated_review_context(script, processing._automated_review_issues(quality), quality=quality)
        self.assertEqual(context["scope"], "full_video_with_issue_ranges")
        self.assertEqual(context["reviewReason"], "tail_content_loss")
        self.assertTrue(any(window["endSec"] == 145 for window in context["ranges"]))

    def test_quiet_video_tail_does_not_report_missing_dialogue(self):
        speech = line("最后一句已经说完", 28, 30)
        script = normalize_script({"scenes": [scene("1-1", [speech, {
            "type": "action", "text": "甲走出房间。", "startSec": 30, "endSec": 145,
        }])]}, "第1集")
        quality = script_quality(script, "test", audio_evidence([speech]))
        self.assertNotIn("台词尾部截断", [issue["tag"] for issue in quality["issues"]])

    def test_fuller_review_is_not_reverted_for_lower_precision_or_action_counts(self):
        before = {"issues": [], "transcriptCoverage": 40, "dialogueCount": 5,
                  "dialogueEvidencePrecision": 99, "actionCount": 17, "performanceCount": 8}
        after = {"issues": [], "transcriptCoverage": 95, "dialogueCount": 30,
                 "dialogueEvidencePrecision": 82, "actionCount": 5, "performanceCount": 4}
        self.assertIsNone(processing._review_regression_reason(before, after))
        self.assertEqual(processing._review_regression_reason(after, before), "lower_transcriptCoverage")
        after["issues"] = [{"severity": "P0", "tag": "台词归属错"}]
        self.assertIsNotNone(processing._review_regression_reason(before, after))

    def test_more_dialogue_blocks_cannot_mask_lost_spoken_words(self):
        before = {"issues": [], "transcriptCoverage": 97, "dialogueCount": 30}
        for count in (30, 40):
            with self.subTest(dialogueCount=count):
                after = {"issues": [], "transcriptCoverage": 80, "dialogueCount": count}
                self.assertEqual(processing._review_regression_reason(before, after), "lower_transcriptCoverage")

    def test_later_descriptive_role_matches_a_unique_established_appearance(self):
        first = normalize_script({"characterProfiles": [{"name": "林小雨", "appearance": "齐刘海双马尾", "clothing": "校服小女孩"}],
                                  "scenes": [scene("1-1", [line("你好", 1, 3, speaker="林小雨")])]}, "第1集")
        later = normalize_script({"characterProfiles": [{"name": "校服小女孩", "appearance": "齐刘海双马尾"}],
                                  "scenes": [scene("3-1", [line("你叫林小雨吗", 1, 3, speaker="校服小女孩")])]}, "第3集")
        original_words = later["scenes"][0]["blocks"][0]["text"]
        apply_batch_character_continuity([first, later])
        self.assertEqual(later["scenes"][0]["blocks"][0]["speaker"], "林小雨")
        self.assertEqual(later["scenes"][0]["blocks"][0]["text"], original_words)

    def test_shared_clothing_or_placeholder_profiles_do_not_merge_different_names(self):
        for profile in ({"clothing": "白色校服衬衫"}, {"firstAppearance": "首登外观/服装待核对"}):
            with self.subTest(profile=profile):
                scripts = [normalize_script({"characterProfiles": [{"name": name, **profile}],
                                              "scenes": [scene(f"{index}-1", [line("你好", 1, 3, speaker=name)])]}, f"第{index}集")
                           for index, name in enumerate(("林小雨", "赵明月"), 1)]
                apply_batch_character_continuity(scripts)
                self.assertEqual(scripts[1]["scenes"][0]["blocks"][0]["speaker"], "赵明月")

    def test_unknown_girl_requires_unique_appearance_evidence(self):
        for names, expected in [(["江词"], "江词"), (["江词", "吴芳"], "未知女生")]:
            with self.subTest(names=names):
                reference = normalize_script({"characterProfiles": [
                    {"name": name, "appearance": "长发梳高马尾", "clothing": "军绿色宽松外套，白色休闲鞋"}
                    for name in names], "scenes": [scene("1-1", [line("你好", 1, 3, speaker=name) for name in names])]}, "第1集")
                later = normalize_script({"characterProfiles": [{"name": "未知女生", "appearance": "长发梳高马尾",
                    "clothing": "军绿色宽松外套，白色休闲鞋"}],
                    "scenes": [scene("9-1", [line("你叫江词吗", 1, 3, speaker="未知女生")])]}, "第9集")
                original_words = later["scenes"][0]["blocks"][0]["text"]
                apply_batch_character_continuity([reference, later])
                self.assertEqual(later["scenes"][0]["blocks"][0]["speaker"], expected)
                self.assertEqual(later["scenes"][0]["blocks"][0]["text"], original_words)


if __name__ == "__main__":
    unittest.main()
