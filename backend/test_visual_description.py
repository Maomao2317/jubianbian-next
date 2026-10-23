"""Profile enrichment must be idempotent and local to the depicted character."""
from copy import deepcopy
import unittest

from app.script import add_character_visual_descriptions, normalize_script


class VisualDescriptionTests(unittest.TestCase):
    def enrich(self, text, profiles):
        blocks = [{"type": "action", "text": text}]
        add_character_visual_descriptions(blocks, profiles)
        first = deepcopy(blocks)
        for _ in range(3):
            add_character_visual_descriptions(blocks, profiles)
            self.assertEqual(blocks, first)
        return blocks[0]["text"]

    def test_overlapping_fields_reordered_facts_and_fused_insertions(self):
        profiles = [{"name": "救援员", "appearance": "两名年轻男性、穿黄色救援制服、戴白色安全帽",
                     "clothing": "黄色救援全套制服、白色安全帽"}]
        text = self.enrich("两名年轻男性、穿黄色救援制服、戴白色安全帽的黄色救援全套制服、白色安全帽的救援员托起伤者。", profiles)
        self.assertEqual(text, "两名年轻男性、穿黄色救援制服、戴白色安全帽的救援员托起伤者。")
        text = self.enrich("戴白色安全帽、穿黄色救援制服的救援员托起伤者。", profiles)
        self.assertEqual(text.count("救援制服"), 1)
        self.assertEqual(text.count("安全帽"), 1)
        self.assertIn("年轻男性", text)

    def test_each_character_keeps_their_own_clothing(self):
        profiles = [{"name": name, "clothing": "穿蓝色外套、戴白色帽子"} for name in ("周青", "林海")]
        text = self.enrich("穿蓝色外套、戴白色帽子的周青扶着林海走出房门。", profiles)
        self.assertEqual(text.count("蓝色外套"), 2)
        self.assertEqual(text.count("白色帽子"), 2)

    def test_missing_fact_added_without_repeating_present_fact(self):
        text = self.enrich("穿蓝色外套的周青把门打开。", [{"name": "周青", "appearance": "黑色短发",
                                                          "clothing": "蓝色外套、白色帽子"}])
        self.assertEqual(text.count("蓝色外套"), 1)
        self.assertIn("黑色短发", text)
        self.assertIn("白色帽子", text)
        self.assertIn("把门打开", text)

    def test_next_characters_leading_clothes_do_not_cover_the_first(self):
        profiles = [{"name": name, "clothing": "穿蓝色外套"} for name in ("周青", "林海")]
        text = self.enrich("周青扶着穿蓝色外套的林海走出房门。", profiles)
        self.assertEqual(text.count("蓝色外套"), 2)

    def test_post_name_wardrobe_belongs_only_to_its_character(self):
        profiles = [{"name": name, "clothing": "穿蓝色外套"} for name in ("周青", "林海")]
        text = self.enrich("周青穿红色外套，扶着林海走出房门。", profiles)
        self.assertEqual(text.count("蓝色外套"), 1)
        self.assertIn("周青穿红色外套", text)

    def test_legacy_first_appearance_has_the_same_fact_deduplication(self):
        text = self.enrich("穿蓝色外套、戴白色帽子的周青把门打开。", [
            {"name": "周青", "firstAppearance": "戴白色帽子、穿蓝色外套的周青走来。"}])
        self.assertEqual(text, "穿蓝色外套、戴白色帽子的周青把门打开。")

    def test_wardrobe_change_and_repeated_actions_are_preserved(self):
        original = "穿红色外套的周青敲了两下门，周青敲了两下门。"
        text = self.enrich(original, [{"name": "周青", "clothing": "穿蓝色外套"}])
        self.assertEqual(text, original)

    def test_role_name_inside_uniform_is_not_a_character_mention(self):
        profiles = [{"name": "巡警", "appearance": "年轻男性、深蓝色巡警制服、头戴黑色警帽",
                     "clothing": "深蓝色巡警制服、头戴黑色警帽"}]
        text = self.enrich("周青抬起右手，年轻男性、深蓝色巡警制服、头戴黑色警帽的巡警后退。", profiles)
        self.assertEqual(text, "周青抬起右手，年轻男性、深蓝色巡警制服、头戴黑色警帽的巡警后退。")

    def test_role_name_inside_appearance_fact_does_not_split_the_introduction(self):
        profiles = [{"name": "小男孩", "appearance": "短发小男孩、手腕上有一处浅色痕迹",
                     "clothing": "白色短袖衬衫、灰色短裤、黑白条纹长袜、黑皮鞋、灰色领带"}]
        original = "穿校服短裤的短发小男孩、手腕上有一处浅色痕迹、白色短袖衬衫、灰色短裤、黑白条纹长袜、黑皮鞋、灰色领带的小风哼了一声转身跑走。"
        self.assertEqual(self.enrich(original, profiles), original)

    def test_name_referenced_in_another_profile_is_not_an_insertion_boundary(self):
        profiles = [{"name": "周青", "clothing": "穿灰色外套"},
                    {"name": "骑手", "appearance": "年长男性、同情周青的遭遇",
                     "clothing": "蓝色背心、蓝色头盔"}]
        original = "年长男性、同情周青的遭遇、蓝色背心、蓝色头盔的年长男性、同情周青的遭遇的骑手伸手拍了拍周青的后背。"
        text = self.enrich(original, profiles)
        self.assertEqual(text.count("年长男性"), 1)
        self.assertEqual(text.count("同情周青的遭遇"), 1)
        self.assertEqual(text.count("灰色外套"), 1)
        self.assertIn("骑手伸手拍了拍穿灰色外套的周青的后背", text)

    def test_normalization_and_dialogue_are_stable_across_read_export_cycles(self):
        raw = {"characters": ["周青"], "characterProfiles": [{"name": "周青", "appearance": "黑色短发、蓝色外套",
                                                           "clothing": "穿蓝色外套、戴白色帽子"}],
               "scenes": [{"heading": "1-1 日 内 门厅", "location": "门厅", "blocks": [
                   {"type": "action", "text": "黑色短发、穿蓝色外套、戴白色帽子的周青把门打开。"},
                   {"type": "dialogue", "speaker": "周青", "text": "走，走，快走！"}]}]}
        first = normalize_script(deepcopy(raw), "第1集")
        for _ in range(3):
            first_again = normalize_script(deepcopy(first), "第1集")
            self.assertEqual([b["text"] for s in first_again["scenes"] for b in s["blocks"]],
                             [b["text"] for s in first["scenes"] for b in s["blocks"]])
        action = first["scenes"][0]["blocks"][0]["text"]
        self.assertEqual(action.count("蓝色外套"), 1)
        self.assertEqual(first["scenes"][0]["blocks"][1]["text"], "走，走，快走！")

    def test_family_name_without_profile_is_a_separate_character_boundary(self):
        raw = {"characters": ["周青"], "characterProfiles": [
            {"name": "周青", "appearance": "黑色长发的少女、面容清秀", "clothing": "蓝色校服"}],
            "scenes": [{"heading": "1-1 日 内 门厅", "location": "门厅",
                        "characters": ["周青爸爸", "周青", "小熊玩偶"], "blocks": [
                {"type": "action", "text": "周青爸爸捧着小熊玩偶，递给梳着双丸子头的小女孩周青。",
                 "startSec": 1, "endSec": 3},
                {"type": "dialogue", "speaker": "周青", "text": "谢谢爸爸！"}]}]}
        first = normalize_script(deepcopy(raw), "第1集")
        blocks = first["scenes"][0]["blocks"]
        self.assertTrue(blocks[0]["text"].startswith("周青爸爸捧着小熊玩偶，递给"))
        self.assertEqual(blocks[0]["text"].count("面容清秀"), 1)
        self.assertEqual((blocks[0]["startSec"], blocks[0]["endSec"]), (1, 3))
        self.assertEqual(blocks[1]["text"], "谢谢爸爸！")
        again = deepcopy(blocks)
        add_character_visual_descriptions(again, first["characterProfiles"])
        self.assertEqual(again, blocks)
        normalized_again = normalize_script(deepcopy(first), "第1集")
        self.assertEqual(normalized_again["scenes"][0]["blocks"][0], blocks[0])
        self.assertEqual(normalized_again["scenes"][0]["blocks"][1]["text"], blocks[1]["text"])

    def test_scene_only_names_delimit_mentions_in_other_scenes_too(self):
        raw = {"characters": ["周青"], "characterProfiles": [
            {"name": "周青", "appearance": "留着黑色长发"}], "scenes": [
                {"heading": "1-1 日 内 门厅", "location": "门厅", "blocks": [
                    {"type": "action", "text": "周青爸爸递给周青一只玩偶。"}]},
                {"heading": "1-2 日 内 厨房", "location": "厨房", "characters": ["周青爸爸"],
                 "blocks": [{"type": "action", "text": "周青爸爸推开门。"}]}]}
        for source in ("cast", "cast_dict", "speaker", "visibleSpeaker"):
            with self.subTest(source=source):
                candidate = deepcopy(raw)
                later = candidate["scenes"][1]
                if source == "cast_dict":
                    later["characters"] = [{"name": "周青爸爸"}]
                elif source != "cast":
                    later.pop("characters")
                    later["blocks"].append({"type": "dialogue", "speaker": "周青爸爸",
                                            "text": "来了。"} if source == "speaker" else {
                                                "type": "vo", "speaker": "旁白",
                                                "visibleSpeaker": "周青爸爸", "text": "他回来了。"})
                first = normalize_script(candidate, "第1集")
                self.assertEqual(first["scenes"][0]["blocks"][0]["text"], "周青爸爸递给留着黑色长发的周青一只玩偶。")
                self.assertEqual(first["scenes"][1]["blocks"][0]["text"], "周青爸爸推开门。")
                again = normalize_script(deepcopy(first), "第1集")
                self.assertEqual([[(b["type"], b["text"]) for b in s["blocks"]] for s in again["scenes"]],
                                 [[(b["type"], b["text"]) for b in s["blocks"]] for s in first["scenes"]])


if __name__ == "__main__":
    unittest.main()
