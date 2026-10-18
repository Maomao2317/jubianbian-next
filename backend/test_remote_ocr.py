"""GPU transport must never accept partial frames or silently fake success."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import evidence, remote_ocr


class RemoteOcrTests(unittest.TestCase):
    def result(self):
        return {"protocol": remote_ocr.PROTOCOL, "source": {
            "kind": "ocr", "status": "available", "provider": "paddleocr-gpu", "items": [],
            "sampling": {"frames": 41, "interval": .25, "maxFrames": 720, "complete": True}}}

    def test_real_blank_frames_are_allowed_but_partial_processing_is_rejected(self):
        value = self.result()
        self.assertEqual(remote_ocr.validate_result(value, 10, 41)["items"], [])
        for key, bad in (("frames", 40), ("interval", 1), ("complete", False)):
            broken = copy.deepcopy(value)
            broken["source"]["sampling"][key] = bad
            with self.assertRaises(ValueError):
                remote_ocr.validate_result(broken, 10, 41)

    def test_impossible_times_and_cpu_response_cannot_pass_as_gpu(self):
        value = self.result()
        for right in (11, float("nan"), -1):
            value["source"]["items"] = [{"type": "screen_text", "text": "名字", "region": "full",
                                         "startSec": 0, "endSec": right}]
            with self.assertRaises(ValueError):
                remote_ocr.validate_result(value, 10, 41)
        value = self.result()
        value["source"]["provider"] = "tesseract"
        with self.assertRaises(ValueError):
            remote_ocr.validate_result(value, 10, 41)

    def test_unavailable_gpu_keeps_existing_cpu_fallback(self):
        with (patch.object(evidence, "OCR_REMOTE_URL", "http://private"),
              patch.object(Path, "exists", return_value=True),
              patch.object(remote_ocr, "request_ocr", side_effect=TimeoutError()),
              patch.object(evidence, "_ocr_evidence_local", return_value={"provider": "tesseract"}) as cpu):
            self.assertEqual(evidence._ocr_evidence(Path("video"), 10)["provider"], "tesseract")
        cpu.assert_called_once()

    def test_available_cpu_fallback_is_reused_when_gpu_is_configured_but_offline(self):
        cached = {"sources": [{"kind": kind, "status": "available", "provider": "tesseract", "items": []}
                              for kind in ("audio", "ocr", "keyframes")]}
        with (patch.object(evidence, "OCR_REMOTE_URL", "http://private"),
              patch.object(evidence, "_audio_evidence") as audio,
              patch.object(evidence, "_keyframe_evidence") as keyframes,
              patch.object(evidence, "_ocr_evidence", return_value={"kind": "ocr", "status": "available", "provider": "paddleocr-gpu", "items": []}) as gpu):
            result = evidence.collect_evidence(Path("video"), "test", 10, cached_evidence=cached)
        audio.assert_not_called()
        keyframes.assert_not_called()
        gpu.assert_not_called()
        self.assertEqual(result["cacheHitSources"], ["audio", "keyframes", "ocr"])
