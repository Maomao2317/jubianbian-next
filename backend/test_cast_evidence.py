"""Regressions from the 90-episode cast audit; no model/network calls."""
from copy import deepcopy
import unittest

from app.script import apply_batch_character_continuity, normalize_script, script_quality


def episode(name, **profile):
    return {"characters": [name], "characterProfiles": [{"name": name, **profile}],
            "scenes": [{"characters": [name], "blocks": [
                {"type": "dialogue", "speaker": name, "visibleSpeaker": name,
                 "text": "林先生，你到底是不是顾青山？", "startSec": 0, "endSec": 3}]}]}


class CastEvidenceTests(unittest.TestCase):
    def test_direct_name_evidence_promotes_descriptive_label(self):
        raw = episode(
            "白发蛇尾男性",
            aliases=["寒澈", "白蟒兽人"],
            nameEvidence="顾薇在开头对白中直接称呼其为寒澈。",
        )
        script = normalize_script(raw, "第48集")
        profile = script["characterProfiles"][0]
        self.assertEqual(profile["name"], "寒澈")
        self.assertIn("白发蛇尾男性", profile["aliases"])
        self.assertEqual(script["characters"], ["寒澈"])
        self.assertEqual(script["scenes"][0]["blocks"][0]["speaker"], "寒澈")

    def test_direct_name_evidence_without_alias_promotes_animated_or_live_role(self):
        raw = episode(
            "短发女孩",
            nameEvidence="台词称呼其为女儿，并称其为婉理。",
        )
        script = normalize_script(raw, "第38集")
        self.assertEqual(script["characterProfiles"][0]["name"], "婉理")
        self.assertEqual(script["scenes"][0]["blocks"][0]["speaker"], "婉理")

    def test_name_card_role_prefix_does_not_hide_name(self):
        raw = episode("黑发黑衣女士", nameEvidence="姓名条显示女主 林长风。")
        script = normalize_script(raw, "第2集")
        self.assertEqual(script["characterProfiles"][0]["name"], "林长风")

    def test_batch_promotes_evidence_before_continuity_matching(self):
        first = episode(
            "白发蛇尾男性",
            aliases=["寒澈"],
            identityEvidence="顾薇在对白中直接称呼其为寒澈。",
        )
        later = episode("寒澈")
        apply_batch_character_continuity([first, later])
        self.assertEqual(first["characters"], ["寒澈"])
        self.assertEqual(later["characters"], ["寒澈"])

    def test_later_direct_name_evidence_backfills_earlier_honorific(self):
        scripts = [episode("林长风", aliases=["男主"], nameEvidence="姓名条显示男主 林长风"),
                   episode("林先生"), episode("男主"),
                   episode("林长风", aliases=["林先生"],
                           nameEvidence="宋星瑶喊林长风，父亲称他为林先生")]
        original_text = [s["scenes"][0]["blocks"][0]["text"] for s in scripts]
        self.assertGreater(apply_batch_character_continuity(scripts), 0)
        for script in scripts:
            block = script["scenes"][0]["blocks"][0]
            self.assertEqual(block["speaker"], "林长风")
            self.assertEqual(block["visibleSpeaker"], "林长风")
        self.assertEqual([s["scenes"][0]["blocks"][0]["text"] for s in scripts], original_text)
        self.assertEqual(apply_batch_character_continuity(scripts), 0)

    def test_shared_honorific_is_ambiguous_and_full_names_stay_distinct(self):
        scripts = [episode("林先生"),
                   episode("林长风", aliases=["林先生"], nameEvidence="林长风也被称林先生"),
                   episode("林青山", aliases=["林先生"], nameEvidence="林青山也被称林先生")]
        apply_batch_character_continuity(scripts)
        self.assertEqual([s["characters"][0] for s in scripts], ["林先生", "林长风", "林青山"])

    def test_alias_list_without_direct_naming_evidence_cannot_backfill(self):
        scripts = [episode("林先生"), episode("林长风", aliases=["林先生"])]
        apply_batch_character_continuity(scripts)
        self.assertEqual(scripts[0]["characters"], ["林先生"])

    def test_single_named_man_does_not_absorb_generic_female_lead(self):
        scripts = [episode("林长风"), episode("女主")]
        apply_batch_character_continuity(scripts)
        self.assertEqual(scripts[1]["characters"], ["女主"])

    def test_unique_evidenced_lead_alias_locks_later_generic_name(self):
        first = episode("甄珠", nameEvidence="画面姓名条显示甄珠")
        generic = episode("女主")
        generic["scenes"][0]["blocks"].insert(0, {
            "type": "action", "text": "女主走到门边，女主角海报挂在墙上。",
            "object": "女主手里的门卡", "result": "女主停下",
            "startSec": 0, "endSec": 1,
        })
        confirmation = episode(
            "甄珠", aliases=["女主"],
            nameEvidence="黎爷爷询问你就是甄珠吧，甄珠开口应答。",
        )
        original_dialogue = generic["scenes"][0]["blocks"][1]["text"]

        self.assertGreater(apply_batch_character_continuity([first, generic, confirmation]), 0)

        self.assertEqual(generic["characters"], ["甄珠"])
        self.assertEqual(generic["scenes"][0]["characters"], ["甄珠"])
        self.assertEqual(generic["scenes"][0]["blocks"][1]["speaker"], "甄珠")
        self.assertEqual(generic["scenes"][0]["blocks"][1]["visibleSpeaker"], "甄珠")
        self.assertEqual(generic["scenes"][0]["blocks"][1]["text"], original_dialogue)
        self.assertEqual(generic["scenes"][0]["blocks"][0]["text"], "甄珠走到门边，女主角海报挂在墙上。")
        self.assertEqual(generic["scenes"][0]["blocks"][0]["object"], "甄珠手里的门卡")
        self.assertEqual(generic["scenes"][0]["blocks"][0]["result"], "甄珠停下")

    def test_ambiguous_lead_alias_is_not_guessed(self):
        scripts = [
            episode("张三", aliases=["男主"], nameEvidence="姓名条显示张三"),
            episode("李四", aliases=["男主"], nameEvidence="姓名条显示李四"),
            episode("男主"),
        ]
        apply_batch_character_continuity(scripts)
        self.assertEqual(scripts[2]["characters"], ["男主"])

    def test_unsupported_later_proper_name_returns_to_evidenced_first_name(self):
        first = episode("张三", nameEvidence="画面姓名条显示张三")
        later = episode("李四", aliases=["张三"], identityEvidence="与张三为同一角色")
        apply_batch_character_continuity([first, later])
        self.assertEqual(later["characters"], ["张三"])
        self.assertEqual(later["scenes"][0]["blocks"][0]["speaker"], "张三")
        self.assertIn("李四", later["characterProfiles"][0]["aliases"])

    def test_missing_profile_for_invented_full_name_requires_review(self):
        raw = episode("顾青山")
        raw.pop("characterProfiles")
        script = normalize_script(raw, "第5集")
        quality = script_quality(script, "test")
        self.assertTrue(any(i["tag"] == "人物姓名缺少依据" and i["severity"] == "P0"
                            for i in quality["issues"]))
        confirmed = deepcopy(script)
        confirmed["characterProfiles"][0]["nameEvidence"] = "姓名条显示顾青山"
        self.assertFalse(any(i["tag"] == "人物姓名缺少依据"
                             for i in script_quality(confirmed, "test")["issues"]))

    def test_unnamed_role_relationship_and_appearance_labels_are_not_guessed_names(self):
        for label in ("林先生", "林父", "父亲", "林大哥", "黑衣人", "保安甲", "顾医生"):
            with self.subTest(label=label):
                raw = episode(label)
                raw.pop("characterProfiles")
                script = normalize_script(raw, "未出现姓名条")
                self.assertFalse(any(i["tag"] == "人物姓名缺少依据"
                                     for i in script_quality(script, "test")["issues"]))
                self.assertEqual(script["scenes"][0]["blocks"][0]["speaker"], label)

    def test_two_character_personal_name_still_requires_evidence(self):
        raw = episode("江川")
        raw.pop("characterProfiles")
        quality = script_quality(normalize_script(raw, "未知姓名"), "test")
        self.assertTrue(any(i["tag"] == "人物姓名缺少依据" for i in quality["issues"]))


if __name__ == "__main__":
    unittest.main()
