import unittest

from app.script import clean_action_text, episode_from_text, normalize_script


class ScriptQualityRegressionTests(unittest.TestCase):
    def test_episode_markers_in_upload_names(self):
        self.assertEqual(episode_from_text("2.mp4"), 2)
        self.assertEqual(episode_from_text("episode_02_final.mp4"), 2)
        self.assertIsNone(episode_from_text("video_2026-10.mp4"))

    def test_batch_scene_order_and_editorial_notes(self):
        script = {
            "characters": ["王铁柱"],
            "characterProfiles": [
                {
                    "name": "王铁柱",
                    "aliases": ["王工"],
                    "appearance": "黑色短发",
                    "clothing": "深色工装",
                }
            ],
            "scenes": [
                {
                    "heading": "3-2 日 外 院子",
                    "location": "院子",
                    "blocks": [
                        {"type": "action", "text": "王工拿起水杯，然后递给孩子。"},
                        {"type": "dialogue", "speaker": "王铁柱", "text": "这里不需要展示"},
                    ],
                },
                {
                    "heading": "2-2 日 外 院子",
                    "location": "院子",
                    "blocks": [{"type": "action", "text": "王铁柱站住。"}],
                },
                {
                    "heading": "2-1 日 外 门口",
                    "location": "门口",
                    "blocks": [
                        {"type": "action", "text": "穿深色工装的王铁柱走到门口。"},
                        {"type": "screen_text", "text": "【字幕：你好】 1：这里是说话"},
                    ],
                },
            ],
        }
        result = normalize_script(script, "demo")
        self.assertEqual([scene["heading"].split()[0] for scene in result["scenes"]], ["2-1", "2-2", "3-1"])
        self.assertTrue(any(block["type"] == "screen_text" for block in result["scenes"][0]["blocks"]))
        self.assertFalse(any("这里不需要" in block["text"] for scene in result["scenes"] for block in scene["blocks"]))
        first_action = result["scenes"][0]["blocks"][0]["text"]
        self.assertIn("深色工装", first_action)
        self.assertIn("黑色短发", first_action)

    def test_micro_action_is_not_lost(self):
        self.assertIn("指尖发白", clean_action_text("指尖发白。"))


if __name__ == "__main__":
    unittest.main()
