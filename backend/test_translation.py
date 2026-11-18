import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.errors import ArkError
from app import providers
from app.providers import ark_prompt
from app.script import script_to_markdown
from app import translation as translation_module
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

    def test_translation_items_include_screenplay_context_and_cast_glossary(self):
        original = sample_script()
        captured = []

        def translate(items, _source, _target):
            captured.extend(items)
            return [{"id": item["id"], "text": f"EN {item['text']}"} for item in items], {}

        translate_delivery(original, "zh", "en", translate_items=translate)

        dialogue = next(item for item in captured if item.get("kind") == "dialogue" and item.get("field") == "text")
        self.assertEqual(dialogue["speaker"], "林夏")
        self.assertIn("1-1", dialogue["scene"])
        self.assertTrue(any("林夏" in item.get("protectedNames", "") for item in captured))

    def test_names_in_translatable_text_use_deterministic_placeholders(self):
        original = sample_script()
        character = original["characters"][0]
        captured = []

        def translate(items, _source, _target):
            captured.extend(items)
            return [{"id": item["id"], "text": f"EN {item['text']}"} for item in items], {}

        delivery, _usage = translate_delivery(original, "zh", "en", translate_items=translate)

        action_item = next(item for item in captured if "JBB_NAME" in item["text"])
        self.assertNotIn(character, action_item["text"])
        self.assertIn("[[JBB_NAME_000]]", action_item["text"])
        self.assertIn(character, delivery["scenes"][0]["blocks"][1]["text"])
        self.assertNotIn("JBB_NAME", delivery["scenes"][0]["blocks"][1]["text"])

    def test_changed_name_placeholder_retries_only_the_affected_field(self):
        original = sample_script()
        character = original["characters"][0]
        calls = []

        def translate(items, _source, _target):
            calls.append([item["id"] for item in items])
            returned = []
            for item in items:
                text = item["text"]
                if "JBB_NAME" in text and "placeholderRule" not in item:
                    text = text.replace("[[JBB_NAME_000]]", "Lin")
                returned.append({"id": item["id"], "text": f"EN {text}"})
            return returned, {}

        delivery, usage = translate_delivery(original, "zh", "en", translate_items=translate)

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[1]), 1)
        self.assertIn(character, delivery["scenes"][0]["blocks"][1]["text"])
        self.assertEqual(usage["callCount"], 2)

    def test_common_placeholder_formatting_is_restored_without_retry(self):
        original = sample_script()
        character = original["characters"][0]
        calls = []

        def translate(items, _source, _target):
            calls.append(len(items))
            return [{
                "id": item["id"],
                "text": item["text"].replace(
                    "[[JBB_NAME_000]]", "［［jbb_name_000］］",
                ),
            } for item in items], {}

        delivery, _usage = translate_delivery(
            original, "zh", "en", translate_items=translate,
        )

        self.assertEqual(calls, [5])
        self.assertIn(character, delivery["scenes"][0]["blocks"][1]["text"])

    def test_latin_name_cannot_match_inside_another_name_placeholder(self):
        original = {
            "title": "Meeting",
            "characters": ["JBB", "Alice"],
            "scenes": [{
                "heading": "1-1 Day INT Office",
                "characters": ["JBB", "Alice"],
                "blocks": [{"type": "action", "text": "Alice meets JBB."}],
            }],
        }

        def translate(items, _source, _target):
            action = next(item for item in items if item.get("kind") == "action")
            self.assertEqual(
                action["text"],
                "[[JBB_NAME_000]] meets [[JBB_NAME_001]].",
            )
            return [{"id": item["id"], "text": item["text"]} for item in items], {}

        delivery, _usage = translate_delivery(
            original, "en", "zh", translate_items=translate,
        )

        self.assertEqual(
            delivery["scenes"][0]["blocks"][0]["text"],
            "Alice meets JBB.",
        )

    def test_translation_preserves_multiline_dialogue_names_and_scene_numbers(self):
        original = sample_script()
        original["scenes"][0]["blocks"][0]["text"] = "林夏：第一句\n第二句"

        def changed_lines(items, *_args):
            return [{"id": item["id"], "text": "collapsed"} for item in items], {}

        with self.assertRaises(ArkError) as lines:
            translate_delivery(original, "zh", "en", translate_items=changed_lines)
        self.assertEqual(lines.exception.provider_code, "translation_structure_changed")

        def changed_name(items, *_args):
            return [{"id": item["id"], "text": re.sub(r"\[\[JBB_NAME_\d+\]\]", "Lin", item["text"])}
                    for item in items], {}

        with self.assertRaises(ArkError) as name:
            translate_delivery(original, "zh", "en", translate_items=changed_name)
        self.assertEqual(name.exception.provider_code, "translation_name_changed")

        def changed_scene(items, *_args):
            return [{"id": item["id"], "text": "2-1 Night INT Living room"
                     if item.get("kind") == "scene_heading" else item["text"]}
                    for item in items], {}

        with self.assertRaises(ArkError) as scene:
            translate_delivery(sample_script(), "zh", "en", translate_items=changed_scene)
        self.assertEqual(scene.exception.provider_code, "translation_structure_changed")

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

    def test_output_limit_splits_translation_batch_and_merges_usage(self):
        original = sample_script()
        original["scenes"][0]["blocks"] = [
            {"type": "dialogue", "text": f"台词 {index}"} for index in range(3)
        ]
        calls = []

        def translate(items, source, target):
            calls.append(len(items))
            if len(items) > 1:
                raise ArkError("output limit", provider_code="output_limit")
            return [{"id": items[0]["id"], "text": f"EN {items[0]['text']}"}], {
                "model": "doubao-lite", "input_tokens": 2, "output_tokens": 3,
                "total_tokens": 5, "api_cost_rmb": 0.001,
            }

        delivery, usage = translate_delivery(original, "zh", "en", translate_items=translate)

        self.assertEqual(calls, [5, 2, 1, 1, 3, 1, 2, 1, 1])
        self.assertEqual(usage["callCount"], 5)
        self.assertEqual(usage["total_tokens"], 25)
        self.assertTrue(all(block["text"].startswith("EN ") for block in delivery["scenes"][0]["blocks"]))

    def test_provider_translation_chunks_run_concurrently_and_preserve_order(self):
        original = sample_script()
        original["scenes"][0]["blocks"] = [
            {"type": "dialogue", "text": f"台词 {index}"} for index in range(95)
        ]
        lock = threading.Lock()
        entered = threading.Barrier(2)
        active = 0
        max_active = 0

        def translate(items, source, target):
            nonlocal active, max_active
            self.assertEqual((source, target), ("zh", "en"))
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                entered.wait(timeout=2)
                return [{"id": item["id"], "text": f"EN {item['text']}"} for item in items], {
                    "model": "doubao-lite", "input_tokens": 2, "output_tokens": 3,
                    "total_tokens": 5, "api_cost_rmb": 0.001,
                }
            finally:
                with lock:
                    active -= 1

        with patch("app.translation.ark_translate_items", side_effect=translate):
            delivery, usage = translate_delivery(original, "zh", "en")

        self.assertGreaterEqual(max_active, 2)
        self.assertEqual(usage["callCount"], 2)
        self.assertEqual(usage["chunkCount"], 2)
        self.assertEqual(
            [block["text"] for block in delivery["scenes"][0]["blocks"][:3]],
            ["EN 台词 0", "EN 台词 1", "EN 台词 2"],
        )

    def test_multiple_tasks_share_bounded_translation_capacity(self):
        lock = threading.Lock()
        two_entered = threading.Event()
        active = 0
        peak = 0

        def translate(items, _source, _target):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if peak >= 2:
                    two_entered.set()
            try:
                self.assertTrue(two_entered.wait(timeout=2))
                return [{"id": item["id"], "text": f"EN {item['text']}"} for item in items], {}
            finally:
                with lock:
                    active -= 1

        def run_task(index):
            script = sample_script()
            script["title"] = f"任务 {index}"
            return translate_delivery(script, "zh", "en", translate_items=translate)[0]

        with (patch.object(translation_module, "_translation_semaphore", threading.BoundedSemaphore(2)),
              ThreadPoolExecutor(max_workers=5) as tasks):
            results = list(tasks.map(run_task, range(5)))

        self.assertEqual(len(results), 5)
        self.assertEqual(peak, 2)

    def test_translation_uses_text_sized_provider_reservation(self):
        completed = {
            "status": "completed",
            "output_text": '{"translations":[{"id":"t0","text":"Hello"}]}',
            "usage": {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
        }
        with (patch.object(providers, "ark_account_key", return_value="key"),
              patch.object(providers, "ark_stream_response", return_value=completed) as request):
            translated, usage = providers.ark_translate_items(
                [{"id": "t0", "text": "你好", "kind": "dialogue"}], "zh", "en",
            )

        payload = request.call_args.args[0]
        self.assertEqual(translated[0]["text"], "Hello")
        self.assertEqual(usage["model"], providers.ARK_LITE_MODEL)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertLess(payload["max_output_tokens"], providers.ARK_MAX_OUTPUT_TOKENS)
        self.assertLess(payload["_jbb_token_reservation"], providers.ARK_INPUT_TOKEN_RESERVE)

    def test_single_field_output_limit_retries_once_with_larger_budget(self):
        completed = {
            "status": "completed",
            "output_text": '{"translations":[{"id":"t0","text":"Hello"}]}',
            "usage": {"input_tokens": 20, "output_tokens": 4, "total_tokens": 24},
        }
        limit = ArkError("output limit", provider_code="output_limit")
        with (
            patch.object(providers, "ark_account_key", return_value="key"),
            patch.object(
                providers, "ark_stream_response", side_effect=[limit, completed],
            ) as request,
        ):
            translated, _usage = providers.ark_translate_items(
                [{"id": "t0", "text": "你好", "kind": "dialogue"}], "zh", "en",
            )

        self.assertEqual(translated, [{"id": "t0", "text": "Hello"}])
        self.assertEqual(request.call_count, 2)
        first, second = (call.args[0] for call in request.call_args_list)
        self.assertEqual(first["thinking"], {"type": "disabled"})
        self.assertEqual(second["thinking"], {"type": "disabled"})
        self.assertGreater(second["max_output_tokens"], first["max_output_tokens"])
        self.assertLessEqual(second["max_output_tokens"], providers.ARK_MAX_OUTPUT_TOKENS)
        self.assertGreater(second["_jbb_token_reservation"], first["_jbb_token_reservation"])

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
