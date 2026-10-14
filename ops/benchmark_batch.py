"""Run real media benchmarks in a NEW isolated data directory.

Example (inside the application image):
  python /app/ops/benchmark_batch.py --mode asr --manifest /bench/media.json \
      --output /bench/asr1 --concurrency 16
  python /app/ops/benchmark_batch.py --mode batch --allow-paid-models \
      --manifest /bench/media.json --output /bench/batch16 --concurrency 16

Manifest: [{"source": "/samples/1.mp4", "title": "第1集"}, ...].
Object storage URIs are also supported. Never put signed URLs or credentials in
the manifest. Inputs are read, never moved/deleted. No live database is opened.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("asr", "evidence", "batch"), required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 4, 8, 12, 16), default=16)
    parser.add_argument("--allow-paid-models", action="store_true")
    parser.add_argument("--evidence-from", type=Path, help="Reuse a prior evidence run to isolate model throughput")
    args = parser.parse_args()
    media = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not media or len(media) > 32 or any(not isinstance(item.get("source"), str) for item in media):
        parser.error("Manifest must contain 1..32 media entries")
    if any("://" in item["source"] and not item["source"].startswith("s3://") for item in media):
        parser.error("Only local paths and canonical s3:// URIs are accepted")
    if args.mode == "batch" and not args.allow_paid_models:
        parser.error("Batch mode calls paid models; pass --allow-paid-models")
    output = args.output.resolve()
    if output.exists():
        parser.error("Output directory must not already exist (protects live data and previous results)")
    output.mkdir(parents=True)
    os.environ["JBB_DATA_DIR"] = str(output / "data")
    os.environ["JBB_WORKER_CONCURRENCY"] = str(args.concurrency)
    os.environ["JBB_ARK_MAX_IN_FLIGHT"] = str(args.concurrency)
    if args.mode != "batch":
        # Evidence-only experiments must not silently call a paid fallback.
        os.environ["OPENAI_API_KEY"] = ""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app import auth_store, config, evidence, processing, worker
    from app.media import probe_duration
    from app.recognition_cache import recognition_cache
    from app.storage import materialize_media, cleanup_materialized

    records = []
    stop = threading.Event()
    minimum_available_mb = None

    def sample_memory():
        nonlocal minimum_available_mb
        while not stop.is_set():
            try:
                values = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
                available = int(values["MemAvailable"].split()[0]) / 1024
                minimum_available_mb = min(minimum_available_mb, available) if minimum_available_mb is not None else available
            except (OSError, KeyError, ValueError):
                pass
            stop.wait(.5)

    def collect(index):
        item = media[index]
        path, temporary = materialize_media(item["source"], f"bench-{index}")
        started = time.perf_counter()
        try:
            duration = probe_duration(path)
            if args.mode == "asr":
                result = {"sources": [{"kind": "audio", "status": "available", "items": evidence._local_asr_items(path)}]}
            else:
                result = evidence.collect_evidence(path, item.get("title", f"第{index + 1}集"), duration)
            (output / f"evidence-{index:02d}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            # Include text and time anchors in the digest, exclude timings.
            sources = [{key: source.get(key) for key in ("kind", "status", "items")} for source in result["sources"]]
            return {"index": index, "durationSec": duration, "elapsedSec": round(time.perf_counter() - started, 3),
                    "timingsMs": result.get("timingsMs"),
                    "sources": [{"kind": source["kind"], "status": source["status"], "items": len(source.get("items") or [])} for source in sources],
                    "evidenceSha256": hashlib.sha256(json.dumps(sources, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
        finally:
            cleanup_materialized(temporary)

    async def batch():
        auth_store.init_db()
        with auth_store.db() as connection:
            for index, item in enumerate(media):
                identity = f"bench-{index:02d}"
                connection.execute(
                    "INSERT INTO tasks (id, user_id, title, file_name, stored_path, status, stage, created_at, updated_at, batch_id, batch_title, batch_index, batch_total) VALUES (?, 'benchmark', ?, ?, ?, 'queued', 'queued', ?, ?, 'benchmark-batch', '实测批次', ?, ?)",
                    (identity, item.get("title", f"第{index + 1}集"), f"{index + 1}.mp4", item["source"], f"2026-10-14T00:00:{index:02d}+00:00", auth_store.now_iso(), index + 1, len(media)))
                if args.evidence_from:
                    result = json.loads((args.evidence_from / f"evidence-{index:02d}.json").read_text(encoding="utf-8"))
                    recognition_cache(identity, item["source"]).save("evidence", result)
        runner = asyncio.create_task(worker.run_worker())
        try:
            while True:
                await asyncio.sleep(1)
                if runner.done():
                    runner.result()
                    raise RuntimeError("Worker exited before batch finished")
                with auth_store.db() as connection:
                    remaining = connection.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0]
                if not remaining:
                    break
            with auth_store.db() as connection:
                for row in connection.execute("SELECT * FROM tasks ORDER BY id"):
                    quality = json.loads(row["quality_json"] or "{}")
                    records.append({"index": len(records), "status": row["status"], "attempts": row["attempts"],
                                    "model": row["model"], "quality": quality, "totalTokens": row["total_tokens"],
                                    "apiCostRmb": row["api_cost_rmb"], "error": row["error"]})
        finally:
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass

    monitor = threading.Thread(target=sample_memory, daemon=True)
    started, cpu_started = time.perf_counter(), time.process_time()
    monitor.start()
    try:
        if args.mode == "batch":
            asyncio.run(batch())
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                records.extend(executor.map(collect, range(len(media))))
    finally:
        stop.set()
        monitor.join(2)
    elapsed = time.perf_counter() - started
    report = {"mode": args.mode, "concurrency": args.concurrency, "asrWorkers": config.LOCAL_ASR_CONCURRENCY,
              "asrCpuThreads": config.LOCAL_ASR_CPU_THREADS, "count": len(media), "elapsedSec": round(elapsed, 3),
              "processCpuSec": round(time.process_time() - cpu_started, 3),
              "processPeakRssMb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
              "minimumHostAvailableMb": minimum_available_mb, "reusedEvidence": bool(args.evidence_from), "records": records}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "records"}), flush=True)
    if any(record.get("status") == "failed" for record in records):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
