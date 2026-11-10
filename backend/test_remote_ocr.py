"""GPU transport must never accept partial frames or silently fake success."""
import copy
from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import evidence, remote_ocr
from app import ocr_pages


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

    def test_lossless_page_transport_preserves_the_exact_sampling_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            video.write_bytes(b"x" * 100000)
            frames = []
            for index, pixel in enumerate((1, 1, 0, 1, 2)):
                frame = root / f"frame-{index}.pgm"
                frame.write_bytes(b"P5\n8 8\n255\n" + bytes([pixel]) * 64)
                frames.append(frame)
            def texts(paths):
                return [{0: "", 1: "101", 2: "102"}[p.read_bytes()[-1]] for p in paths]
            def original(paths, *_):
                return [(0, t, "") for t in texts(paths)], {"processes": 0, "batches": 1, "fallback_batches": 0}
            def post(_url, _token, endpoint, stream, size, headers, timeout):
                self.assertEqual(endpoint, "/ocr/pages")
                self.assertEqual(size, Path(stream.name).stat().st_size)
                self.assertGreaterEqual(timeout, 240)
                output = root / "unpacked"
                output.mkdir()
                paths = ocr_pages.unpack(stream, output, int(headers["X-OCR-Pages"]), 1440)
                return {"protocol": ocr_pages.PROTOCOL, "sha256": headers["X-OCR-SHA256"], "pages": texts(paths)}
            with (patch.object(evidence.shutil, "which", return_value="fixture-tool"),
                  patch.object(evidence, "_adaptive_ocr_times", return_value=[i * .25 for i in range(5)]),
                  patch.object(evidence, "_extract_ocr_frames", return_value=frames),
                  patch.object(remote_ocr, "OCR_REMOTE_TOKEN", "fixture-token"),
                  patch.object(remote_ocr, "OCR_REMOTE_PAGE_TRANSPORT", True),
                  patch.object(remote_ocr, "remote_attempt", return_value=nullcontext()),
                  patch.object(remote_ocr, "post", side_effect=post)):
                expected = evidence._ocr_evidence_local(video, 1, page_runner=original, provider="paddleocr-gpu")
                actual = remote_ocr.request_ocr(video, 1, 5)
            self.assertEqual(actual, expected)
            self.assertEqual([(i["text"], i["startSec"], i["endSec"]) for i in actual["items"]
                              if i["region"] == "subtitle_band"], [("101", 0, .5), ("101", .75, 1), ("102", 1, 1)])

    def test_larger_page_archive_uses_original_gpu_video_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "tiny.mp4"
            video.write_bytes(b"x")
            frame = Path(directory) / "frame.pgm"
            frame.write_bytes(b"P5\n8 8\n255\n" + b"x" * 64)
            value = self.result()
            value["source"]["sampling"]["frames"] = 1
            with (patch.object(evidence.shutil, "which", return_value="fixture-tool"),
                  patch.object(evidence, "_adaptive_ocr_times", return_value=[0]),
                  patch.object(evidence, "_extract_ocr_frames", return_value=[frame]),
                  patch.object(remote_ocr, "OCR_REMOTE_TOKEN", "fixture-token"),
                  patch.object(remote_ocr, "OCR_REMOTE_PAGE_TRANSPORT", True),
                  patch.object(remote_ocr, "remote_attempt", return_value=nullcontext()),
                  patch.object(remote_ocr, "post", return_value=value) as post):
                remote_ocr.request_ocr(video, 1, 1)
            self.assertEqual(post.call_args.args[2], "/ocr")
