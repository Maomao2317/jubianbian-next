"""GPU ASR keeps complete audio, original decoding settings and CPU recovery."""
import copy
from contextlib import nullcontext
import gzip
import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from app import evidence, remote_asr


class RemoteAsrTests(unittest.TestCase):
    def result(self):
        return {"protocol": remote_asr.PROTOCOL, "source": {
            "kind": "audio", "status": "available", "provider": "faster-whisper-gpu",
            "settings": remote_asr.settings(),
            "sampling": {"samples": 160000, "sampleRate": 16000, "complete": True},
            "items": [{"type": "transcript_segment", "text": "我", "startSec": 1, "endSec": 1.2,
                       "words": [{"text": "我", "startSec": 1, "endSec": 1.2}]}]}}

    def test_short_lines_and_word_anchors_are_preserved(self):
        source = remote_asr.validate_result(self.result(), 10, 160000)
        self.assertEqual(source["items"][0]["words"][0]["text"], "我")

    def test_partial_audio_and_changed_decoding_are_rejected(self):
        for group, key, bad in (("sampling", "samples", 100), ("sampling", "complete", False),
                                ("settings", "LOCAL_ASR_BEAM_SIZE", 1)):
            value = self.result()
            value["source"][group][key] = bad
            with self.assertRaises(ValueError):
                remote_asr.validate_result(value, 10, 160000)

    def test_invalid_word_times_or_cpu_provider_are_rejected(self):
        for end in (float("nan"), 12, -1):
            value = self.result()
            value["source"]["items"][0]["words"][0]["endSec"] = end
            with self.assertRaises(ValueError):
                remote_asr.validate_result(value, 10, 160000)
        value = self.result()
        value["source"]["provider"] = "cpu"
        with self.assertRaises(ValueError):
            remote_asr.validate_result(value, 10, 160000)

    def test_remote_failure_and_empty_transcript_use_cpu(self):
        for response in (TimeoutError(), {"items": []}):
            with (patch.object(evidence, "ASR_REMOTE_URL", "http://private"),
                  patch.object(evidence, "LOCAL_ASR_MODEL", "small"),
                  patch.object(Path, "exists", return_value=True),
                  patch.object(evidence, "_decode_audio_pcm", return_value=b"\0\0"),
                  patch.object(remote_asr, "request_asr", **(
                      {"side_effect": response} if isinstance(response, Exception) else {"return_value": response})),
                  patch.object(evidence, "_local_asr_items", return_value=[{"text": "保留原识别"}]) as cpu):
                self.assertEqual(evidence._audio_evidence(Path("video"), 10)["provider"], "faster-whisper:small")
            cpu.assert_called_once()

    def test_injected_provider_keeps_priority(self):
        with patch.object(remote_asr, "request_asr") as gpu:
            source = evidence._audio_evidence(Path("video"), 10, lambda _: "原功能")
        self.assertEqual(source["provider"], "injected")
        gpu.assert_not_called()

    def test_audio_transport_is_lossless_and_samples_match(self):
        pcm = b"\1\0\2\0" * 80000
        response = io.BytesIO(json.dumps(self.result()).encode())
        opener = Mock()
        opener.open.return_value = response
        with (patch.object(remote_asr, "remote_attempt", return_value=nullcontext()),
              patch.object(remote_asr.config, "ASR_REMOTE_TOKEN", "private-test-token"),
              patch.object(remote_asr.config, "ASR_REMOTE_URL", "http://private"),
              patch.object(remote_asr, "build_opener", return_value=opener)):
            remote_asr.request_asr(pcm, 10)
        request = opener.open.call_args.args[0]
        self.assertEqual(gzip.decompress(request.data), pcm)
        self.assertEqual(request.get_header("X-asr-samples"), "160000")
