"""Downloads omit dialogue wrappers without changing stored screenplay text."""
from copy import deepcopy
from unittest.mock import MagicMock, patch
import unittest

from app import routes
from app.script import script_to_markdown


class DialogueExportTests(unittest.TestCase):
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
        self.assertIn('【字幕：告示：“请安静”】', output)

    def test_single_batch_and_all_downloads_in_txt_and_md(self):
        task = self.task("“他说‘别动’，你听见了吗？”")
        original = deepcopy(task)
        connection = MagicMock()
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
