"""Print benchmark progress and comparable results without media URLs or secrets.

Pass one or more isolated output directories from benchmark_batch.py. This
reader never imports the application and opens supplied databases read-only.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
import statistics


QUALITY_KEYS = ("transcriptCoverage", "dialogueEvidencePrecision", "ocrCoverage")


def summarize(directory: Path, details: bool = False) -> dict:
    report_path = directory / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    result = {"run": directory.name, "complete": bool(report)}
    for key in ("mode", "concurrency", "count", "elapsedSec", "processCpuSec",
                "processPeakRssMb", "minimumHostAvailableMb", "reusedEvidence"):
        if key in report:
            result[key] = report[key]
    records = report.get("records", [])
    database = directory / "data" / "jubianbian.sqlite3"
    if database.is_file():
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT status, stage, count(*) FROM tasks GROUP BY status, stage").fetchall()
            result["stages"] = {f"{status}/{stage}": count for status, stage, count in rows}
            if not report:
                records = [{"index": index, "status": status, "attempts": attempts,
                            "quality": json.loads(quality or "{}"), "totalTokens": tokens}
                           for index, (status, attempts, quality, tokens) in enumerate(connection.execute(
                               "SELECT status, attempts, quality_json, total_tokens FROM tasks ORDER BY id"))]
        finally:
            connection.close()
    if any("status" in row for row in records):
        result["statuses"] = dict(Counter(row["status"] for row in records))
        result["taskRetries"] = sum(row.get("attempts") or 0 for row in records)
    completed = [row for row in records if row.get("status") in {"done", "review"}]
    if completed:
        result["totalTokens"] = sum(row.get("totalTokens") or 0 for row in completed)
        result["quality"] = {}
        for key in QUALITY_KEYS:
            values = [row["quality"].get(key) for row in completed]
            values = [value for value in values if isinstance(value, (int, float))]
            result["quality"][key] = {"count": len(values), "mean": round(statistics.mean(values), 2),
                                      "min": min(values), "max": max(values)} if values else None
        result["severityCounts"] = {key: sum(row["quality"].get("severityCounts", {}).get(key, 0)
                                           for row in completed) for key in ("P0", "P1", "P2")}
        if details:
            result["episodes"] = [{"index": row["index"], "status": row["status"],
                                   **{key: row["quality"].get(key) for key in QUALITY_KEYS},
                                   "P0": row["quality"].get("severityCounts", {}).get("P0", 0)}
                                  for row in completed]
    if report.get("mode") in {"asr", "evidence"}:
        result["durationSec"] = round(sum(row.get("durationSec", 0) for row in records), 3)
        result["evidenceSources"] = dict(Counter(f"{source['kind']}/{source['status']}"
                                                for row in records for source in row.get("sources", [])))
    log_files = sorted((directory / "data" / "logs").glob("app.log*"))
    operations = Counter()
    http_429 = task_retries = 0
    for path in log_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            operations.update(re.findall(r"\boperation=([a-z_]+)\b", line))
            http_429 += bool(re.search(r"\bstatus_code=429\b", line))
            task_retries += "task_retry_auto " in line
    result["operations"] = dict(operations)
    result["http429LogEvents"] = http_429
    result["taskRetryLogEvents"] = task_retries
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--details", action="store_true")
    arguments = parser.parse_args()
    for output in arguments.directories:
        if not output.is_dir():
            parser.error(f"Benchmark directory does not exist: {output}")
        print(json.dumps(summarize(output, arguments.details), ensure_ascii=False, indent=2))
