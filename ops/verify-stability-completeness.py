"""Read-only comparison on retained scripts/evidence; no model requests or writes."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
import re
import sqlite3
import time

from app.script import (normalize_script, script_quality, restore_spoken_subtitles,
                        evidence_comparison_text, add_character_visual_descriptions)


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
    patterns = []
    for fact in sorted(facts, key=len, reverse=True):
        key = re.sub('^' + verb, '', fact.strip()).strip('的 ')
        if key:
            pattern = re.escape(key).replace('全套制服', '制服').replace('制服', '(?:全套)?制服')
            patterns.append(re.compile(verb + pattern + r'(?:的)?'))
    # The deployed enricher could insert a fact into the middle of the same
    # fact. Removing the inner copy exposes the complete outer copy, so strip
    # exact known facts to a fixed point before comparing the action itself.
    while True:
        previous = text
        for pattern in patterns:
            text = pattern.sub('', text)
        if text == previous:
            break
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


def verify_description_stability(script):
    for scene in script['scenes']:
        blocks = deepcopy(scene['blocks'])
        add_character_visual_descriptions(blocks, script.get('characterProfiles', []))
        assert blocks == scene['blocks'], 'Repeated enrichment changed descriptions'


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
        repaired_raw = deepcopy(raw)
        recovered = restore_spoken_subtitles(repaired_raw, evidence)
        counts['recoveredSpokenSubtitles'] += recovered
        if recovered:
            for before, after in zip(raw['scenes'], repaired_raw['scenes']):
                for left, right in zip(before.get('blocks', []), after.get('blocks', [])):
                    if left == right:
                        continue
                    assert left.get('type') in {'screen_text', 'subtitle', 'caption'}
                    assert left.get('screenType', 'subtitle') == 'subtitle'
                    assert right.get('type') in {'dialogue', 'vo'}
                    assert evidence_comparison_text(left['text']) == evidence_comparison_text(right['text'])
                    assert (left.get('startSec'), left.get('endSec')) == (right.get('startSec'), right.get('endSec'))
        new_script = normalize_script(deepcopy(raw), row['title'], evidence)
        counts['changedTasks'] += old_script != new_script
        compare_scripts(baseline.normalize_script(repaired_raw, row['title']), new_script, counts)
        verify_description_stability(new_script)
        repeated = normalize_script(deepcopy(new_script), row['title'], evidence)
        # Existing scene merging can expose repeated action subjects to the
        # second normalization pass. Compare that pass with the deployed
        # baseline too, while checking appearance enrichment itself exactly.
        compare_scripts(baseline.normalize_script(deepcopy(new_script), row['title']), repeated, Counter())
        counts['secondPassTextChanges'] += any(
            a.get('text') != b.get('text') for x, y in zip(new_script['scenes'], repeated['scenes'])
            for a, b in zip(x['blocks'], y['blocks']))
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
