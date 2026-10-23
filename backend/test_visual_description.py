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


if __name__ == "__main__":
    unittest.main()
