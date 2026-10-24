"""Read-only release replay: identical screenplay/quality and preserved P0 triggers."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
import sqlite3
import time

from app import processing, providers
from app.script import normalize_script, script_quality, quality_gate


def main():
    spec = importlib.util.spec_from_file_location('app.cost_baseline', '/baseline_script.py')
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    connection = sqlite3.connect('file:/audit/jubianbian.sqlite3?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    counts = Counter()
    started = time.monotonic()
    for row in connection.execute("SELECT result_json,evidence_json,title,duration_sec,quality_json FROM tasks "
                                  "WHERE result_json IS NOT NULL AND status IN ('done','review')"):
        raw = json.loads(row['result_json'])
        evidence = json.loads(row['evidence_json']) if row['evidence_json'] else None
        old = baseline.normalize_script(deepcopy(raw), row['title'])
        new = normalize_script(deepcopy(raw), row['title'])
        assert old == new, 'Screenplay normalization changed'
        before = baseline.script_quality(deepcopy(old), 'audit', deepcopy(evidence))
        after = script_quality(deepcopy(new), 'audit', deepcopy(evidence))
        assert before == after, 'Quality detector or threshold changed'
        quality = json.loads(row['quality_json'] or '{}')
        triggered = processing._needs_automated_content_review(quality)
        blocking = quality_gate(quality)[1]
        if blocking:
            assert triggered, 'P0 review lost'
            assert processing._automated_review_issues(quality)[:len(blocking)] == blocking
            counts['p0ReviewPreserved'] += 1
        counts['checked'] += 1
        counts['newReviewTriggers'] += bool(triggered)
        context = processing._automated_review_context(new, processing._automated_review_issues(quality), quality=quality)
        counts['localReviewCandidates'] += bool(triggered and providers._review_clip_bounds(context, row['duration_sec'] or 0))
    print(json.dumps(dict(counts=counts, readOnly=True, screenplayAndQualityIdentical=True,
                          elapsedSeconds=round(time.monotonic()-started, 2))), flush=True)


if __name__ == '__main__':
    main()
