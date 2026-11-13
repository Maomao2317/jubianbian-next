import unittest

from app.errors import ArkError
from app.providers import ark_prompt
from app.script import script_to_markdown
from app.translation import translate_delivery


def sample_script():
    return {
        "title": "雨夜来客",
        "characters": ["林夏"],
        "scenes": [{
            "heading": "1-1 夜 内 客厅",
            "characters": ["林夏"],
            "blocks": [
                {"type": "dialogue", "speaker": "林夏", "emotion": "紧张", "text": "你是谁？"},
                {"type": "action", "text": "林夏关上门。"},
            ],
        }],
    }


class TranslationTests(unittest.TestCase):
    def test_english_source_prompt_specifies_english_punctuation(self):
        prompt = ark_prompt("Episode", 60, {"sourceLanguage": "en", "sources": []})

        self.assertIn("英文对白使用符合英文语法的标点", prompt)
        self.assertIn("不要恢复中文标点", prompt)

    def test_same_language_returns_copy_without_provider_call(self):
        original = sample_script()

        def unexpected_call(*_args):
            raise AssertionError("same-language delivery must not call the translator")

        delivery, usage = translate_delivery(original, "zh", "zh", translate_items=unexpected_call)

        self.assertEqual(delivery, original)
        self.assertIsNot(delivery, original)
        self.assertEqual(usage["callCount"], 0)

    def test_screenplay_export_uses_translated_delivery_copy(self):
        original = sample_script()
        delivery = sample_script()
        delivery["title"] = "The Visitor"
        delivery["scenes"][0]["blocks"][0]["text"] = "Who are you?"

        exported = script_to_markdown({"title": original["title"], "result": original, "delivery": delivery})

        self.assertIn("# The Visitor", exported)
        self.assertIn("Who are you?", exported)
        self.assertNotIn("你是谁？", exported)

    def test_screenplay_export_localizes_format_labels_and_bilingual_labels(self):
        script = sample_script()
        script["scenes"][0]["blocks"].extend([
            {"type": "sound", "category": "ambience", "text": "Rain"},
            {"type": "screen_text", "text": "No entry"},
            {"type": "transition", "transitionType": "flashback"},
            {"type": "vo", "speaker": "旁白", "text": "It begins."},
        ])

        english = script_to_markdown({**script, "result": script, "outputLanguage": "en"})
        bilingual = script_to_markdown({**script, "result": script, "outputLanguage": "both"})

        self.assertIn("Cast: 林夏", english)
        self.assertIn("[Ambience: Rain]", english)
        self.assertIn("[On-screen text: No entry]", english)
        self.assertIn("[Flashback]", english)
        self.assertIn("VO: It begins.", english)
        self.assertNotIn("出场人物", english)
        self.assertIn("出场人物 / Cast: 林夏", bilingual)
        self.assertIn("【环境音 / Ambience：Rain】", bilingual)
        self.assertIn("【字幕 / On-screen text：No entry】", bilingual)
        self.assertIn("【闪回 / Flashback】", bilingual)

    def test_translation_and_bilingual_delivery_preserve_structure_and_names(self):
        def translate(items, source, target):
            self.assertEqual((source, target), ("zh", "en"))
            return [{"id": item["id"], "text": f"EN {item['text']}"} for item in items], {
                "model": "doubao-lite", "input_tokens": 12, "output_tokens": 8,
                "total_tokens": 20, "api_cost_rmb": 0.001,
            }

        original = sample_script()
        translated, usage = translate_delivery(original, "zh", "en", translate_items=translate)
        bilingual, _ = translate_delivery(original, "zh", "both", translate_items=translate)

        self.assertEqual(translated["characters"], ["林夏"])
        self.assertEqual(translated["scenes"][0]["blocks"][0]["speaker"], "林夏")
        self.assertEqual(translated["scenes"][0]["blocks"][0]["text"], "EN 你是谁？")
        self.assertEqual(bilingual["scenes"][0]["blocks"][0]["text"], "你是谁？\nEN 你是谁？")
        self.assertEqual(len(translated["scenes"][0]["blocks"]), len(original["scenes"][0]["blocks"]))
        self.assertEqual(original["scenes"][0]["blocks"][0]["text"], "你是谁？")
        self.assertEqual(usage["callCount"], 1)
        self.assertEqual(usage["model"], "doubao-lite")

    def test_english_source_can_be_delivered_in_chinese_or_bilingually(self):
        original = {
            "title": "The visitor",
            "characters": ["Lin Xia"],
            "scenes": [{
                "heading": "1-1 Night INT Living room",
                "characters": ["Lin Xia"],
                "blocks": [{"type": "dialogue", "speaker": "Lin Xia", "text": "Who are you?"}],
            }],
        }

        def translate(items, source, target):
            self.assertEqual((source, target), ("en", "zh"))
            return [{"id": item["id"], "text": f"ZH {item['text']}"} for item in items], {
                "model": "doubao-lite", "input_tokens": 7, "output_tokens": 5,
                "total_tokens": 12, "api_cost_rmb": 0.001,
            }

        translated, _ = translate_delivery(original, "en", "zh", translate_items=translate)
        bilingual, _ = translate_delivery(original, "en", "both", translate_items=translate)

        self.assertEqual(translated["scenes"][0]["blocks"][0]["text"], "ZH Who are you?")
        self.assertEqual(bilingual["scenes"][0]["blocks"][0]["text"], "Who are you?\nZH Who are you?")
        self.assertEqual(len(translated["scenes"][0]["blocks"]), 1)

    def test_missing_or_duplicate_translation_ids_fail_closed(self):
        for returned in ([{"id": "t0", "text": "translated"}],
                         [{"id": "t0", "text": "first"}, {"id": "t0", "text": "duplicate"},
                          {"id": "t1", "text": "extra"}]):
            with self.subTest(returned=returned):
                with self.assertRaises(ArkError) as caught:
                    translate_delivery(
                        sample_script(), "zh", "en",
                        translate_items=lambda *_args, result=returned: (result, {}),
                    )
                self.assertEqual(caught.exception.provider_code, "translation_incomplete")

    def test_english_source_prompt_requires_original_language_transcription(self):
        prompt = ark_prompt("Episode", 60, {"sourceLanguage": "en", "sources": []})
        review_prompt = ark_prompt(
            "Episode", 60, {"sourceLanguage": "en", "sources": []},
            {"scope": "issue_patches", "issues": [], "ranges": []},
        )

        self.assertIn("原剧语言为英文", prompt)
        self.assertIn("不翻译成中文", prompt)
        self.assertIn("原剧语言为英文", review_prompt)
        self.assertIn("不翻译成中文", review_prompt)


if __name__ == "__main__":
    unittest.main()
