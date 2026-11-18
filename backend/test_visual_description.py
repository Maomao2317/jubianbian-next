"""Profile enrichment must be idempotent and local to the depicted character."""
from copy import deepcopy
import unittest

from app.script import (
    add_character_visual_descriptions,
    compact_action_character_introductions,
    normalize_script,
    script_to_markdown,
    strip_action_character_visual_descriptions,
)


class VisualDescriptionTests(unittest.TestCase):
    def test_static_portraits_are_removed_but_wardrobe_actions_and_dialogue_remain(self):
        blocks = [
            {"type": "action", "text": "穿蓝色外套、戴白色帽子的周青推开门。"},
            {"type": "action", "text": "江晚走到穿深灰西装的顾岑舟身侧，将手搭在他的肩上。"},
            {"type": "action", "text": "周青穿上蓝色外套，扣好纽扣后走出房门。"},
            {"type": "dialogue", "speaker": "周青", "text": "这件衣服是证据，不能丢。"},
        ]
        strip_action_character_visual_descriptions(blocks, [
            {"name": "周青"}, {"name": "江晚"}, {"name": "顾岑舟"},
        ])

        self.assertEqual(blocks[0]["text"], "周青推开门。")
        self.assertEqual(blocks[1]["text"], "江晚走到顾岑舟身侧，将手搭在他的肩上。")
        self.assertEqual(blocks[2]["text"], "周青穿上蓝色外套，扣好纽扣后走出房门。")
        self.assertEqual(blocks[3]["text"], "这件衣服是证据，不能丢。")

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
        self.assertNotIn("白色帽子", text)

    def test_existing_fact_overlapping_a_longer_name_is_not_split(self):
        profiles = [{"name": "母狮妈妈", "appearance": "成年母狮、金色毛发、褐色眼眸"}]
        for original in ("成年母狮妈妈转身。", "幼崽靠近成年母狮妈妈，母狮妈妈转身。"):
            with self.subTest(original=original):
                text = self.enrich(original, profiles)
                self.assertIn("金色毛发、褐色眼眸的成年母狮妈妈", text)
                self.assertEqual(text.count("成年母狮"), 1)
                self.assertEqual(text.count("母狮妈妈"), original.count("母狮妈妈"))
                self.assertTrue(text.endswith("转身。"))

    def test_missing_fact_added_without_repeating_present_fact(self):
        text = self.enrich("穿蓝色外套的周青把门打开。", [{"name": "周青", "appearance": "黑色短发",
                                                          "clothing": "蓝色外套、白色帽子"}])
        self.assertEqual(text.count("蓝色外套"), 1)
        self.assertNotIn("黑色短发", text)
        self.assertNotIn("白色帽子", text)
        self.assertIn("把门打开", text)

    def test_punctuation_inside_profile_fact_does_not_split_the_introduction(self):
        profiles = [{"name": "巡逻员", "appearance": "黑色短发、面容清瘦",
                     "clothing": "印A.B.标识的蓝色背心、银色项链"}]
        for original in ("巡逻员打开门。", "路人停下。巡逻员打开门。",
                         "黑色短发、面容清瘦、印A。B。标识的蓝色背心、银色项链的黑色短发的巡逻员打开门。"):
            with self.subTest(original=original):
                text = self.enrich(original, profiles)
                for fact in ("黑色短发", "面容清瘦", "蓝色背心", "银色项链"):
                    self.assertEqual(text.count(fact), 1)
                self.assertTrue(text.endswith("巡逻员打开门。"))
                self.assertEqual(text.count("路人停下。"), original.count("路人停下。"))

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
        self.assertEqual(text, "穿蓝色外套的周青把门打开。")

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
        self.assertNotIn("年长男性", text)
        self.assertNotIn("同情周青的遭遇", text)
        self.assertIn("穿蓝色背心的骑手伸手拍了拍", text)
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
        self.assertEqual(action, "周青把门打开。")
        self.assertEqual(first["characterProfiles"][0]["appearance"], "黑色短发、蓝色外套")
        self.assertEqual(first["characterProfiles"][0]["clothing"], "穿蓝色外套、戴白色帽子")
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
        self.assertEqual(blocks[0]["text"], "周青爸爸捧着小熊玩偶，递给周青。")
        self.assertNotIn("面容清秀", blocks[0]["text"])
        self.assertEqual((blocks[0]["startSec"], blocks[0]["endSec"]), (1, 3))
        self.assertEqual(blocks[1]["text"], "谢谢爸爸！")
        again = deepcopy(blocks)
        strip_action_character_visual_descriptions(again, first["characterProfiles"])
        self.assertEqual(again, blocks)
        normalized_again = normalize_script(deepcopy(first), "第1集")
        self.assertEqual(normalized_again["scenes"][0]["blocks"][0], blocks[0])
        self.assertEqual(normalized_again["scenes"][0]["blocks"][1]["text"], blocks[1]["text"])

    def test_visual_introduction_is_omitted_while_action_and_dialogue_stay_complete(self):
        appearance = "梳双丸子头、脸颊圆润、眼神明亮、个子娇小"
        action_text = "希希先扶住缸沿，再谨慎探身向内观察，确认奶奶的位置后才伸出右手。"
        raw = {
            "characterProfiles": [{
                "name": "希希",
                "appearance": appearance,
                "clothing": "红色碎花棉袄、黑色长裤、白色布鞋、银色手镯",
            }],
            "scenes": [{
                "heading": "1-1 日 外 农家院子",
                "characters": ["希希"],
                "blocks": [
                    {"type": "action", "text": action_text},
                    {"type": "dialogue", "speaker": "希希", "text": "奶奶，我来帮你！"},
                ],
            }],
        }
        script = normalize_script(raw, "第1集")
        profile = script["characterProfiles"][0]
        self.assertEqual(profile["appearance"], appearance)
        self.assertEqual(profile["clothing"], "红色碎花棉袄、黑色长裤")
        action, dialogue = script["scenes"][0]["blocks"]
        self.assertEqual(action["text"], action_text)
        self.assertNotIn("红色棉袄", action["text"])
        self.assertNotIn("黑色长裤", action["text"])
        self.assertNotIn("双丸子头", action["text"])
        self.assertNotIn("脸颊圆润", action["text"])
        self.assertNotIn("白色布鞋", action["text"])
        self.assertNotIn("银色手镯", action["text"])
        self.assertEqual(dialogue["text"], "奶奶，我来帮你！")
        exported = script_to_markdown({"title": "第1集", "result": script})
        self.assertNotIn("红色碎花棉袄", exported)
        self.assertNotIn("双丸子头", exported)
        self.assertIn(action_text, exported)
        self.assertIn("奶奶，我来帮你！", exported)

    def test_long_model_written_portraits_are_removed(self):
        cases = [
            (
                "顾岑舟",
                "年轻男性、黑色偏分短发、眼下有痣",
                "深灰色西装外套内搭黑色衬衫、西装领上别着带链条装饰的胸针",
                "穿深灰西装、内搭黑衬衫的年轻男性、黑色偏分短发、眼下有痣、"
                "深灰色西装外套内搭黑色衬衫、西装领上别着带链条装饰的胸针的"
                "顾岑舟坐在长会议桌主位上，面前放着文件。",
                "顾岑舟坐在长会议桌主位上，面前放着文件。",
            ),
            (
                "江晚",
                "年轻女性、黑色大波浪长发",
                "黑色吊带长裙、黑色粗花呢短外套、黑色高跟鞋、细手链",
                "穿黑色开叉长裙、脚踩黑色高跟鞋的年轻女性、黑色大波浪长发、"
                "黑色吊带长裙领口有珍珠装饰、后期穿黑色粗花呢短外套配白色衬衫"
                "与黑色短裤的江晚迈步走进会议室，左手腕戴着细手链。",
                "江晚迈步走进会议室。",
            ),
            (
                "商场女销售",
                "年轻女性、黑色头发扎成低马尾",
                "米白色西装套装、同色上衣、白手套",
                "穿米白西装、戴白手套的年轻女性、黑色头发扎成低马尾、"
                "米白色西装套装、内搭同色系上衣的商场女销售双手接过黑卡，"
                "低头微笑看向卡片。",
                "商场女销售双手接过黑卡，低头微笑看向卡片。",
            ),
            (
                "顾宜悦",
                "年轻女性、黑色长卷发、头上别着粉色蝴蝶结发饰",
                "粉色无袖连衣裙、白色大翻领",
                "穿粉色连衣裙、头戴粉色蝴蝶结的年轻女性、黑色长卷发、"
                "头上别着粉色蝴蝶结发饰、粉色无袖连衣裙、配有白色大翻领的"
                "顾宜悦站在客厅中，转头看向进门的江晚，面露怒色。",
                "顾宜悦站在客厅中，转头看向进门的江晚，面露怒色。",
            ),
        ]
        for name, appearance, clothing, action, expected in cases:
            with self.subTest(name=name):
                raw = {
                    "characterProfiles": [{
                        "name": name,
                        "appearance": appearance,
                        "clothing": clothing,
                    }],
                    "scenes": [{
                        "heading": "1-1 日 内 会议室",
                        "characters": [name],
                        "blocks": [
                            {"type": "action", "text": action},
                            {"type": "dialogue", "speaker": name, "text": "原话必须完整保留。"},
                        ],
                    }],
                }
                first = normalize_script(deepcopy(raw), "第1集")
                self.assertEqual(first["scenes"][0]["blocks"][0]["text"], expected)
                self.assertEqual(
                    first["scenes"][0]["blocks"][1]["text"], "原话必须完整保留。"
                )
                again = normalize_script(deepcopy(first), "第1集")
                self.assertEqual(
                    again["scenes"][0]["blocks"][0]["text"], expected
                )
                self.assertEqual(
                    again["scenes"][0]["blocks"][1]["text"], "原话必须完整保留。"
                )

    def test_described_object_is_not_mistaken_for_the_acting_character(self):
        original = "江晚走到穿深灰西装、戴胸针的顾岑舟身侧，将一只手搭在他的肩膀上。"
        blocks = [{"type": "action", "text": original}]
        compact_action_character_introductions(blocks, [{
            "name": "顾岑舟",
            "clothing": "深灰色西装外套、黑色衬衫、胸针",
        }])
        self.assertEqual(blocks[0]["text"], original)

    def test_long_portrait_without_a_known_garment_keeps_only_name_and_action(self):
        blocks = [{
            "type": "action",
            "text": "年轻女性、黑色长卷发、佩戴蝴蝶结发饰的顾宜悦站在客厅中。",
        }]
        compact_action_character_introductions(blocks, [{
            "name": "顾宜悦",
            "appearance": "年轻女性、黑色长卷发",
        }])
        self.assertEqual(blocks[0]["text"], "顾宜悦站在客厅中。")

    def test_setting_before_long_portrait_is_preserved(self):
        blocks = [
            {
                "type": "action",
                "text": "会议室内，穿深灰西装、内搭黑衬衫的年轻男性、黑色偏分短发、"
                "眼下有痣的顾岑舟坐在主位上，翻开文件。",
            },
            {"type": "dialogue", "speaker": "顾岑舟", "text": "开始吧。"},
        ]
        compact_action_character_introductions(blocks, [{
            "name": "顾岑舟",
            "clothing": "深灰色西装外套、黑色衬衫",
        }])
        self.assertEqual(
            blocks[0]["text"],
            "会议室内，穿深灰色西装的顾岑舟坐在主位上，翻开文件。",
        )
        self.assertEqual(blocks[1]["text"], "开始吧。")

    def test_prior_walk_through_action_is_not_mistaken_for_clothing(self):
        blocks = [{
            "type": "action",
            "text": "穿过走廊后，穿黑色长裙、戴项链的江晚走进会议室。",
        }]
        compact_action_character_introductions(blocks, [{
            "name": "江晚",
            "clothing": "黑色长裙、项链",
        }])
        self.assertEqual(
            blocks[0]["text"], "穿过走廊后，穿黑色长裙的江晚走进会议室。"
        )

    def test_repeated_outfit_is_removed_without_changing_actions_or_dialogue(self):
        blocks = [
            {"type": "action", "text": "穿米白色上衣的诺诺妈抬手轻轻打开陆星尘的手，侧头摇头说话。"},
            {"type": "action", "text": "身穿米白色波点露肩上衣的诺诺妈身体发软，靠进陆星尘的怀里，抬手抓住他的衬衫衣襟。"},
            {"type": "action", "text": "身穿酒红色衬衫的陆星尘伸手环住诺诺妈，手掌轻拍她的后背安抚。"},
            {"type": "action", "text": "身穿米白色波点露肩上衣的诺诺妈从陆星尘怀里抬起头，看向他开口说话。"},
            {"type": "dialogue", "speaker": "诺诺妈", "text": "妈妈没事了，诺诺不用担心。"},
        ]
        original_actions = [block["text"] for block in blocks if block["type"] == "action"]
        compact_action_character_introductions(blocks, [
            {"name": "诺诺妈", "clothing": "米白色上衣"},
            {"name": "陆星尘", "clothing": "酒红色衬衫"},
        ])

        actions = [block["text"] for block in blocks if block["type"] == "action"]
        self.assertEqual(actions[0], original_actions[0])
        self.assertIn("身体发软，靠进陆星尘的怀里，抬手抓住他的衬衫衣襟。", actions[1])
        self.assertIn("伸手环住诺诺妈，手掌轻拍她的后背安抚。", actions[2])
        self.assertIn("从陆星尘怀里抬起头，看向他开口说话。", actions[3])
        self.assertEqual(sum("米白色" in action for action in actions), 1)
        self.assertEqual(blocks[-1]["text"], "妈妈没事了，诺诺不用担心。")

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
                self.assertEqual(first["scenes"][0]["blocks"][0]["text"], "周青爸爸递给周青一只玩偶。")
                self.assertEqual(first["scenes"][1]["blocks"][0]["text"], "周青爸爸推开门。")
                again = normalize_script(deepcopy(first), "第1集")
                self.assertEqual([[(b["type"], b["text"]) for b in s["blocks"]] for s in again["scenes"]],
                                 [[(b["type"], b["text"]) for b in s["blocks"]] for s in first["scenes"]])


if __name__ == "__main__":
    unittest.main()
