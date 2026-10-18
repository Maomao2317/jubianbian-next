"""Protect page order and fail-closed behavior without requiring a local GPU."""
import ast
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class CrossFrameOcrTests(unittest.TestCase):
    def run_pages(self, *, missing_page=False, missing_crop=False):
        tree = ast.parse(Path(__file__).with_name("gpu_ocr_server.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run_pages")
        # Mixed full/subtitle shapes, a blank page between equal subtitles, and
        # two differently shaped words whose recognition batch reverses order.
        images = {
            "a": SimpleNamespace(shape=(100, 200, 3), words=[("first", (20, 80)), ("second", (20, 20))]),
            "blank": SimpleNamespace(shape=(100, 200, 3), words=[]),
            "full": SimpleNamespace(shape=(200, 200, 3), words=[("name", (20, 40))]),
            "b": SimpleNamespace(shape=(100, 200, 3), words=[("first", (20, 80))]),
        }
        calls = []

        def detect(input, **kwargs):
            result = [{"dt_polys": image.words} for image in input]
            return result[:-1] if missing_page else result

        def crop(image, boxes):
            return [SimpleNamespace(text=text, shape=shape) for text, shape in boxes]

        def recognize(input, **kwargs):
            calls.append([word.text for word in input])
            result = [{"rec_text": word.text} for word in input]
            return result[:-1] if missing_crop else result

        namespace = dict(GPU_LOCK=threading.Lock(), DET_BATCH=64, REC_BATCH=64, time=time,
                         MODEL=(SimpleNamespace(predict=detect), SimpleNamespace(predict=recognize),
                                lambda boxes: boxes, crop), _bounded_text=lambda text, limit: text[:limit])
        exec(compile(ast.Module(body=[function], type_ignores=[]), "gpu_ocr_server.py", "exec"), namespace)
        cv2 = SimpleNamespace(IMREAD_COLOR=1, imread=lambda path, mode: images[path])
        with patch.dict("sys.modules", {"cv2": cv2}):
            result, _ = namespace["run_pages"](list(images), None, None)
        return [text for _, text, _ in result], calls

    def test_mixed_pages_and_cross_frame_text_keep_reading_order_and_blanks(self):
        texts, calls = self.run_pages()
        self.assertEqual(texts, ["first second", "", "name", "first"])
        self.assertEqual(calls, [["second", "name", "first", "first"]])

    def test_missing_detected_page_fails_instead_of_shifting_subtitles(self):
        with self.assertRaisesRegex(ValueError, "lost input pages"):
            self.run_pages(missing_page=True)

    def test_missing_recognized_crop_fails_instead_of_returning_partial_text(self):
        with self.assertRaisesRegex(ValueError, "lost recognition crops"):
            self.run_pages(missing_crop=True)


if __name__ == "__main__":
    unittest.main()
