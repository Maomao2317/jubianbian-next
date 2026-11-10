"""Lossless archives must keep every page and reject damaged/reordered input."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from app import ocr_pages as pages


class OcrPagesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = []
        for index, pixel in enumerate((255, 0, 255, 1)):
            path = self.root / f"source {index}.pgm"
            path.write_bytes(b"P5\n8 8\n255\n" + bytes([pixel]) * 64)
            self.paths.append(path)
        self.archive = self.root / "pages.zip"
        pages.pack(self.paths, self.archive, 10)
        self.output = self.root / "output"
        self.output.mkdir()

    def rewrite(self, change):
        with zipfile.ZipFile(self.archive) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        change(entries)
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)

    def test_pixels_blanks_repeated_pages_and_order_are_identical(self):
        output = pages.unpack(self.archive, self.output, 4, 10)
        self.assertEqual([p.read_bytes() for p in output], [p.read_bytes() for p in self.paths])

    def test_corrupt_pixels_missing_page_extra_path_or_order_fail_closed(self):
        def reverse(entries):
            manifest = json.loads(entries["manifest.json"])
            manifest["pages"].reverse()
            entries["manifest.json"] = json.dumps(manifest).encode()
        for change in (lambda e: e.update({"0001.pgm": b"P5\n8 8\n255\n" + b"x" * 64}),
                       lambda e: e.pop("0001.pgm"), lambda e: e.update({"../escape": b"x"}), reverse):
            with self.subTest(change=change):
                pages.pack(self.paths, self.archive, 10)
                self.rewrite(change)
                with self.assertRaises(ValueError):
                    pages.unpack(self.archive, self.output, 4, 10)
        self.assertFalse((self.root / "escape").exists())

    def test_limits_are_checked_before_loading_uncompressed_pages(self):
        with patch.object(pages, "MAX_PAGE_BYTES", 10):
            with self.assertRaises(ValueError):
                pages.unpack(self.archive, self.output, 4, 10)
            with self.assertRaises(pages.TooLarge):
                pages.pack(self.paths, self.root / "large.zip", 10)
        with self.assertRaises(ValueError):
            pages.unpack(self.archive, self.output, 5, 10)


if __name__ == "__main__":
    unittest.main()
