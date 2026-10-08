import unittest
from pathlib import Path

from app.evidence import collect_evidence, evidence_summary, transcript_from_evidence
from app.script import clean_action_text, episode_from_text, normalize_script, quality_gate, script_quality, script_to_markdown


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

    def test_uncertain_evidence_is_explicit_in_export(self):
        script = normalize_script(
            {
                "characters": [],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [
                        {"type": "dialogue", "speaker": "未知说话人", "text": "你来了", "uncertain": True},
                        {"type": "screen_text", "text": "模糊字幕", "uncertain": True},
                        {"type": "action", "text": "未知人物拿起文件", "uncertain": True, "object": "文件", "result": "文件离开桌面"},
                    ],
                }],
            },
            "测试",
        )
        markdown = script_to_markdown({"title": "测试", "result": script})
        self.assertIn("【需核对·说话人】", markdown)
        self.assertIn("【需核对·字幕】", markdown)
        self.assertIn("【需核对·画面】", markdown)

    def test_quality_gate_blocks_unresolved_p0_but_allows_clean_script(self):
        uncertain = normalize_script(
            {
                "characters": [],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{"type": "dialogue", "speaker": "未知说话人", "text": "听不清", "uncertain": True}],
                }],
            },
            "测试",
        )
        blocked = script_quality(uncertain, "test")
        approved, issues = quality_gate(blocked)
        self.assertFalse(approved)
        self.assertTrue(any(item["severity"] == "P0" for item in issues))

        clean = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "characters": ["甲"],
                    "blocks": [{"type": "action", "text": "甲走向窗边", "object": "窗边", "result": "甲到达窗边"}],
                }],
            },
            "测试",
        )
        approved_quality = script_quality(clean, "test")
        self.assertIn(approved_quality["complexityBand"], {"simple", "standard", "complex"})
        self.assertEqual(approved_quality["reviewRecommendation"], "lite_only")
        approved, issues = quality_gate(approved_quality)
        self.assertTrue(approved)
        self.assertEqual(issues, [])

    def test_evidence_layer_preserves_transcript_and_marks_missing_sources(self):
        evidence = collect_evidence(
            Path("missing.mp4"),
            "demo",
            12,
            transcribe=lambda _: "原始对白，不得改写",
        )
        self.assertEqual(transcript_from_evidence(evidence), "原始对白，不得改写")
        self.assertIn("audio", evidence["availableSources"])
        self.assertIn("ocr: unavailable", evidence_summary({"sources": [{"kind": "ocr", "status": "unavailable", "reason": "缺少工具"}]}))

    def test_evidence_layer_never_fakes_transcript_when_provider_is_empty(self):
        evidence = collect_evidence(Path("missing.mp4"), "demo", 0, transcribe=lambda _: "")
        self.assertEqual(transcript_from_evidence(evidence), "")
        audio = next(source for source in evidence["sources"] if source["kind"] == "audio")
        self.assertEqual(audio["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
