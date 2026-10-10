import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from app import processing, providers
from app.evidence import collect_evidence, evidence_summary, transcript_from_evidence
from app.media import default_batch_title
from app.processing import _is_lite_model_unavailable, _is_non_retryable_provider_error, select_ark_route
from app.errors import ArkError
from app.script import clean_action_text, compact_character_detail, compact_emotion_text, episode_from_text, matching_character_counts, normalize_script, quality_gate, script_quality, script_to_markdown


class ScriptQualityRegressionTests(unittest.TestCase):
    def test_ark_upload_proxy_leaves_small_source_untouched(self):
        source = Path(__file__)
        with patch.object(providers, "ARK_UPLOAD_PROXY_MAX_MB", 32):
            upload_path, proxy_dir = providers._ark_upload_proxy(source)
        self.assertEqual(upload_path, source)
        self.assertIsNone(proxy_dir)

    def test_ark_proxy_profile_caps_long_video_near_target_size(self):
        with (
            patch.object(providers, "ARK_UPLOAD_PROXY_MAX_MB", 4),
            patch.object(providers, "ARK_UPLOAD_PROXY_VIDEO_BITRATE", "400k"),
            patch.object(providers, "ARK_UPLOAD_PROXY_AUDIO_BITRATE", "32k"),
            patch.object(providers, "ARK_UPLOAD_PROXY_MAX_HEIGHT", 540),
        ):
            bitrate, height = providers._ark_proxy_profile(180)
        self.assertLess(int(bitrate.removesuffix("k")), 180)
        self.assertEqual(height, 360)

    def test_ark_sampling_is_denser_for_short_fast_video(self):
        with patch.object(providers, "ARK_VIDEO_FPS", 0.5):
            self.assertEqual(providers.ark_sampling_fps(60), 2.0)
            self.assertEqual(providers.ark_sampling_fps(150), 1.0)
            self.assertEqual(providers.ark_sampling_fps(240), 0.5)

    def test_ark_stream_response_returns_completed_payload(self):
        completed = {
            "id": "resp-test",
            "status": "completed",
            "output": [{"content": [{"text": "OK"}]}],
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                events = [
                    {"type": "response.created", "response": {"id": "resp-test", "status": "in_progress"}},
                    {"type": "response.completed", "response": completed},
                ]
                return iter([f"data: {providers.json.dumps(event)}\n".encode() for event in events])

        with patch.object(providers, "urlopen", return_value=FakeResponse()):
            payload = providers.ark_stream_response({"model": "test", "input": "hello"}, "key")
        self.assertEqual(payload, completed)

    def test_ark_stream_response_polls_after_disconnect(self):
        completed = {"id": "resp-test", "status": "completed", "output": [{"content": [{"text": "OK"}]}]}

        class DisconnectingResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                yield b'data: {"type":"response.created","response":{"id":"resp-test","status":"in_progress"}}\n'
                raise TimeoutError("read operation timed out")

        with (
            patch.object(providers, "urlopen", return_value=DisconnectingResponse()),
            patch.object(providers, "_ark_poll_response", return_value=completed) as poll,
        ):
            payload = providers.ark_stream_response({"model": "test", "input": "hello"}, "key")
        poll.assert_called_once_with("resp-test", "key")
        self.assertEqual(payload, completed)

    def test_episode_markers_in_upload_names(self):
        self.assertEqual(episode_from_text("2.mp4"), 2)
        self.assertEqual(episode_from_text("episode_02_final.mp4"), 2)
        self.assertIsNone(episode_from_text("video_2026-10.mp4"))

    def test_auto_batch_title_uses_shanghai_upload_time(self):
        self.assertEqual(default_batch_title("2026-10-08T06:06:00+00:00"), "短剧批次 2026-10-08 14:06")

    def test_lite_model_failure_is_non_retryable_and_detected(self):
        error = ArkError("方舟接口请求失败（HTTP 400）：ModelNotOpen model not active", status_code=400, provider_code="ModelNotOpen")
        self.assertTrue(_is_lite_model_unavailable(error))
        self.assertTrue(_is_non_retryable_provider_error(error))
        self.assertTrue(_is_non_retryable_provider_error(TimeoutError("The read operation timed out")))

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

    def test_character_description_is_compact_and_keeps_concrete_details(self):
        detail = compact_character_detail("面容温柔、梳麻花辫的长麻花辫、浅蓝碎花衫、浅蓝碎花翻领衬衫、深色长裤")
        self.assertLessEqual(len(detail), 42)
        self.assertIn("长麻花辫", detail)
        self.assertIn("浅蓝碎花翻领衬衫", detail)
        self.assertNotIn("长麻花辫的长麻花辫", detail)

    def test_repeated_emotion_is_compacted(self):
        emotion = compact_emotion_text("神情紧张，情绪紧张，语气急切，声音急切")
        self.assertEqual(emotion, "神情紧张、语气急切")

    def test_evidence_matching_handles_repetitive_transcript_quickly(self):
        matched, reference_size, candidate_size = matching_character_counts("啊" * 12000, "啊" * 12000)
        self.assertEqual((matched, reference_size, candidate_size), (12000, 12000, 12000))

    def test_verbatim_dialogue_wins_over_polished_text(self):
        script = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{
                        "type": "dialogue",
                        "speaker": "甲",
                        "rawText": "我没有拿你的东西",
                        "finalText": "我没碰过你的东西",
                        "text": "我没碰过你的东西",
                    }],
                }],
            },
            "测试",
        )
        line = script["scenes"][0]["blocks"][0]
        self.assertEqual(line["text"], "我没有拿你的东西。")
        self.assertEqual(line["rawText"], "我没有拿你的东西")

    def test_independent_audio_evidence_blocks_rewritten_dialogue(self):
        script = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{"type": "dialogue", "speaker": "甲", "text": "我今天一直在公司开会，没有去过你家"}],
                }],
            },
            "测试",
        )
        matching_evidence = {
            "sources": [{
                "kind": "audio",
                "status": "available",
                "items": [{"type": "transcript", "text": "我今天一直在公司开会，没有去过你家。"}],
            }],
        }
        clean_quality = script_quality(script, "test", matching_evidence)
        self.assertEqual(clean_quality["transcriptCoverage"], 100)
        self.assertEqual(clean_quality["dialogueEvidencePrecision"], 100)
        self.assertTrue(quality_gate(clean_quality)[0])

        conflicting_evidence = {
            "sources": [{
                "kind": "audio",
                "status": "available",
                "items": [{"type": "transcript", "text": "你昨晚明明来到我家，还从桌上拿走了那份文件。"}],
            }],
        }
        conflicting_quality = script_quality(script, "test", conflicting_evidence)
        approved, issues = quality_gate(conflicting_quality)
        self.assertFalse(approved)
        self.assertTrue(any(item["tag"] == "音频对白不一致" for item in issues))

    def test_time_anchored_asr_finds_one_missing_line_inside_longer_transcript(self):
        script = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [
                        {"type": "dialogue", "speaker": "甲", "text": "第一句话完整保留"},
                        {"type": "dialogue", "speaker": "甲", "text": "第三句话也完整保留"},
                    ],
                }],
            },
            "测试",
        )
        evidence = {
            "sources": [{
                "kind": "audio",
                "status": "available",
                "items": [
                    {"type": "transcript_segment", "text": "第一句话完整保留", "startSec": 1, "endSec": 2},
                    {"type": "transcript_segment", "text": "中间这一句被漏掉了", "startSec": 3, "endSec": 4},
                    {"type": "transcript_segment", "text": "第三句话也完整保留", "startSec": 5, "endSec": 6},
                ],
            }],
        }
        quality = script_quality(script, "test", evidence)
        missing = next(issue for issue in quality["issues"] if issue["tag"] == "疑似漏台词片段")
        self.assertEqual((missing["startSec"], missing["endSec"]), (3, 4))
        self.assertEqual(quality["evidenceProblemRanges"][0]["kind"], "audio")
        self.assertFalse(quality_gate(quality)[0])

    def test_repeated_asr_line_is_not_deduplicated_before_coverage(self):
        script = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{"type": "dialogue", "speaker": "甲", "text": "你快回来这里"}],
                }],
            },
            "测试",
        )
        evidence = {
            "sources": [{
                "kind": "audio",
                "status": "available",
                "items": [
                    {"type": "transcript_segment", "text": "你快回来这里", "startSec": 1, "endSec": 2},
                    {"type": "transcript_segment", "text": "你快回来这里", "startSec": 3, "endSec": 4},
                ],
            }],
        }
        quality = script_quality(script, "test", evidence)
        self.assertLess(quality["transcriptCoverage"], 72)
        self.assertFalse(quality_gate(quality)[0])

    def test_repeated_ocr_frames_do_not_count_as_missing_subtitles(self):
        script = normalize_script(
            {
                "characters": [],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{"type": "screen_text", "text": "你今天来了。", "startSec": 1, "endSec": 3}],
                }],
            },
            "测试",
        )
        evidence = {
            "sources": [{
                "kind": "ocr",
                "status": "available",
                "items": [
                    {"type": "screen_text", "text": "你今天来了", "startSec": 1},
                    {"type": "screen_text", "text": "你今天来了", "startSec": 2},
                    {"type": "screen_text", "text": "你今天来了", "startSec": 3},
                ],
            }],
        }
        quality = script_quality(script, "test", evidence)
        self.assertEqual(quality["ocrCoverage"], 100)
        self.assertTrue(quality_gate(quality)[0])

    def test_uncertain_evidence_stays_out_of_export_body(self):
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
        self.assertNotIn("需核对", markdown)
        self.assertIn("你来了", markdown)
        self.assertIn("模糊字幕", markdown)
        self.assertIn("未知人物拿起文件", markdown)

    def test_mixed_speaker_line_is_split_and_marked_uncertain(self):
        script = normalize_script(
            {
                "characters": ["甲", "乙"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{
                        "type": "dialogue",
                        "speaker": "甲",
                        "text": "甲：你先走。乙：我不走。",
                    }],
                }],
            },
            "测试",
        )
        blocks = script["scenes"][0]["blocks"]
        self.assertEqual([block["speaker"] for block in blocks], ["甲", "乙"])
        self.assertTrue(all(block.get("uncertain") for block in blocks))

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

    def test_missing_independent_transcript_is_explicit_and_never_a_percentage(self):
        script = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{"type": "dialogue", "speaker": "甲", "text": "我今天一直在公司开会"}],
                }],
            },
            "测试",
        )
        evidence = {
            "sources": [
                {"kind": "audio", "status": "unavailable", "reason": "测试环境没有 ASR", "items": []},
                {"kind": "ocr", "status": "available", "items": []},
            ]
        }
        quality = script_quality(script, "test", evidence)
        self.assertIsNone(quality["transcriptCoverage"])
        self.assertIsNone(quality["dialogueEvidencePrecision"])
        self.assertTrue(any(issue["tag"] == "独立音频转写缺失" for issue in quality["issues"]))
        self.assertFalse(quality_gate(quality)[0])

    def test_ark_route_uses_turbo_for_complex_or_evidence_poor_video(self):
        simple = select_ark_route(
            30,
            {
                "sources": [
                    {"kind": "audio", "status": "available", "items": [{"text": "你好"}]},
                    {"kind": "ocr", "status": "available", "items": []},
                    {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
                ]
            },
        )
        complex_route = select_ark_route(
            210,
            {
                "sources": [
                    {"kind": "audio", "status": "unavailable", "items": []},
                    {"kind": "ocr", "status": "unavailable", "items": []},
                    {"kind": "keyframes", "status": "unavailable", "items": []},
                ]
            },
        )
        self.assertEqual(simple["band"], "simple")
        self.assertEqual(complex_route["band"], "complex")
        self.assertIn("duration>=180s", complex_route["reasons"])

        short_without_independent_evidence = select_ark_route(
            30,
            {
                "sources": [
                    {"kind": "audio", "status": "unavailable", "items": []},
                    {"kind": "ocr", "status": "unavailable", "items": []},
                    {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
                ]
            },
        )
        self.assertEqual(short_without_independent_evidence["band"], "complex")
        self.assertIn("independent_audio_and_ocr_unavailable", short_without_independent_evidence["reasons"])

        short_without_audio = select_ark_route(
            30,
            {
                "sources": [
                    {"kind": "audio", "status": "unavailable", "items": []},
                    {"kind": "ocr", "status": "available", "items": [{"text": "画面字幕"}]},
                    {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
                ]
            },
        )
        self.assertEqual(short_without_audio["band"], "complex")

    def test_lite_blocking_result_gets_one_turbo_review_and_can_finish(self):
        evidence = {
            "status": "available",
            "summary": "test",
            "sources": [
                {"kind": "audio", "status": "available", "items": [{"type": "transcript", "text": "你好"}]},
                {"kind": "ocr", "status": "available", "items": []},
                {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
            ],
        }
        bad = {
            "characters": ["甲"],
            "scenes": [{
                "heading": "1-1 日 内 客厅",
                "location": "客厅",
                "blocks": [{"type": "dialogue", "speaker": "未知说话人", "text": "你好", "uncertain": True, "startSec": 1, "endSec": 2}],
            }],
        }
        repaired = {
            "characters": ["甲"],
            "scenes": [{
                "heading": "1-1 日 内 客厅",
                "location": "客厅",
                "blocks": [{"type": "dialogue", "speaker": "甲", "text": "你好", "startSec": 1, "endSec": 2}],
            }],
        }
        row = {"id": "task-1", "stored_path": str(Path(__file__).resolve()), "title": "测试", "duration_sec": 30}
        with (
            patch.object(processing, "ARK_API_KEYS", ("key",)),
            patch.object(processing, "ARK_LITE_MODEL", "lite"),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "ARK_MODEL", "turbo"),
            patch.object(processing, "ARK_ROUTING_MODE", "complexity"),
            patch.object(processing, "probe_duration", return_value=30),
            patch.object(processing, "collect_evidence", return_value=evidence),
            patch.object(processing, "update_task"),
            patch.object(
                processing,
                "ark_recognize",
                side_effect=[
                    (bad, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "api_cost_rmb": 0.1}),
                    (repaired, {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28, "api_cost_rmb": 0.2}),
                ],
            ) as recognize,
        ):
            script, quality, usage, _ = asyncio.run(processing.run_recognizer(row))
        self.assertEqual(recognize.call_count, 2)
        self.assertEqual(script["scenes"][0]["blocks"][0]["speaker"], "甲")
        self.assertTrue(quality_gate(quality)[0])
        self.assertEqual(quality["modelRoute"], "turbo_review")
        self.assertEqual(quality["automatedReview"]["attemptCount"], 2)
        self.assertEqual(usage["model"], "turbo")
        self.assertEqual(usage["total_tokens"], 43)

    def test_turbo_review_is_not_recursive_when_issues_remain(self):
        evidence = {
            "status": "available",
            "summary": "test",
            "sources": [
                {"kind": "audio", "status": "available", "items": [{"type": "transcript", "text": "你好"}]},
                {"kind": "ocr", "status": "available", "items": []},
                {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
            ],
        }
        unresolved = {
            "characters": ["甲"],
            "scenes": [{
                "heading": "1-1 日 内 客厅",
                "location": "客厅",
                "blocks": [{"type": "dialogue", "speaker": "未知说话人", "text": "你好", "uncertain": True}],
            }],
        }
        row = {"id": "task-2", "stored_path": str(Path(__file__).resolve()), "title": "测试", "duration_sec": 30}
        with (
            patch.object(processing, "ARK_API_KEYS", ("key",)),
            patch.object(processing, "ARK_LITE_MODEL", "lite"),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "ARK_MODEL", "turbo"),
            patch.object(processing, "ARK_ROUTING_MODE", "complexity"),
            patch.object(processing, "probe_duration", return_value=30),
            patch.object(processing, "collect_evidence", return_value=evidence),
            patch.object(processing, "update_task"),
            patch.object(
                processing,
                "ark_recognize",
                side_effect=[
                    (unresolved, {"total_tokens": 10, "api_cost_rmb": 0}),
                    (unresolved, {"total_tokens": 12, "api_cost_rmb": 0}),
                ],
            ) as recognize,
        ):
            _script, quality, _usage, _ = asyncio.run(processing.run_recognizer(row))
        self.assertEqual(recognize.call_count, 2)
        self.assertFalse(quality_gate(quality)[0])
        self.assertTrue(quality["automatedReview"]["unresolvedIssues"])


if __name__ == "__main__":
    unittest.main()
