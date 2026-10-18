"""Compare warmed GPU OCR with the CPU sampler, without calling paid models."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
from app.evidence import _adaptive_ocr_times, _ocr_evidence_local
from app.remote_ocr import request_ocr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("duration", type=float)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"durationSec": args.duration, "bytes": args.video.stat().st_size,
              "additionalModelCalls": 0}
    sources = {}
    for name, operation in (
        ("gpu", lambda: request_ocr(args.video, args.duration, len(_adaptive_ocr_times(args.duration)))),
        ("cpu", lambda: _ocr_evidence_local(args.video, args.duration)),
    ):
        start = time.perf_counter()
        source = operation()
        seconds = time.perf_counter() - start
        if source.get("status") != "available" or not source.get("sampling", {}).get("complete"):
            raise RuntimeError(f"{name} OCR incomplete")
        sources[name] = source
        report[name] = {"seconds": round(seconds, 3), "provider": source.get("provider"),
                        "items": len(source.get("items", [])), "sampling": source["sampling"]}
        print(name, json.dumps(report[name]), flush=True)
    report["speedup"] = round(report["cpu"]["seconds"] / report["gpu"]["seconds"], 3)
    args.output.write_text(json.dumps({"report": report, "sources": sources}, ensure_ascii=False, indent=2))
    print("OCR BENCHMARK", json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
