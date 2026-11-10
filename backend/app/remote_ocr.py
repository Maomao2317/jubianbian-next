"""Bounded GPU OCR transport with CPU fallback at the caller."""
from __future__ import annotations
import math
from pathlib import Path
import logging
from .config import (OCR_REMOTE_URL, OCR_REMOTE_TOKEN, OCR_REMOTE_TIMEOUT_SECONDS,
                     OCR_SAMPLE_INTERVAL_SECONDS, OCR_MAX_FRAMES, OCR_REMOTE_PAGE_TRANSPORT)
from .remote_circuit import remote_attempt
from .gpu_transport import post
from . import ocr_pages

logger = logging.getLogger("jubianbian")

PROTOCOL = "jbb-ocr-1"


def validate_result(value, duration: float, expected_frames: int) -> dict:
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise ValueError("invalid OCR protocol")
    source = value.get("source")
    if not isinstance(source, dict) or source.get("status") != "available" or source.get("provider") != "paddleocr-gpu":
        raise ValueError("GPU OCR did not complete")
    sampling = source.get("sampling") or {}
    if (sampling.get("frames") != expected_frames or sampling.get("interval") != OCR_SAMPLE_INTERVAL_SECONDS
            or sampling.get("maxFrames") != OCR_MAX_FRAMES or not sampling.get("complete")):
        raise ValueError("incomplete GPU frame coverage")
    items = source.get("items")
    if not isinstance(items, list) or len(items) > 4000:
        raise ValueError("invalid OCR items")
    for item in items:
        if (not isinstance(item, dict) or item.get("type") != "screen_text"
                or not isinstance(item.get("text"), str) or not item["text"].strip()
                or len(item["text"]) > 2000 or item.get("region") not in {"full", "subtitle_band"}):
            raise ValueError("invalid OCR text")
        left, right = item.get("startSec"), item.get("endSec")
        if (type(left) not in {int, float} or type(right) not in {int, float}
                or not math.isfinite(left) or not math.isfinite(right) or not 0 <= left <= right <= duration + .01):
            raise ValueError("invalid OCR timestamps")
    return source


def request_ocr(path: Path, duration: float, expected_frames: int) -> dict:
    if not OCR_REMOTE_TOKEN:
        raise ValueError("GPU OCR token missing")
    timeout = max(OCR_REMOTE_TIMEOUT_SECONDS, min(1200, duration + 60))
    if OCR_REMOTE_PAGE_TRANSPORT:
        from .evidence import _ocr_evidence_local

        def runner(paths, _tesseract, directory):
            archive = directory / "gpu-pages.zip"
            try:
                ocr_pages.pack(paths, archive, OCR_MAX_FRAMES * 2)
            except ocr_pages.TooLarge:
                raise _UseVideo()
            size = archive.stat().st_size
            logger.info("gpu_ocr_payload source_bytes=%s page_bytes=%s pages=%s", path.stat().st_size, size, len(paths))
            # Some already compressed videos are smaller than lossless pages.
            # Select the smaller transport, never lower frame/pixel quality.
            if size >= min(path.stat().st_size, ocr_pages.MAX_BYTES):
                raise _UseVideo()
            digest = ocr_pages.file_digest(archive)
            with remote_attempt("ocr", OCR_REMOTE_URL, OCR_REMOTE_TOKEN, timeout), archive.open("rb") as stream:
                value = post(OCR_REMOTE_URL, OCR_REMOTE_TOKEN, "/ocr/pages", stream, size,
                             {"X-OCR-Pages": str(len(paths)), "X-OCR-SHA256": digest}, timeout)
                texts = value.get("pages")
                if (value.get("protocol") != ocr_pages.PROTOCOL or value.get("sha256") != digest
                        or not isinstance(texts, list) or len(texts) != len(paths)
                        or any(not isinstance(t, str) or len(t) > 1000 for t in texts)):
                    raise ValueError("Invalid GPU page response")
                return [(0, text, "") for text in texts], {"processes": 0, "batches": 1, "fallback_batches": 0}

        try:
            result = _ocr_evidence_local(path, duration, page_runner=runner, provider="paddleocr-gpu", raise_errors=True)
            return validate_result({"protocol": PROTOCOL, "source": result}, duration, expected_frames)
        except _UseVideo:
            pass
    with remote_attempt("ocr", OCR_REMOTE_URL, OCR_REMOTE_TOKEN, timeout), path.open("rb") as video:
        value = post(OCR_REMOTE_URL, OCR_REMOTE_TOKEN, "/ocr", video, path.stat().st_size, {
            "X-OCR-Duration": str(duration), "X-OCR-Interval": str(OCR_SAMPLE_INTERVAL_SECONDS),
            "X-OCR-Max-Frames": str(OCR_MAX_FRAMES),
        }, timeout)
        return validate_result(value, duration, expected_frames)


class _UseVideo(Exception):
    pass
