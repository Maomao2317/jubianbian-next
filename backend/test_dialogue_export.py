"""Downloads omit dialogue wrappers without changing stored screenplay text."""
from copy import deepcopy
from unittest.mock import MagicMock, patch
import unittest

from app import routes
from app.script import normalize_script, script_to_markdown, suppress_redundant_subtitles


class DialogueExportTests(unittest.TestCase):
    def test_internal_labels_and_dialogue_survive_repeated_normalization(self):
        source = {"title": "第1集", "scenes": [{"heading": "1-1 日 内 客厅", "blocks": [
            {"type": "action", "text": "林川站在门内，看向门外。", "startSec": 0, "endSec": 1},
            {"type": "screen_text", "screenType": "name_card", "text": "林川", "startSec": 0, "endSec": 1},
            {"type": "screen_text", "screenType": "scene_label", "text": "客厅", "startSec": 0, "endSec": 1},
            {"type": "dialogue", "speaker": "林川", "text": "走。", "startSec": 1, "endSec": 2},
            {"type": "screen_text", "screenType": "subtitle", "text": "走。", "startSec": 1, "endSec": 2},
            {"type": "dialogue", "speaker": "林川", "text": "走。", "startSec": 3, "endSec": 4},
            {"type": "screen_text", "screenType": "system", "text": "任务完成", "startSec": 4, "endSec": 5},
            {"type": "screen_text", "screenType": "other", "text": "林川", "startSec": 5, "endSec": 6},
        ]}]}
        script = normalize_script(source, "第1集")
        script = normalize_script(script, "第1集")
        suppress_redundant_subtitles(script)
        blocks = script["scenes"][0]["blocks"]
        self.assertEqual([b["text"] for b in blocks if b["type"] == "dialogue"], ["走。", "走。"])
        labels = {b["screenType"]: b for b in blocks if b["type"] == "screen_text"}
        self.assertTrue(labels["name_card"]["suppressInOutput"])
        self.assertTrue(labels["scene_label"]["suppressInOutput"])
        self.assertTrue(labels["subtitle"]["suppressInOutput"])
        self.assertFalse(labels["system"].get("suppressInOutput", False))
        self.assertFalse(labels["other"].get("suppressInOutput", False))
        output = script_to_markdown({"title": "第1集", "result": script})
        self.assertEqual(output.count("林川：走。"), 2)
        self.assertIn("任务完成", output)
        self.assertIn("【画面文字：林川】", output)
        self.assertNotIn("【字幕：客厅】", output)
        self.assertNotIn("【字幕：", output)

    def task(self, text, block_type="dialogue"):
        return {"id": "fixture", "title": "第1集", "status": "done", "result": {
            "title": "第1集", "scenes": [{"heading": "1-1 日 内 客厅", "blocks": [
                {"type": block_type, "speaker": "林川", "performance": "愤怒", "text": text},
            ]}],
        }}

    def test_dialogue_vo_and_os_omit_only_whole_utterance_wrappers(self):
        cases = [
            ("你故意的！", "你故意的！"),
            ("  “你故意的！”  ", "你故意的！"),
            ('"Let\'s go!"', "Let's go!"),
            ("「我们出发吧。」", "我们出发吧。"),
            ("他说‘别动’，你听见了吗？", "他说‘别动’，你听见了吗？"),
            ("“他说‘别动’，你听见了吗？”", "他说‘别动’，你听见了吗？"),
            ("“他说“别动”，你听见了吗？”", "他说“别动”，你听见了吗？"),
            ("“你好！”她说，“再见。”", "“你好！”她说，“再见。”"),
            ('"yes" or "no"', '"yes" or "no"'),
            ('"He said \\"go\\"."', 'He said \\"go\\".'),
            ("「你好」「再见」", "「你好」「再见」"),
            ("“未闭合", "“未闭合"),
            ('“不匹配"', '“不匹配"'),
        ]
        for kind, label in (("dialogue", "林川（愤怒）"), ("vo", "林川（VO）"), ("os", "林川（OS）")):
            for source, expected in cases:
                with self.subTest(kind=kind, source=source):
                    task = self.task(source, kind)
                    original = deepcopy(task)
                    self.assertIn(f"{label}：{expected}\n", script_to_markdown(task))
                    self.assertEqual(task, original)

    def test_action_and_screen_text_keep_quotes(self):
        task = self.task("“请进。”", "action")
        task["result"]["scenes"][0]["blocks"].append({"type": "screen_text", "text": '告示：“请安静”'})
        output = script_to_markdown(task)
        self.assertIn("“请进。”", output)
        self.assertIn('【画面文字：告示：“请安静”】', output)

    def test_single_batch_and_all_downloads_in_txt_and_md(self):
        task = self.task("“他说‘别动’，你听见了吗？”")
        original = deepcopy(task)
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchone.return_value = None
        connection.__enter__.return_value.execute.return_value.fetchall.return_value = [
            {"file_name": "1.mp4", "batch_index": 1, "batch_title": "测试批次", "created_at": "2026-11-09T00:00:00Z"},
        ]
        with (patch.object(routes, "current_user", return_value={"id": "fixture-owner"}),
              patch.object(routes, "get_task", return_value=task),
              patch.object(routes, "db", return_value=connection),
              patch.object(routes, "row_to_task", return_value=task)):
            for fmt in ("txt", "md"):
                for download in (lambda: routes.download_task(None, "fixture", fmt),
                                 lambda: routes.download_batch(None, "fixture-batch", fmt),
                                 lambda: routes.download_all_tasks(None, fmt)):
                    with self.subTest(fmt=fmt, download=download):
                        response = download()
                        self.assertIn("林川（愤怒）：他说‘别动’，你听见了吗？\n", response.body.decode())
                        self.assertNotIn("：“", response.body.decode())
                        self.assertIn("attachment;", response.headers["Content-Disposition"])
        self.assertEqual(task, original)


if __name__ == "__main__":
    unittest.main()
