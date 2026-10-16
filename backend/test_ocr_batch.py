"""OCR batching must preserve blank boundaries, short changes and timestamps."""

from contextlib import nullcontext
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app import evidence


class OcrBatchTests(unittest.TestCase):
    def run_pages(self, outputs, count=4, size=16):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Spaces in image paths are legal in Tesseract's file-list input.
            pages = [root / f"sample {i}.pgm" for i in range(count)]
            with (patch.object(evidence, "OCR_BATCH_SIZE", size),
                  patch.object(evidence, "capacity", return_value=nullcontext()),
                  patch.object(evidence.subprocess, "run", side_effect=outputs) as run):
                result, stats = evidence._run_ocr_pages(pages, "tesseract", root)
                lists = [Path(call.args[0][1]).read_text(encoding="utf-8").splitlines()
                         for call in run.call_args_list if str(call.args[0][1]).endswith(".txt")]
            return result, stats, run.call_args_list, lists

    def test_blank_first_middle_and_last_pages_keep_their_slots(self):
        output = subprocess.CompletedProcess([], 0, "\f江阅\n\f\f", "")
        results, stats, calls, lists = self.run_pages([output])
        self.assertEqual([r[1] for r in results], ["", "江阅", "", ""])
        self.assertEqual(stats["processes"], 1)
        self.assertEqual(stats["fallback_batches"], 0)
        self.assertEqual(len(lists[0]), 4)
        self.assertTrue(all("sample " in path for path in lists[0]))
        self.assertEqual(calls[0].kwargs["env"]["OMP_THREAD_LIMIT"], "1")

    def test_unsupported_or_partial_batch_falls_back_without_shifting_pages(self):
        for broken in (subprocess.CompletedProcess([], 1, "partial", "engine failed"),
                       subprocess.CompletedProcess([], 0, "first\fsecond", ""),
                       subprocess.TimeoutExpired("tesseract", 120)):
            with self.subTest(broken=type(broken).__name__):
                singles = [subprocess.CompletedProcess([], code, text, "error" if code else "")
                           for code, text in ((0, "甲"), (0, ""), (1, "ignored"), (0, "乙"))]
                results, stats, calls, _ = self.run_pages([broken, *singles])
                self.assertEqual([(r[0], r[1]) for r in results], [(0, "甲"), (0, ""), (1, ""), (0, "乙")])
                self.assertEqual(stats["processes"], 5)
                self.assertEqual(stats["fallback_batches"], 1)
                self.assertTrue(all(call.kwargs["timeout"] == 30 for call in calls[1:]))

    def test_size_one_preserves_the_original_single_page_command(self):
        output = subprocess.CompletedProcess([], 0, "原话\n", "")
        results, stats, calls, lists = self.run_pages([output] * 4, size=1)
        self.assertEqual([r[1] for r in results], ["原话"] * 4)
        self.assertEqual(stats, {"processes": 4, "batches": 0, "fallback_batches": 0})
        self.assertFalse(lists)
        for call in calls:
            self.assertEqual(call.args[0][2:], ["stdout", "-l", evidence.OCR_LANG, "--psm", "11"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("tesseract"), "real OCR tools required")
class RealOcrBatchTests(unittest.TestCase):
    def test_single_and_batch_evidence_are_identical_for_short_text_changes(self):
        with tempfile.TemporaryDirectory(prefix="jbb-ocr-test-") as directory:
            root = Path(directory)
            frames = []
            for index, text in enumerate(("", "101", "102", "A", "B")):
                image = root / f"sample {index}.pgm"
                command = [shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                           "-f", "lavfi", "-i", "color=c=white:s=640x240:d=0.04"]
                if text:
                    command += ["-vf", f"drawtext=text={text}:fontcolor=black:fontsize=36:x=30:y=160"]
                subprocess.run(command + ["-frames:v", "1", "-pix_fmt", "gray", "-y", str(image)],
                               capture_output=True, check=True, timeout=30)
                frames.append(image)
            # A blank separates two occurrences of the same subtitle. The
            # one-digit/one-letter changes must retain their own timestamps.
            ordered = [frames[i] for i in (0, 1, 1, 0, 1, 2, 3, 4, 0)]
            times = [i * .25 for i in range(len(ordered))]
            variants = []
            with (patch.object(evidence, "_adaptive_ocr_times", return_value=times),
                  patch.object(evidence, "_extract_ocr_frames", return_value=ordered)):
                for size in (1, 16):
                    with patch.object(evidence, "OCR_BATCH_SIZE", size):
                        variants.append(evidence._ocr_evidence(frames[0], 2.25))
            self.assertEqual(variants[0], variants[1])
            self.assertEqual(variants[1]["status"], "available")
            observed = {(item["text"], item["startSec"], item["endSec"])
                        for item in variants[1]["items"] if item["region"] == "subtitle_band"}
            self.assertEqual(observed, {("101", .25, .75), ("101", 1, 1.25),
                                       ("102", 1.25, 1.5), ("A", 1.5, 1.75), ("B", 1.75, 2)})


if __name__ == "__main__":
    unittest.main()
