import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app import processing, providers
from app.evidence import collect_evidence, evidence_summary, transcript_from_evidence
from app.media import default_batch_title
from app.processing import _is_lite_model_unavailable, _is_non_retryable_provider_error, select_ark_route
from app.errors import ArkError
from app.script import apply_batch_character_continuity, clean_action_text, compact_character_detail, compact_emotion_text, compact_performance, episode_from_text, matching_character_counts, normalize_script, quality_gate, restore_evidence_boundary_segments, restore_short_transcript_segments, restore_transcript_gap_segments, script_quality, script_to_markdown


class ScriptQualityRegressionTests(unittest.TestCase):
    def test_non_story_disclaimer_is_removed_but_plot_screen_text_remains(self):
        script = normalize_script({
            "characters": ["晓晨"],
            "scenes": [{
                "heading": "1-1 日 内 客厅",
                "location": "客厅",
                "blocks": [
                    {"type": "screen_text", "screenType": "system", "text": "【字幕:本故事纯属虚构无不良导向切勿模仿】"},
                    {"type": "screen_text", "screenType": "system", "text": "系统提示：账户将在今晚冻结"},
                    {"type": "action", "text": "晓晨看向手机。"},
                ],
            }],
        }, "第1集")

        overlays = [block["text"] for block in script["scenes"][0]["blocks"]
                    if block["type"] == "screen_text"]
        self.assertEqual(overlays, ["系统提示：账户将在今晚冻结"])
        self.assertNotIn("本故事纯属虚构", script_to_markdown({"title": "第1集", "result": script}))

    def test_consecutive_lines_from_same_speaker_remain_sentence_by_sentence(self):
        script = normalize_script({
            "characters": ["晓晨", "林川"],
            "scenes": [{
                "heading": "1-1 日 内 客厅",
                "location": "客厅",
                "blocks": [
                    {"type": "dialogue", "speaker": "晓晨", "text": "你回来了", "startSec": 1, "endSec": 2},
                    {"type": "dialogue", "speaker": "晓晨", "text": "我等了很久", "startSec": 2.1, "endSec": 3},
                    {"type": "dialogue", "speaker": "林川", "text": "对不起", "startSec": 3.1, "endSec": 4},
                    {"type": "dialogue", "speaker": "晓晨", "text": "别说了", "startSec": 4.1, "endSec": 5},
                ],
            }],
        }, "第1集")

        dialogue = [block for block in script["scenes"][0]["blocks"] if block["type"] == "dialogue"]
        self.assertEqual([(block["speaker"], block["text"]) for block in dialogue], [
            ("晓晨", "你回来了。"),
            ("晓晨", "我等了很久。"),
            ("林川", "对不起。"),
            ("晓晨", "别说了。"),
        ])
        self.assertEqual(script_to_markdown({"title": "第1集", "result": script}).count("晓晨："), 3)

    def test_only_overlapping_exact_dialogue_duplicate_is_dropped(self):
        script = normalize_script({
            "characters": ["晓晨"],
            "scenes": [{
                "heading": "1-1 日 内 客厅",
                "location": "客厅",
                "blocks": [
                    {"type": "dialogue", "speaker": "晓晨", "text": "等等", "startSec": 1, "endSec": 2},
                    {"type": "dialogue", "speaker": "晓晨", "text": "等等", "startSec": 1.02, "endSec": 2.02},
                    {"type": "dialogue", "speaker": "晓晨", "text": "等等", "startSec": 5, "endSec": 6},
                ],
            }],
        }, "第1集")

        dialogue = [block for block in script["scenes"][0]["blocks"] if block["type"] == "dialogue"]
        self.assertEqual(len(dialogue), 2)
        self.assertEqual([block["text"] for block in dialogue], ["等等。", "等等。"])
        self.assertEqual([(block["startSec"], block["endSec"]) for block in dialogue], [(1.0, 2.0), (5.0, 6.0)])

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

    def test_ark_sampling_respects_configured_budget_for_every_duration(self):
        with patch.object(providers, "ARK_VIDEO_FPS", 0.5):
            for duration in (0, 60, 90, 150, 240):
                self.assertEqual(providers.ark_sampling_fps(duration), 0.5)

    def test_ark_upload_retries_transient_network_without_reencoding(self):
        source = Path(__file__)
        with (
            patch.object(providers, "ARK_UPLOAD_ATTEMPTS", 2),
            patch.object(providers, "_ark_upload_proxy", return_value=(source, None)) as proxy,
            patch.object(
                providers,
                "ark_http",
                side_effect=[
                    ArkError("write timeout", provider_code="transient_network"),
                    {"id": "file-test"},
                ],
            ) as request,
            patch.object(providers.time, "sleep"),
        ):
            file_id, api_key = providers.ark_upload_video(source, "key-test", 120)
        proxy.assert_called_once_with(source, 120)
        self.assertEqual(request.call_count, 2)
        self.assertEqual((file_id, api_key), ("file-test", "key-test"))

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
            patch.object(providers, "urlopen", return_value=DisconnectingResponse()) as create,
            patch.object(providers, "ARK_STREAM_IDLE_TIMEOUT_SECONDS", 45),
            patch.object(providers, "ARK_RESPONSE_TIMEOUT_SECONDS", 420),
            patch.object(providers, "_ark_poll_response", return_value=completed) as poll,
        ):
            payload = providers.ark_stream_response({"model": "test", "input": "hello"}, "key")
        poll.assert_called_once_with("resp-test", "key")
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.kwargs["timeout"], 45)
        self.assertEqual(payload, completed)

    def test_ark_poll_retries_transient_network_without_losing_response(self):
        completed = {"id": "resp-test", "status": "completed", "output": []}
        with (
            patch.object(
                providers,
                "ark_http",
                side_effect=[ArkError("temporary", provider_code="transient_network"), completed],
            ) as request,
            patch.object(providers.time, "sleep"),
            patch.object(providers, "ARK_RESPONSE_POLL_TIMEOUT_SECONDS", 30),
        ):
            payload = providers._ark_poll_response("resp-test", "key")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(payload, completed)

    def test_ark_response_creation_retry_reuses_prepared_video(self):
        completed = {
            "id": "resp-test",
            "status": "completed",
            "output": [{"content": [{"text": '{"title":"测试","scenes":[{"heading":"1-1 日 内 客厅","location":"客厅","blocks":[{"type":"dialogue","speaker":"甲","text":"你好"}]}]}'}]}],
        }
        for error in (ArkError("socket closed", provider_code="transient_network"),
                      ArkError("limited", status_code=429)):
            with (
                self.subTest(error=error),
                patch.object(providers, "ARK_RESPONSE_CREATE_ATTEMPTS", 2),
                patch.object(providers, "ark_stream_response", side_effect=[error, completed]) as response,
                patch.object(providers, "ark_prepare_video") as prepare,
                patch.object(providers.time, "sleep"),
            ):
                script, _usage = providers.ark_recognize(
                    Path(__file__), "测试", 30, model="lite",
                    prepared_video={"api_key": "key", "file_id": "file"},
                )
                prepare.assert_not_called()
                self.assertEqual(response.call_count, 2)
                self.assertEqual(response.call_args.args[0]["input"][0]["content"][0]["file_id"], "file")
                self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "你好。")

    def test_episode_markers_in_upload_names(self):
        self.assertEqual(episode_from_text("2.mp4"), 2)
        self.assertEqual(episode_from_text("episode_02_final.mp4"), 2)
        self.assertIsNone(episode_from_text("video_2026-10.mp4"))

    def test_auto_batch_title_uses_shanghai_upload_time(self):
        self.assertEqual(default_batch_title("2026-10-08T06:06:00+00:00"), "短剧批次 2026-10-08 14:06")

    def test_lite_model_failure_is_non_retryable_but_timeout_can_recover(self):
        error = ArkError("方舟接口请求失败（HTTP 400）：ModelNotOpen model not active", status_code=400, provider_code="ModelNotOpen")
        self.assertTrue(_is_lite_model_unavailable(error))
        self.assertTrue(_is_non_retryable_provider_error(error))
        self.assertFalse(_is_non_retryable_provider_error(TimeoutError("The read operation timed out")))

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

    def test_repeated_emotion_with_ideographic_comma_is_compacted(self):
        emotion = compact_emotion_text("委屈又气愤、委屈又气愤、委屈又气愤")
        self.assertEqual(emotion, "委屈又气愤")

    def test_repeated_performance_clauses_with_ideographic_comma_are_compacted(self):
        performance = compact_performance("目光愁切、关切、关切、关切、从看文件")
        self.assertEqual(performance, "目光愁切、关切")

    def test_batch_character_continuity_uses_first_episode_name(self):
        first = {
            "characters": ["江川"],
            "characterProfiles": [{"name": "江川", "aliases": []}],
            "scenes": [{"characters": ["江川"], "blocks": [{"type": "dialogue", "speaker": "江川", "text": "我来了"}]}],
        }
        second = {
            "characters": ["江川哥"],
            "characterProfiles": [{"name": "江川哥", "aliases": []}],
            "scenes": [{"characters": ["江川哥"], "blocks": [{"type": "dialogue", "speaker": "江川哥", "text": "江川哥，你来了"}]}],
        }
        changed = apply_batch_character_continuity([first, second])
        self.assertEqual(changed, 1)
        self.assertEqual(second["characters"], ["江川"])
        self.assertEqual(second["scenes"][0]["blocks"][0]["speaker"], "江川")
        self.assertEqual(second["scenes"][0]["blocks"][0]["text"], "江川哥，你来了")

    def test_interior_transcript_gap_is_recovered_only_when_unambiguous(self):
        script = {
            "scenes": [{
                "blocks": [{
                    "type": "dialogue",
                    "speaker": "甲",
                    "text": "我决定。",
                    "startSec": 1.0,
                    "endSec": 3.0,
                }],
            }],
        }
        evidence = {"sources": [{
            "kind": "audio",
            "status": "available",
            "items": [{"type": "transcript_segment", "text": "我今天决定", "startSec": 1.1, "endSec": 2.8}],
        }]}
        repaired = restore_transcript_gap_segments(script, evidence)
        self.assertEqual(repaired, {"segments": 1, "blocks": 1})
        self.assertEqual(script["scenes"][0]["blocks"][0]["text"], "我今天决定。")

    def test_repeated_character_name_is_collapsed_and_emotion_is_exported(self):
        normalized = normalize_script(
            {
                "characters": ["周景明"],
                "characterProfiles": [{"name": "周景明", "aliases": []}],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [
                        {"type": "action", "text": "周周周景明抬头看向门口。", "emotion": "紧张"},
                        {"type": "dialogue", "speaker": "周周周景明", "text": "你来了", "emotionType": "震惊", "emotionEvidence": "睁大眼睛"},
                    ],
                }],
            },
            "测试",
        )
        blocks = normalized["scenes"][0]["blocks"]
        self.assertEqual(blocks[0]["text"], "周景明抬头看向门口。")
        self.assertEqual(blocks[1]["speaker"], "周景明")
        self.assertIn("震惊", blocks[1]["performance"])
        exported = script_to_markdown({"title": "测试", "result": normalized})
        self.assertIn("周景明（震惊）", exported)
        self.assertIn("▲ 周景明抬头看向门口。（紧张）", exported)

    def test_evidence_matching_handles_repetitive_transcript_quickly(self):
        matched, reference_size, candidate_size = matching_character_counts("啊" * 12000, "啊" * 12000)
        self.assertEqual((matched, reference_size, candidate_size), (12000, 12000, 12000))

    def test_alias_inside_canonical_name_is_not_expanded_on_each_review(self):
        script = {
            "characters": ["苏婉晴（小青）", "萧华音（皇后）"],
            "characterProfiles": [
                {"name": "苏婉晴（小青）", "aliases": ["小青"]},
                {"name": "萧华音（皇后）", "aliases": ["皇后"]},
            ],
            "scenes": [{"heading": "1-1 日 内 宫殿", "characters": ["小青", "皇后"], "blocks": [
                {"type": "action", "text": "苏婉晴（小青）看向皇后，小青点头。"},
                {"type": "dialogue", "speaker": "小青", "text": "皇后，皇后！",
                 "performance": "惊讶", "emotionType": "惊讶", "emotionEvidence": "睁大眼睛"},
                {"type": "dialogue", "speaker": "皇后", "text": "我在这里。"},
            ]}],
        }
        for _ in range(5):
            script = normalize_script(script, "第1集")
            action, dialogue = script["scenes"][0]["blocks"][:2]
            self.assertEqual(action["text"], "苏婉晴(小青)看向萧华音(皇后)，苏婉晴(小青)点头。")
            self.assertEqual(dialogue["speaker"], "苏婉晴（小青）")
            self.assertEqual(dialogue["text"], "皇后，皇后！")
            self.assertEqual(dialogue["performance"], "惊讶")

        script["scenes"][0]["blocks"][0]["text"] = (
            "苏婉晴（苏婉晴（苏婉晴（小青）））看向萧华音(萧华音(皇后))。"
        )
        repaired = normalize_script(script, "第1集")
        self.assertEqual(repaired["scenes"][0]["blocks"][0]["text"],
                         "苏婉晴(小青)看向萧华音(皇后)。")

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

    def test_explicit_speech_action_repairs_wrong_speaker(self):
        normalized = normalize_script(
            {
                "characters": ["甲", "乙"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "characters": ["甲", "乙"],
                    "blocks": [
                        {"type": "action", "text": "甲转向乙，开口问道。", "object": "乙", "result": "开口"},
                        {"type": "dialogue", "speaker": "乙", "text": "你去哪里"},
                    ],
                }],
            },
            "测试",
        )
        dialogue = normalized["scenes"][0]["blocks"][1]
        self.assertEqual(dialogue["speaker"], "甲")
        self.assertEqual(dialogue["speakerCorrectedFrom"], "乙")

    def test_non_lip_synced_character_voice_exports_as_parenthesized_os(self):
        normalized = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{
                        "type": "dialogue",
                        "speaker": "甲",
                        "mouthMoving": False,
                        "voiceSource": "internal",
                        "text": "今天，我一定要离开",
                    }],
                }],
            },
            "测试",
        )
        block = normalized["scenes"][0]["blocks"][0]
        self.assertEqual(block["type"], "vo")
        self.assertEqual(block["voKind"], "os")
        exported = script_to_markdown({"title": "测试", "result": normalized})
        self.assertIn("甲（OS）：", exported)

    def test_one_character_time_anchored_line_is_checked(self):
        script = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{"type": "dialogue", "speaker": "甲", "text": "今天", "startSec": 0, "endSec": 1}],
                }],
            },
            "测试",
        )
        evidence = {
            "sources": [
                {"kind": "audio", "status": "available", "items": [{"type": "transcript_segment", "text": "我", "startSec": 2, "endSec": 2.2}]},
                {"kind": "ocr", "status": "available", "items": []},
            ],
        }
        quality = script_quality(script, "test", evidence)
        self.assertTrue(any(issue["tag"] == "疑似漏台词片段" for issue in quality["issues"]))
        self.assertFalse(quality_gate(quality)[0])

    def test_short_asr_beats_restore_repeated_words_without_deduplication(self):
        script = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{
                        "type": "dialogue",
                        "speaker": "甲",
                        "text": "今天，决定",
                        "startSec": 0,
                        "endSec": 3,
                    }],
                }],
            },
            "测试",
        )
        evidence = {
            "sources": [{
                "kind": "audio",
                "status": "available",
                "items": [
                    {"type": "transcript_segment", "text": "今天", "startSec": 0.1, "endSec": 0.7},
                    {"type": "transcript_segment", "text": "我决定", "startSec": 1.0, "endSec": 1.8},
                    {"type": "transcript_segment", "text": "我", "startSec": 2.1, "endSec": 2.4},
                ],
            }],
        }

        repaired = restore_short_transcript_segments(script, evidence)

        blocks = script["scenes"][0]["blocks"]
        self.assertEqual(repaired, {"segments": 3, "blocks": 1})
        self.assertEqual([block["text"] for block in blocks], ["今天。", "我决定。", "我。"])
        self.assertTrue(all(block.get("asrRecovered") for block in blocks))
        self.assertTrue(quality_gate(script_quality(script, "test", evidence))[0])

    def test_boundary_evidence_restores_independently_confirmed_subtitle(self):
        script = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{
                        "type": "dialogue",
                        "speaker": "甲",
                        "text": "今天已经决定离开",
                        "startSec": 0.1,
                        "endSec": 2.8,
                    }, {"type": "screen_text", "screenType": "subtitle",
                        "text": "我今天已经决定离开", "startSec": 0, "endSec": 2.5,
                        "confidence": "high"}],
                }],
            },
            "测试",
        )
        evidence = {
            "sources": [{
                "kind": "ocr",
                "status": "available",
                "items": [{
                    "type": "screen_text",
                    "text": "我今天已经决定离开",
                    "startSec": 0.0,
                    "endSec": 2.5,
                }],
            }],
        }

        repaired = restore_evidence_boundary_segments(script, evidence)

        block = next(b for b in script["scenes"][0]["blocks"] if b["type"] == "dialogue")
        self.assertEqual(repaired, {"segments": 1, "blocks": 1})
        self.assertEqual(block["text"], "我今天已经决定离开。")
        self.assertTrue(block["subtitleRecovered"])

    def test_known_low_confidence_speaker_is_advisory_not_p0(self):
        script = normalize_script(
            {
                "characters": ["甲"],
                "scenes": [{
                    "heading": "1-1 日 内 客厅",
                    "location": "客厅",
                    "blocks": [{"type": "dialogue", "speaker": "甲", "text": "我知道", "uncertain": True}],
                }],
            },
            "测试",
        )

        quality = script_quality(script, "test")

        self.assertTrue(any(issue["tag"] == "对白置信度低" for issue in quality["issues"]))
        self.assertFalse(any(issue["tag"] == "台词归属错" for issue in quality["issues"]))
        self.assertTrue(quality_gate(quality)[0])

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
                    "blocks": [{"type": "action", "text": "甲站在客厅。", "startSec": 0, "endSec": 3},
                               {"type": "screen_text", "text": "你今天来了。", "startSec": 1, "endSec": 3}],
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

    def test_p0_after_more_than_100_advisories_still_blocks_and_enters_review(self):
        blocks = [{"type": "action", "text": "甲走向窗边。", "emotion": "紧张"} for _ in range(110)]
        blocks.append({"type": "dialogue", "speaker": "未知说话人", "text": "我来了。", "uncertain": True})
        quality = script_quality({"characters": ["甲"], "scenes": [{
            "heading": "1-1 日 内 客厅", "location": "客厅", "blocks": blocks}]}, "test")
        self.assertEqual(len(quality["issues"]), 100)
        self.assertFalse(any(issue["severity"] == "P0" for issue in quality["issues"]))
        self.assertFalse(quality_gate(quality)[0])
        self.assertIn("台词归属错", [issue["tag"] for issue in quality["blockingIssues"]])
        self.assertTrue(processing._needs_automated_content_review(quality))
        self.assertIn("台词归属错", [issue["tag"] for issue in processing._automated_review_issues(quality)])

    def test_legacy_truncated_p0_summary_cannot_be_approved(self):
        quality = {"issues": [{"severity": "P1", "tag": "普通提示"}], "severityCounts": {"P0": 2}}
        approved, blockers = quality_gate(quality)
        self.assertFalse(approved)
        self.assertEqual(blockers[0]["tag"], "阻断问题明细不完整")

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
        self.assertTrue(quality_gate(quality)[0])

    @patch.object(processing, "ARK_ROUTING_MODE", "complexity")
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
        self.assertEqual(simple["provider"], "ark")
        self.assertEqual(simple["band"], "simple")
        audio_without_ocr = select_ark_route(
            117,
            {
                "sources": [
                    {"kind": "audio", "status": "available", "items": [{"text": "完整对白"}]},
                    {"kind": "ocr", "status": "unavailable", "items": []},
                    {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
                ]
            },
        )
        self.assertEqual(audio_without_ocr["band"], "simple")
        self.assertIn("ocr_unavailable_optional", audio_without_ocr["reasons"])
        short_with_subtitles = select_ark_route(
            117,
            {
                "sources": [
                    {"kind": "audio", "status": "available", "items": [{"text": "完整对白"}]},
                    {"kind": "ocr", "status": "available", "items": [{"text": str(index)} for index in range(6)]},
                    {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
                ]
            },
        )
        self.assertEqual(short_with_subtitles["band"], "simple")
        self.assertIn("ocr>=3items", short_with_subtitles["reasons"])
        medium_with_rich_evidence = select_ark_route(
            158,
            {
                "sources": [
                    {"kind": "audio", "status": "available", "items": [{"text": "完整对白"}]},
                    {"kind": "ocr", "status": "available", "items": []},
                    {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
                ]
            },
        )
        self.assertEqual(medium_with_rich_evidence["band"], "complex")
        self.assertIn("duration>=120s", medium_with_rich_evidence["reasons"])
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

    def test_lite_first_keeps_complex_video_on_lite_initially(self):
        evidence = {
            "sources": [
                {"kind": "audio", "status": "unavailable", "items": []},
                {"kind": "ocr", "status": "unavailable", "items": []},
                {"kind": "keyframes", "status": "unavailable", "items": []},
            ]
        }
        with (
            patch.object(processing, "ARK_ROUTING_MODE", "lite_first"),
            patch.object(processing, "ARK_LITE_MODEL", "lite"),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
        ):
            route = select_ark_route(210, evidence)
        self.assertEqual(route["model"], "lite")
        self.assertEqual(route["band"], "simple")
        self.assertGreaterEqual(route["score"], 2)
        self.assertIn("routing_mode=lite_first", route["reasons"])

    @staticmethod
    def _funnel_evidence():
        return {
            "status": "available",
            "summary": "test",
            "durationSec": 30,
            "sources": [
                {"kind": "audio", "status": "available", "items": [
                    {"type": "transcript_segment", "text": "你好", "startSec": 1, "endSec": 2}
                ]},
                {"kind": "ocr", "status": "available", "items": []},
                {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
            ],
        }

    @staticmethod
    def _funnel_script(*, resolved: bool):
        dialogue = {
            "type": "dialogue",
            "speaker": "甲" if resolved else "未知说话人",
            "text": "你好",
            "startSec": 1,
            "endSec": 2,
        }
        if not resolved:
            dialogue["uncertain"] = True
        return {
            "characters": ["甲"],
            "scenes": [{
                "heading": "1-1 日 内 客厅",
                "location": "客厅",
                "blocks": [
                    {"type": "action", "text": "甲在客厅等候。", "startSec": 0, "endSec": 30},
                    dialogue,
                ],
            }],
        }

    def test_lite_first_approved_draft_finishes_after_one_fast_call(self):
        route = {"provider": "ark", "model": "lite", "mode": "lite_first",
                 "band": "simple", "score": 0, "reasons": ["routing_mode=lite_first"]}
        row = {"id": "task-fast", "title": "测试", "credits_used": 1}
        with (
            patch.object(processing, "ARK_API_KEYS", ("key",)),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "mark_task_stage"),
            patch.object(
                processing,
                "ark_recognize",
                return_value=(self._funnel_script(resolved=True), {
                    "total_tokens": 10, "api_cost_rmb": 0.02,
                }),
            ) as recognize,
        ):
            _script, quality, usage, _ = asyncio.run(processing._run_recognizer_with_media(
                row, Path("unused"), 30, self._funnel_evidence(), route, {}, None,
            ))
        self.assertEqual(recognize.call_count, 1)
        self.assertEqual([call.args[4] for call in recognize.call_args_list], ["lite"])
        self.assertTrue(quality_gate(quality)[0])
        self.assertEqual(quality["automatedReview"]["attemptCount"], 1)
        self.assertEqual(usage["model"], "lite")

    def test_lite_first_funnel_uses_two_lite_calls_before_one_turbo(self):
        route = {"provider": "ark", "model": "lite", "mode": "lite_first",
                 "band": "simple", "score": 0, "reasons": ["routing_mode=lite_first"]}
        row = {"id": "task-funnel", "title": "测试", "credits_used": 1}
        with (
            patch.object(processing, "ARK_API_KEYS", ("key",)),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "mark_task_stage"),
            patch.object(processing, "ark_recognize", side_effect=[
                (self._funnel_script(resolved=False), {"total_tokens": 10, "api_cost_rmb": 0.02}),
                (self._funnel_script(resolved=False), {"total_tokens": 8, "api_cost_rmb": 0.02}),
                (self._funnel_script(resolved=True), {"total_tokens": 12, "api_cost_rmb": 0.03}),
            ]) as recognize,
        ):
            _script, quality, usage, _ = asyncio.run(processing._run_recognizer_with_media(
                row, Path("unused"), 30, self._funnel_evidence(), route, {}, None,
            ))
        models = [call.args[4] for call in recognize.call_args_list]
        self.assertEqual(models, ["lite", "lite", "turbo"])
        self.assertGreater(models.count("lite"), models.count("turbo"))
        self.assertTrue(quality_gate(quality)[0])
        self.assertEqual(usage["model"], "turbo")

    def test_turbo_blocker_gets_one_final_lite_cleanup_without_second_turbo(self):
        route = {"provider": "ark", "model": "lite", "mode": "lite_first",
                 "band": "simple", "score": 0, "reasons": ["routing_mode=lite_first"]}
        row = {"id": "task-cleanup", "title": "测试", "credits_used": 1}
        with (
            patch.object(processing, "ARK_API_KEYS", ("key",)),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "mark_task_stage"),
            patch.object(processing, "ark_recognize", side_effect=[
                (self._funnel_script(resolved=False), {"total_tokens": 10, "api_cost_rmb": 0.02}),
                (self._funnel_script(resolved=False), {"total_tokens": 8, "api_cost_rmb": 0.02}),
                (self._funnel_script(resolved=False), {"total_tokens": 12, "api_cost_rmb": 0.03}),
                (self._funnel_script(resolved=True), {"total_tokens": 6, "api_cost_rmb": 0.01}),
            ]) as recognize,
        ):
            _script, quality, usage, _ = asyncio.run(processing._run_recognizer_with_media(
                row, Path("unused"), 30, self._funnel_evidence(), route, {}, None,
            ))
        models = [call.args[4] for call in recognize.call_args_list]
        self.assertEqual(models, ["lite", "lite", "turbo", "lite"])
        self.assertEqual(models.count("turbo"), 1)
        self.assertTrue(quality_gate(quality)[0])
        self.assertEqual(usage["model"], "lite")

    def test_content_repair_never_uses_two_lite_calls_after_turbo(self):
        route = {"provider": "ark", "model": "lite", "mode": "lite_first",
                 "band": "simple", "score": 0, "reasons": ["routing_mode=lite_first"]}
        row = {"id": "task-gap-cap", "title": "测试", "credits_used": 10, "attempts": 0}
        issue = {"severity": "P0", "tag": "missing", "description": "missing",
                 "startSec": 5, "endSec": 10}
        gap = [{"startSec": 5, "endSec": 10}]
        script = self._funnel_script(resolved=True)
        calls = []

        def quality(remaining):
            return {
                "issues": [issue], "blockingIssues": [issue],
                "severityCounts": {"P0": 1}, "contentGapRanges": remaining,
                "contentRepair": {
                    "attemptedRanges": [{"startSec": 5, "endSec": 10}],
                    "completedRanges": [] if remaining else gap,
                    "remainingRanges": remaining, "errors": [],
                },
            }

        def recognize(*args, **kwargs):
            calls.append(args[4])
            return script, {"total_tokens": 10, "api_cost_rmb": 0.01}

        repair_results = iter([
            (script, quality(gap), {"total_tokens": 5, "api_cost_rmb": 0.01}),
            (script, quality(gap), {"total_tokens": 5, "api_cost_rmb": 0.01}),
            (script, quality([]), {"total_tokens": 5, "api_cost_rmb": 0.01}),
        ])

        async def repair(*args, **kwargs):
            calls.append(kwargs["model"])
            return next(repair_results)

        initial_quality = quality(gap)
        with (
            patch.object(processing, "ARK_API_KEYS", ("key",)),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "mark_task_stage"),
            patch.object(processing, "ark_recognize", side_effect=recognize),
            patch.object(processing, "repair_content_gaps", side_effect=repair),
            patch.object(processing, "_evaluate_quality",
                         return_value=(initial_quality, False, [issue])),
        ):
            _script, final_quality, _usage, _ = asyncio.run(
                processing._run_recognizer_with_media(
                    row, Path("unused"), 30, self._funnel_evidence(), route, {}, None,
                )
            )
        self.assertEqual(calls, ["lite", "lite", "turbo", "lite"])
        self.assertEqual(calls.count("turbo"), 1)
        self.assertFalse(quality_gate(final_quality)[0])

    def test_profit_budget_rejects_turbo_but_allows_lite_at_same_spend(self):
        row = {"credits_used": 1}
        usage = {"api_cost_rmb": 0.2118}
        with (
            patch.object(processing, "POINTS_PER_MINUTE", 5),
            patch.object(processing, "POINTS_PER_YUAN", 12.8),
            patch.object(processing, "ARK_MIN_GROSS_MARGIN_RATIO", 0.2),
            patch.object(processing, "ARK_TURBO_COST_SAFETY_FACTOR", 1.25),
        ):
            turbo = processing._quality_call_budget(row, 60, 60, usage, "turbo")
            lite = processing._quality_call_budget(row, 60, 60, usage, "lite")
        self.assertFalse(turbo["allowed"])
        self.assertTrue(lite["allowed"])
        for allowed in (item for item in (turbo, lite) if item["allowed"]):
            self.assertLessEqual(allowed["projectedCostRmb"], allowed["costCeilingRmb"])

    def test_lite_unavailable_does_not_fall_back_to_unprofitable_turbo(self):
        unavailable = ArkError(
            "ModelNotOpen model not active", status_code=400, provider_code="ModelNotOpen"
        )
        route = {"provider": "ark", "model": "lite", "mode": "lite_first",
                 "band": "simple", "score": 0, "reasons": ["routing_mode=lite_first"]}
        with (
            patch.object(processing, "ARK_API_KEYS", ("key",)),
            patch.object(processing, "ARK_LITE_MODEL", "lite"),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "ark_recognize", side_effect=unavailable) as recognize,
            patch.object(processing, "_quality_call_budget", return_value={"allowed": False}),
            patch.object(processing, "mark_task_stage"),
        ):
            with self.assertRaises(ArkError) as caught:
                asyncio.run(processing._run_recognizer_with_media(
                    {"id": "task-budget", "title": "测试", "credits_used": 1},
                    Path("unused"), 30, self._funnel_evidence(), route, {}, None,
                ))
        self.assertEqual(recognize.call_count, 1)
        self.assertEqual(caught.exception.provider_code, "profit_budget_guard")
        self.assertTrue(processing._is_non_retryable_provider_error(caught.exception))
        self.assertFalse(processing._is_quality_gate_failure(caught.exception))

    def test_process_task_quality_block_enters_review_without_refund(self):
        candidate = self._funnel_script(resolved=False)
        quality = {
            "issues": [{"severity": "P0", "tag": "台词归属错", "description": "说话人未知"}],
            "blockingIssues": [{"severity": "P0", "tag": "台词归属错", "description": "说话人未知"}],
            "severityCounts": {"P0": 1},
        }
        usage = {
            "provider": "ark", "model": "lite", "input_tokens": 10,
            "output_tokens": 5, "total_tokens": 15, "api_cost_rmb": 0.08,
        }
        row = {
            "id": "task-quality", "title": "测试", "file_name": "test.mp4",
            "attempts": 0, "user_id": "user", "credits_used": 1,
        }
        with (
            patch.object(processing, "task_row", return_value=row),
            patch.object(processing, "run_recognizer", return_value=(candidate, quality, usage, {})),
            patch.object(processing, "mark_task_stage"),
            patch.object(processing, "record_task_event") as record_event,
            patch.object(processing, "has_pending_recheck", return_value=False),
            patch.object(processing, "discard_local_proxy"),
            patch.object(processing, "_refund_task_once") as refund,
            patch.object(processing, "update_task") as update,
        ):
            asyncio.run(processing.process_task("task-quality"))
        refund.assert_not_called()
        persisted = update.call_args.kwargs
        self.assertEqual((persisted["status"], persisted["stage"]), ("review", "review"))
        self.assertEqual(persisted["api_cost_rmb"], 0.08)
        self.assertEqual(persisted["total_tokens"], 15)
        self.assertEqual(json.loads(persisted["result_json"])["characters"], ["甲"])
        saved_quality = json.loads(persisted["quality_json"])
        self.assertEqual(saved_quality["deliveryStatus"], "review_required")
        self.assertEqual(saved_quality["blockingIssues"][0]["severity"], "P0")
        self.assertEqual(record_event.call_args.args[1], "review_required")
        self.assertEqual(record_event.call_args.kwargs["status"], "review")

    def test_incomplete_quality_repair_with_candidate_enters_review(self):
        candidate = self._funnel_script(resolved=False)
        quality = {
            "blockingIssues": [{"severity": "P0", "tag": "missing", "description": "gap remains"}],
        }
        failure = processing._quality_failure(
            "missing content remains", "content_repair_incomplete",
            {"provider": "ark", "model": "lite", "total_tokens": 12, "api_cost_rmb": 0.04},
            quality, candidate,
        )
        row = {
            "id": "task-incomplete", "title": "测试", "file_name": "test.mp4",
            "attempts": 0, "user_id": "user", "credits_used": 1,
        }
        with (
            patch.object(processing, "task_row", return_value=row),
            patch.object(processing, "run_recognizer", side_effect=failure),
            patch.object(processing, "mark_task_stage"),
            patch.object(processing, "record_task_event") as record_event,
            patch.object(processing, "has_pending_recheck", return_value=False),
            patch.object(processing, "_refund_task_once") as refund,
            patch.object(processing, "update_task") as update,
        ):
            asyncio.run(processing.process_task("task-incomplete"))
        refund.assert_not_called()
        persisted = update.call_args.kwargs
        self.assertEqual((persisted["status"], persisted["stage"]), ("review", "review"))
        self.assertIsNone(persisted["error"])
        self.assertEqual(json.loads(persisted["result_json"])["scenes"][0]["blocks"][0]["type"], "action")
        self.assertTrue(json.loads(persisted["quality_json"])["automatedResolution"]["manualReview"])
        self.assertEqual(record_event.call_args.args[1], "review_required")

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
                "blocks": [{"type": "action", "text": "甲在客厅等候。", "startSec": 0, "endSec": 30},
                           {"type": "dialogue", "speaker": "未知说话人", "text": "你好", "uncertain": True, "startSec": 1, "endSec": 2}],
            }],
        }
        repaired = {
            "characters": ["甲"],
            "scenes": [{
                "heading": "1-1 日 内 客厅",
                "location": "客厅",
                "blocks": [{"type": "action", "text": "甲在客厅等候。", "startSec": 0, "endSec": 30},
                           {"type": "dialogue", "speaker": "甲", "text": "你好", "startSec": 1, "endSec": 2}],
            }],
        }
        row = {"id": "task-1", "stored_path": str(Path(__file__).resolve()), "title": "测试", "duration_sec": 30}
        with (
            patch.object(processing, "recognition_cache", return_value=None),
            patch.object(processing, "ARK_API_KEYS", ("key",)),
            patch.object(processing, "ARK_LITE_MODEL", "lite"),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "ARK_MODEL", "turbo"),
            patch.object(processing, "ARK_ROUTING_MODE", "complexity"),
            patch.object(processing, "probe_duration", return_value=30),
            patch.object(processing, "collect_evidence", return_value=evidence),
            patch.object(processing, "ark_prepare_video", return_value={"api_key": "key", "file_id": "file"}),
            patch.object(processing, "mark_task_stage"),
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
        self.assertEqual(next(b for b in script["scenes"][0]["blocks"] if b["type"] == "dialogue")["speaker"], "甲")
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
                "blocks": [{"type": "action", "text": "甲在客厅等候。", "startSec": 0, "endSec": 210},
                           {"type": "dialogue", "speaker": "未知说话人", "text": "你好", "uncertain": True}],
            }],
        }
        # A complex clip starts on Turbo. It never receives another Turbo call
        # when a real P0 remains; the quality gate fails closed instead.
        row = {"id": "task-2", "stored_path": str(Path(__file__).resolve()), "title": "测试", "duration_sec": 210}
        with (
            patch.object(processing, "recognition_cache", return_value=None),
            patch.object(processing, "ARK_API_KEYS", ("key",)),
            patch.object(processing, "ARK_LITE_MODEL", "lite"),
            patch.object(processing, "ARK_TURBO_MODEL", "turbo"),
            patch.object(processing, "ARK_MODEL", "turbo"),
            patch.object(processing, "ARK_ROUTING_MODE", "complexity"),
            patch.object(processing, "probe_duration", return_value=210),
            patch.object(processing, "collect_evidence", return_value=evidence),
            patch.object(processing, "ark_prepare_video", return_value={"api_key": "key", "file_id": "file"}),
            patch.object(processing, "mark_task_stage"),
            patch.object(processing, "update_task"),
            patch.object(
                processing,
                "ark_recognize",
                return_value=(unresolved, {"total_tokens": 10, "api_cost_rmb": 0}),
            ) as recognize,
        ):
            _script, quality, _usage, _ = asyncio.run(processing.run_recognizer(row))
        self.assertEqual(recognize.call_count, 1)
        self.assertFalse(quality_gate(quality)[0])
        self.assertTrue(quality["automatedReview"]["unresolvedIssues"])


if __name__ == "__main__":
    unittest.main()
