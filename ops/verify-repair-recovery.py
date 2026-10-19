"""Read-only legacy replay: no providers, credentials, or business writes."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

from app import config
from app.media import title_with_episode
from app.script import normalize_script, screenplay_output_scenes, script_to_markdown
from app.task_store import row_to_task

TARGETS = {'task-26a131d9c0bb4b8f93f918e9a4620d52': 6,
           'task-6a5f6a8bc7e840ceb10869d3a3ea9efb': None}


def blocks(script):
    return Counter(json.dumps(b, sort_keys=True, ensure_ascii=False)
                   for s in script.get('scenes', []) for b in s.get('blocks', []))


def main():
    started = time.monotonic()
    # Imports/logging use writable scratch; only audit reads use business data.
    config.DATA_DIR = Path('/app/data')
    spec = importlib.util.spec_from_file_location('app.repair_baseline', '/baseline_script.py')
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    c = sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    changed, targets, checked = [], [], 0
    for row in c.execute("SELECT * FROM tasks WHERE result_json IS NOT NULL AND status IN ('done','review')"):
        raw = json.loads(row['result_json'])
        untouched = deepcopy(raw)
        title = title_with_episode(row['title'], row['file_name'])
        old = baseline.normalize_script(deepcopy(raw), title)
        # Exercise the real API/read adapter, including retained model audits.
        new_task = row_to_task(row)
        new = new_task['result']
        assert blocks(old) == blocks(new), row['id'] + ': changed a screenplay block'
        assert raw == untouched, row['id'] + ': mutated original JSON'
        assert Counter(s.get('segmentType') for s in new['scenes'] if any(b.get('type') == 'transition' for b in s['blocks'])) == Counter(s.get('segmentType') for s in old['scenes'] if any(b.get('type') == 'transition' for b in s['blocks'])), row['id'] + ': changed an explicit transition boundary'
        if len(old['scenes']) != len(new['scenes']):
            md = script_to_markdown(new_task)
            again = normalize_script(deepcopy(new), title)
            old_again = baseline.normalize_script(deepcopy(new), title)
            # Some historical character descriptions already expand again
            # on a second normalization. Compare both normalizers on the same
            # scene arrangement: descriptions are scoped to a scene, so the
            # old fragmented arrangement is not an equivalent second input.
            # The original first-pass comparison above remains byte exact for
            # every block. The corrected scene structure must also be stable.
            assert blocks(old_again) == blocks(again), row['id'] + ': introduced a repeated-normalization content change'
            assert [s['heading'] for s in again['scenes']] == [s['heading'] for s in new['scenes']], row['id'] + ': unstable scene structure'
            visible = screenplay_output_scenes(new)
            assert [s['heading'] for s in visible] == [line for line in md.splitlines() if line in {s['heading'] for s in visible}], row['id'] + ': export mismatch'
            record = dict(taskId=row['id'], oldScenes=len(old['scenes']), newScenes=len(new['scenes']),
                          visibleScenes=len(visible), blocks=sum(blocks(new).values()),
                          textRenormalizationStable=blocks(again) == blocks(new))
            changed.append(record)
            if row['id'] in TARGETS:
                record['headings'] = [s['heading'] for s in visible]
                targets.append(record)
        checked += 1
    if sys.argv[-1] == 'production':
        assert {x['taskId'] for x in targets} == set(TARGETS), 'Reported tasks were not repaired'
        for item in targets:
            assert item['newScenes'] < item['oldScenes'], item
            if TARGETS[item['taskId']]:
                assert item['newScenes'] <= TARGETS[item['taskId']], item
    c.close()
    print(json.dumps(dict(environment=sys.argv[-1], checkedTasks=checked, changedTasks=changed,
                         targets=targets, allBlocksUnchanged=True, transitionsPreserved=True,
                         exportsVerified=True, repeatSceneStructureStable=True,
                         repeatedNormalizationMatchesBaselineOnSameInput=True,
                         elapsedSeconds=round(time.monotonic()-started, 2)), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
