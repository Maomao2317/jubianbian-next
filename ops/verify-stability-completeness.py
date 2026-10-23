"""Read-only comparison on retained scripts/evidence; no model requests or writes."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
import re
import sqlite3
import time

from app.script import normalize_script, script_quality


def action_core(text, profiles):
    """Ignore only profile facts while proving actions survived enrichment."""
    verb = r"(?:身穿|身着|穿着|穿|戴着|戴|梳着|梳|留着|留|扎着|扎|披着|披)?"
    facts = set()
    for profile in profiles:
        for field in ('appearance', 'clothing'):
            value = str(profile.get(field) or '').strip('。！？；; ')
            facts.update(re.split(r'[、，；]', value))
        if not (profile.get('appearance') or profile.get('clothing')):
            value = str(profile.get('firstAppearance') or '')
            name = str(profile.get('name') or '')
            if name and name in value:
                facts.add(value.split(name, 1)[0].rstrip('的，、；：: '))
    for fact in sorted(facts, key=len, reverse=True):
        key = re.sub('^' + verb, '', fact.strip()).strip('的 ')
        if key:
            pattern = re.escape(key).replace('全套制服', '制服').replace('制服', '(?:全套)?制服')
            text = re.sub(verb + pattern, '', text)
    return re.sub(r'[\s的、，。！？；：:（）()]', '', text)


def compare_scripts(old, new, counts):
    old, new = deepcopy(old), deepcopy(new)
    assert len(old['scenes']) == len(new['scenes']), 'Scene count changed'
    profiles = new.get('characterProfiles', [])
    for before, after in zip(old['scenes'], new['scenes']):
        assert len(before['blocks']) == len(after['blocks']), 'Block count changed'
        for left, right in zip(before['blocks'], after['blocks']):
            if left.get('type') == right.get('type') == 'action':
                a, b = left.pop('text', ''), right.pop('text', '')
                if a != b:
                    counts['changedActionBlocks'] += 1
                    assert action_core(a, profiles) == action_core(b, profiles), 'Action content changed beyond profile facts'
    assert old == new, 'Dialogue, metadata, timing or structure changed'


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
    for row in connection.execute("SELECT result_json,evidence_json,title FROM tasks WHERE result_json IS NOT NULL AND status IN ('done','review')"):
        raw = json.loads(row['result_json'])
        evidence = json.loads(row['evidence_json']) if row['evidence_json'] else None
        old_script = baseline.normalize_script(deepcopy(raw), row['title'])
        new_script = normalize_script(deepcopy(raw), row['title'])
        counts['changedTasks'] += old_script != new_script
        compare_scripts(old_script, new_script, counts)
        repeated = normalize_script(deepcopy(new_script), row['title'])
        assert [[b.get('text') for b in s['blocks']] for s in repeated['scenes']] == [[b.get('text') for b in s['blocks']] for s in new_script['scenes']], 'Repeated normalization grew descriptions'
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
                          dialogueMetadataActionsPreserved=True, readOnly=True,
                          elapsedSeconds=round(time.monotonic()-started, 2),
                          slowestQualitySeconds=round(slowest, 3)), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
