"""Read-only comparison on retained scripts/evidence; no model requests or writes."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
import sqlite3
import time

from app.script import normalize_script, script_quality


def main():
    spec = importlib.util.spec_from_file_location('app.stability_baseline', '/baseline_script.py')
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    connection = sqlite3.connect('file:/audit/jubianbian.sqlite3?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    counts, added, removed = Counter(), Counter(), Counter()
    started = time.monotonic()
    slowest = 0
    for row in connection.execute("SELECT result_json,evidence_json,title FROM tasks WHERE created_at >= '2026-10-19T04:00:00' AND result_json IS NOT NULL AND status IN ('done','review')"):
        raw = json.loads(row['result_json'])
        evidence = json.loads(row['evidence_json']) if row['evidence_json'] else None
        old_script = baseline.normalize_script(deepcopy(raw), row['title'])
        new_script = normalize_script(deepcopy(raw), row['title'])
        assert old_script == new_script, 'Historical screenplay changed'
        old_quality = baseline.script_quality(deepcopy(old_script), 'audit', deepcopy(evidence))
        tick = time.monotonic()
        new_quality = script_quality(deepcopy(new_script), 'audit', deepcopy(evidence))
        slowest = max(slowest, time.monotonic() - tick)
        def issues(quality):
            def signatures(items):
                return Counter((x.get('severity'), x.get('tag'), x.get('startSec'), x.get('endSec'))
                               for x in items)
            # Display issues are capped at 100; include the complete blocking
            # list without counting its overlap with display issues twice.
            return signatures(quality.get('issues', [])) | signatures(quality.get('blockingIssues', []))
        old, new = issues(old_quality), issues(new_quality)
        counts['checked'] += 1
        counts['withEvidence'] += bool(evidence)
        counts['stricterTasks'] += bool(new - old)
        counts['fewerIssueTasks'] += bool(old - new)
        counts['displayCappedTasks'] += len(old_quality.get('issues', [])) == 100 or len(new_quality.get('issues', [])) == 100
        for key, count in (new - old).items():
            added[str(key[1])] += count
        for key, count in (old - new).items():
            removed[str(key[1])] += count
    connection.close()
    print(json.dumps(dict(counts=counts, addedIssues=added, removedIssues=removed,
                          screenplayUnchanged=True, readOnly=True,
                          elapsedSeconds=round(time.monotonic()-started, 2),
                          slowestQualitySeconds=round(slowest, 3)), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
