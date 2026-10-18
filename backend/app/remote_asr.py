"""Private, bounded lossless PCM transport; the caller keeps CPU fallback."""
from __future__ import annotations
import json
import gzip
import math
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler
from . import config

PROTOCOL = "jbb-asr-1"
_lock = threading.Lock()
_retry_after = 0.0


def settings():
    return {name: getattr(config, name) for name in (
        "LOCAL_ASR_MODEL", "LOCAL_ASR_LANGUAGE", "LOCAL_ASR_BEAM_SIZE",
        "LOCAL_ASR_VAD_MIN_SPEECH_MS", "LOCAL_ASR_VAD_MIN_SILENCE_MS", "LOCAL_ASR_VAD_SPEECH_PAD_MS")}


def validate_result(value, duration: float, samples: int):
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise ValueError("invalid ASR protocol")
    source = value.get("source")
    if (not isinstance(source, dict) or source.get("kind") != "audio"
            or source.get("status") != "available" or source.get("provider") != "faster-whisper-gpu"
            or source.get("settings") != settings()
            or source.get("sampling") != {"samples": samples, "sampleRate": 16000, "complete": True}):
        raise ValueError("incomplete or incompatible GPU ASR")
    items = source.get("items")
    if not isinstance(items, list) or len(items) > 4000:
        raise ValueError("invalid ASR items")
    def check(part):
        left, right = part.get("startSec"), part.get("endSec")
        if (not isinstance(part.get("text"), str) or not part["text"].strip() or len(part["text"]) > 1500
                or type(left) not in {int, float} or type(right) not in {int, float}
                or not math.isfinite(left) or not math.isfinite(right)
                or not 0 <= left <= right <= max(duration, samples / 16000) + .1):
            raise ValueError("invalid ASR text or anchors")
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "transcript_segment":
            raise ValueError("invalid ASR segment")
        check(item)
        words = item.get("words", [])
        if not isinstance(words, list) or len(words) > 2000:
            raise ValueError("invalid ASR words")
        for word in words:
            if not isinstance(word, dict):
                raise ValueError("invalid ASR word")
            check(word)
    if sum(len(item["text"]) for item in items) > 12000:
        raise ValueError("oversized ASR transcript")
    return source


def request_asr(pcm: bytes, duration: float):
    global _retry_after
    with _lock:
        if time.monotonic() < _retry_after:
            raise RuntimeError("GPU ASR cooling down")
    if not config.ASR_REMOTE_TOKEN or not pcm or len(pcm) % 2 or len(pcm) > 3600 * 32000:
        raise ValueError("invalid ASR request")
    try:
        # Speech PCM compresses well; transport remains bit-for-bit lossless.
        payload = gzip.compress(pcm, compresslevel=1)
        request = Request(config.ASR_REMOTE_URL + "/asr", data=payload, method="POST", headers={
            "Authorization": "Bearer " + config.ASR_REMOTE_TOKEN,
            "Content-Type": "application/octet-stream", "Content-Length": str(len(payload)),
            "Content-Encoding": "gzip", "X-ASR-Samples": str(len(pcm) // 2),
            "X-ASR-Duration": str(duration), "X-ASR-Settings": json.dumps(settings(), separators=(",", ":")),
        })
        with build_opener(ProxyHandler({})).open(request, timeout=config.ASR_REMOTE_TIMEOUT_SECONDS) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError("oversized ASR response")
        return validate_result(json.loads(raw), duration, len(pcm) // 2)
    except Exception:
        with _lock:
            _retry_after = time.monotonic() + 30
        raise
