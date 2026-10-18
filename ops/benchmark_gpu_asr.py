"""Compare existing CPU ASR and private GPU ASR without invoking paid models."""
from __future__ import annotations
import argparse
import difflib
import json
from pathlib import Path
import re
import time
from app import evidence, remote_asr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record = {"durationSec": args.duration, "settings": remote_asr.settings(), "paidCalls": 0}
    started = time.perf_counter()
    cpu = evidence._local_asr_items(args.video)
    record["cpuSeconds"] = round(time.perf_counter() - started, 3)
    started = time.perf_counter()
    pcm = evidence._decode_audio_pcm(args.video)
    gpu = remote_asr.request_asr(pcm, args.duration)
    record["gpuSeconds"] = round(time.perf_counter() - started, 3)
    def summary(items):
        text = "".join(item["text"] for item in items)
        return {"segments": len(items), "chars": len(text),
                "words": sum(len(item.get("words", [])) for item in items),
                "firstSec": items[0]["startSec"] if items else None,
                "lastSec": items[-1]["endSec"] if items else None}
    record["cpu"], record["gpu"] = summary(cpu), summary(gpu["items"])
    normalize = lambda items: re.sub(r"[\W_]+", "", "".join(x["text"] for x in items))
    record["textSimilarity"] = round(difflib.SequenceMatcher(None, normalize(cpu), normalize(gpu["items"]), autojunk=False).ratio(), 4)
    record["speedup"] = round(record["cpuSeconds"] / record["gpuSeconds"], 2)
    args.output.write_text(json.dumps({**record, "cpuItems": cpu, "gpuSource": gpu}, ensure_ascii=False, indent=2))
    print("ASR_BENCHMARK", json.dumps(record, ensure_ascii=False), flush=True)
    assert cpu and gpu["items"], "The speech sample must produce nonempty transcripts"


if __name__ == "__main__":
    main()
