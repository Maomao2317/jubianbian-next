import asyncio
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from app import processing


class ProcessTaskTranslationTests(unittest.TestCase):
    def test_sqlite_row_language_fields_reach_bilingual_delivery(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT, title TEXT, file_name TEXT, attempts INTEGER,
                user_id TEXT, credits_used INTEGER, batch_id TEXT,
                source_language TEXT, output_language TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO tasks VALUES
              ('task-bilingual', '测试', 'test.mp4', 0, 'user', 1, NULL, 'zh', 'both')
            """
        )
        row = connection.execute("SELECT * FROM tasks").fetchone()
        script = {
            "title": "测试",
            "scenes": [{"heading": "1-1 日 内 客厅", "blocks": [
                {"type": "dialogue", "speaker": "甲", "text": "你好"},
            ]}],
        }
        delivery = {
            **script,
            "scenes": [{"heading": "1-1 日 内 客厅", "blocks": [
                {"type": "dialogue", "speaker": "甲", "text": "你好\nHello"},
            ]}],
        }
        usage = {"provider": "ark", "model": "doubao-seed-2-1-lite-260915",
                 "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                 "api_cost_rmb": 0.01}
        model_call = AsyncMock(return_value=(delivery, {
            "provider": "ark", "model": "doubao-seed-2-1-lite-260915",
            "callCount": 1, "input_tokens": 4, "output_tokens": 3,
            "total_tokens": 7, "api_cost_rmb": 0.002,
        }))
        try:
            with (
                patch.object(processing, "task_row", return_value=row),
                patch.object(processing, "run_recognizer", new=AsyncMock(
                    return_value=(script, {"blockingIssues": []}, usage, {}))),
                patch.object(processing, "_run_model_call", new=model_call),
                patch.object(processing, "quality_gate", return_value=(True, [])),
                patch.object(processing, "mark_task_stage"),
                patch.object(processing, "record_task_event"),
                patch.object(processing, "has_pending_recheck", return_value=False),
                patch.object(processing, "discard_local_proxy"),
                patch.object(processing, "update_task") as update,
            ):
                asyncio.run(processing.process_task("task-bilingual"))

            model_call.assert_awaited_once()
            self.assertEqual(model_call.await_args.args[2:], ("zh", "both"))
            persisted = update.call_args.kwargs
            self.assertEqual(persisted["status"], "done")
            self.assertEqual(
                json.loads(persisted["delivery_json"])["scenes"][0]["blocks"][0]["text"],
                "你好\nHello",
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
