"""Bounded GPU OCR transport with CPU fallback at the caller."""
from __future__ import annotations
import json
import math
import threading
import time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from .config import (OCR_REMOTE_URL, OCR_REMOTE_TOKEN, OCR_REMOTE_TIMEOUT_SECONDS,
                     OCR_SAMPLE_INTERVAL_SECONDS, OCR_MAX_FRAMES)

PROTOCOL = "jbb-ocr-1"
_lock = threading.Lock()
_retry_after = 0.0


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
    global _retry_after
    with _lock:
        if time.monotonic() < _retry_after:
            raise RuntimeError("GPU OCR cooling down")
    if not OCR_REMOTE_TOKEN:
        raise ValueError("GPU OCR token missing")
    try:
        with path.open("rb") as video:
            request = Request(OCR_REMOTE_URL + "/ocr", data=video, method="POST", headers={
                "Authorization": "Bearer " + OCR_REMOTE_TOKEN,
                "Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size),
                "X-OCR-Duration": str(duration), "X-OCR-Interval": str(OCR_SAMPLE_INTERVAL_SECONDS),
                "X-OCR-Max-Frames": str(OCR_MAX_FRAMES),
            })
            with build_opener(ProxyHandler({})).open(request, timeout=OCR_REMOTE_TIMEOUT_SECONDS) as response:
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError("oversized OCR response")
            return validate_result(json.loads(raw), duration, expected_frames)
    except Exception:
        with _lock:
            _retry_after = time.monotonic() + 30
        raise
