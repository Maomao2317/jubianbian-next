"""Reassess saved scripts without media processing, provider calls or DB writes.

Run with backend on PYTHONPATH. Input is a JSON list of task snapshots; each
snapshot contains the same result_json/evidence_json fields as a persisted task.
"""

import collections
import copy
import hashlib
import json
import sys

from app.script import has_screenplay_body, normalize_script, quality_gate, script_quality


def decode(value):
    return json.loads(value or "{}") if isinstance(value, str) else (value or {})


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def replay(rows):
    original = fingerprint(rows)
    results = []
    for row in rows:
        script = decode(row["result_json"])
        evidence = {**decode(row["evidence_json"]), "durationSec": row["duration_sec"]}
        saved = fingerprint(script)
        normalized = normalize_script(copy.deepcopy(script), row["title"])
        quality = script_quality(normalized, row["provider"], evidence)
        approved, blockers = quality_gate(quality)
        assert fingerprint(script) == saved, "replay changed source script"
        results.append({
            "episode": row["batch_index"], "savedStatus": row["status"],
            "bodyPresent": has_screenplay_body(normalized), "approved": approved,
            "normalizedChanged": normalized != script,
            "blockingTags": dict(collections.Counter(i.get("tag") for i in blockers)),
            "blockingCount": len(blockers), "contentGapRanges": quality.get("contentGapRanges", []),
            "transcriptCoverage": quality.get("transcriptCoverage"),
            "dialogueEvidencePrecision": quality.get("dialogueEvidencePrecision"),
            "ocrCoverage": quality.get("ocrCoverage"),
            "severityCounts": quality.get("severityCounts"),
            "displayIssueTags": dict(collections.Counter(i.get("tag") for i in quality.get("issues", []))),
        })
    assert fingerprint(rows) == original, "replay changed input snapshot"
    return {
        "sourceSha256": original, "sourceUnchanged": True, "episodes": results,
        "summary": {
            "total": len(results), "approved": sum(r["approved"] for r in results),
            "review": [r["episode"] for r in results if not r["approved"]],
            "empty": [r["episode"] for r in results if not r["bodyPresent"]],
            "gaps": {r["episode"]: r["contentGapRanges"] for r in results if r["contentGapRanges"]},
            "blockingTags": dict(sum((collections.Counter(r["blockingTags"]) for r in results), collections.Counter())),
        },
    }


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as source:
        print(json.dumps(replay(json.load(source)), ensure_ascii=False, separators=(",", ":")))
