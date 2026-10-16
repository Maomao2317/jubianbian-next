"""Scan first; explicitly apply additive repairs to one existing batch.

Run in the application container with PYTHONPATH=/app/backend. This reuses
persisted evidence, never debits user credits, and keeps a recheck snapshot.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from app.auth_store import db, now_iso
from app.config import DATA_DIR, DB_PATH
from app.content_coverage import content_gaps
from app.logging_setup import safe_error_text
from app.rechecks import create_recheck_snapshot, latest_pending_recheck


def rows_for_batch(batch_id):
    with db() as connection:
        return connection.execute(
            "SELECT * FROM tasks WHERE batch_id=? ORDER BY batch_index,created_at,id", (batch_id,)
        ).fetchall()


def preserved_blocks(before, after):
    """A repair may add words, but must retain every original typed utterance."""
    def counts(script):
        return Counter((b.get("type"), b.get("text"), b.get("speaker"))
                       for s in script.get("scenes") or [] for b in s.get("blocks") or [])
    return not (counts(before) - counts(after))


def save_candidate(row, before, script, quality, usage):
    if not preserved_blocks(before, script):
        raise ValueError("repair changed original words; candidate not published")
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
        if (current["updated_at"] != row["updated_at"] or current["result_json"] != row["result_json"]
                or current["status"] not in {"done", "review"}
                or latest_pending_recheck(connection, row["id"])):
            raise ValueError("task changed or already has a pending recheck; candidate retained in cache")
        create_recheck_snapshot(connection, current)
        values = dict(result_json=json.dumps(script, ensure_ascii=False),
                      quality_json=json.dumps(quality, ensure_ascii=False),
                      status="review", stage="review", progress_percent=100, error=None,
                      model=quality["contentRepair"]["model"], updated_at=now_iso())
        for key in ("input_tokens", "output_tokens", "total_tokens", "api_cost_rmb"):
            values[key] = (row[key] or 0) + (usage.get(key) or 0)
        connection.execute(f"UPDATE tasks SET {','.join(k+'=?' for k in values)} WHERE id=?",
                           (*values.values(), row["id"]))


async def repair_one(row, *, stop_event=None):
    from app.processing import repair_content_gaps, select_ark_route
    from app.recognition_cache import recognition_cache
    from app.storage import materialize_media, cleanup_materialized
    from app.task_store import batch_character_context
    before = json.loads(row["result_json"])
    evidence = json.loads(row["evidence_json"] or "{}")
    duration = row["duration_sec"]
    evidence["durationSec"] = duration
    context = batch_character_context(row)
    if context:
        evidence["batchCharacterContext"] = context
    cache = await asyncio.to_thread(recognition_cache, row["id"], row["stored_path"])
    path, directory = await asyncio.to_thread(materialize_media, row["stored_path"], row["id"])
    try:
        script, quality, usage = await repair_content_gaps(
            path, row["title"], duration, before, evidence, select_ark_route(duration, evidence),
            cache=cache, stop_event=stop_event)
        # An error without additions must not turn a usable old result into a
        # replacement. The failed attempt remains available in the audit cache.
        if quality["contentRepair"]["completedRanges"]:
            save_candidate(row, before, script, quality, usage)
        report = quality["contentRepair"]
        print(json.dumps({"task": row["id"], "title": row["title"], **report}, ensure_ascii=False), flush=True)
        return any(e.get("balanceError") for e in report["errors"])
    finally:
        await asyncio.to_thread(cleanup_materialized, directory)


async def run_targets(targets, concurrency=1):
    stop = asyncio.Event()
    pending = iter(targets)

    async def worker():
        while not stop.is_set():
            row = next(pending, None)
            if row is None:
                return
            try:
                if await repair_one(row, stop_event=stop):
                    stop.set()
                    print("STOP: provider balance insufficient; remaining calls paused", flush=True)
            except Exception as exc:
                stop.set()
                print(json.dumps({"task": row["id"], "error": safe_error_text(exc)}, ensure_ascii=False), flush=True)

    await asyncio.gather(*(worker() for _ in range(min(concurrency, len(targets)))))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--episodes", type=int, nargs="+")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=1)
    args = parser.parse_args()
    rows = rows_for_batch(args.batch)
    if not rows:
        raise SystemExit("batch not found")
    targets = []
    for episode, row in enumerate(rows, 1):
        if args.episodes and episode not in args.episodes:
            continue
        script = json.loads(row["result_json"] or "{}")
        gaps = content_gaps(script, row["duration_sec"])
        if not gaps:
            continue
        with db() as connection:
            pending = bool(latest_pending_recheck(connection, row["id"]))
        eligible = row["status"] in {"done", "review"} and not pending and bool(row["result_json"])
        print(json.dumps(dict(episode=episode, task=row["id"], gaps=gaps, eligible=eligible), ensure_ascii=False), flush=True)
        if eligible:
            targets.append(row)
    if not args.apply or not targets:
        return
    # Use SQLite's consistent backup API, rather than copying a live DB file.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = DATA_DIR / "repair-backups" / stamp
    folder.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as source, sqlite3.connect(folder / "before.sqlite3") as backup:
        source.backup(backup)
    await run_targets(targets, args.concurrency)


if __name__ == "__main__":
    asyncio.run(main())
