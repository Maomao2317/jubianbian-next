"""Compare dense OCR extraction against the previous independent seeks."""

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import evidence


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for pixel comparisons")
class EvidenceFrameTests(unittest.TestCase):
    def compare_samples(self, *, offset=False, fail_batch=False):
        ffmpeg = shutil.which("ffmpeg")
        with tempfile.TemporaryDirectory(prefix="jbb-frame-test-") as root:
            directory = Path(root)
            video = directory / "sample.mp4"
            command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=25:duration=4.2"]
            if offset:
                command += ["-vf", "select=not(mod(n\\,3)),setpts=PTS+1.7/TB", "-fps_mode", "vfr"]
            subprocess.run(command + ["-c:v", "mpeg4", "-y", str(video)], capture_output=True, check=True, timeout=30)
            duration = 4.137
            times = [round(index * duration / 7, 3) for index in range(7)]
            target = directory / "batch"
            target.mkdir()
            original_run = subprocess.run

            def run_with_batch_error(command, **kwargs):
                if "-vf" in command:
                    raise subprocess.CalledProcessError(1, command)
                return original_run(command, **kwargs)

            with patch.object(evidence.subprocess, "run", side_effect=run_with_batch_error if fail_batch else original_run):
                frames = evidence._extract_ocr_frames(video, target, times, ffmpeg, duration)
            self.assertEqual(len(frames), len(times))
            for index, (timestamp, frame) in enumerate(zip(times, frames)):
                previous = directory / f"seek-{index}.png"
                subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(timestamp), "-i", str(video), "-frames:v", "1", "-y", str(previous)], capture_output=True, check=True, timeout=30)
                self.assertEqual(hashlib.sha256(frame.read_bytes()).digest(), hashlib.sha256(previous.read_bytes()).digest(), f"pixels changed at {timestamp}s")

    def test_dense_frames_preserve_pixels(self):
        self.compare_samples()

    def test_variable_frame_rate_and_nonzero_start_preserve_pixels(self):
        self.compare_samples(offset=True)

    def test_failed_batch_decode_preserves_seek_fallback(self):
        self.compare_samples(fail_batch=True)


if __name__ == "__main__":
    unittest.main()
