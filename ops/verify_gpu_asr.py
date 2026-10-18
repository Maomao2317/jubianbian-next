"""Free deployed smoke check: concurrent OCR/ASR and real CPU failover."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from unittest.mock import patch
from app import config, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # This verifier deliberately cannot fall through to a paid transcription.
    assert not config.OPENAI_API_KEY and not config.ARK_API_KEYS
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        audio = pool.submit(evidence._audio_evidence, args.video, args.duration)
        ocr = pool.submit(evidence._ocr_evidence, args.video, args.duration)
        audio, ocr = audio.result(), ocr.result()
    assert audio["provider"] == "faster-whisper-gpu" and audio["items"]
    assert ocr["provider"] == "paddleocr-gpu" and ocr["sampling"]["complete"]
    record = {"concurrentSeconds": round(time.perf_counter()-started, 3),
              "asrProvider": audio["provider"], "asrItems": len(audio["items"]),
              "ocrProvider": ocr["provider"], "ocrItems": len(ocr["items"]), "ocrSampling": ocr["sampling"]}
    started = time.perf_counter()
    with patch.object(config, "ASR_REMOTE_TOKEN", "intentional-invalid-smoke-token"):
        fallback = evidence._audio_evidence(args.video, args.duration)
    assert fallback["provider"] == "faster-whisper:small" and fallback["items"]
    record.update(fallbackProvider=fallback["provider"], fallbackItems=len(fallback["items"]),
                  fallbackSeconds=round(time.perf_counter()-started, 3), additionalPaidCalls=0)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print("DEPLOYED_ASR_VERIFIED", json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
