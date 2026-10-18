"""Read-only replay of the reported production scene-fragmentation case."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
import sqlite3

from app.script import normalize_script, screenplay_output_scenes, script_to_markdown

spec = importlib.util.spec_from_file_location('app.scene_baseline', '/baseline_script.py')
baseline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baseline)
c = sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro', uri=True)
c.row_factory = sqlite3.Row
r = c.execute('SELECT id,title,result_json FROM tasks WHERE id=?',
              ('task-d646d51453b640f2b12dd471f1611810',)).fetchone()
assert r is not None and r['result_json']
raw = json.loads(r['result_json'])
before = deepcopy(raw)
old = baseline.normalize_script(deepcopy(raw), r['title'])
new = normalize_script(raw, r['title'])
assert raw == before, 'Read normalization mutated input'


def blocks(script):
    return Counter(json.dumps(block, sort_keys=True, ensure_ascii=False)
                   for scene in script['scenes'] for block in scene['blocks'])


assert blocks(old) == blocks(new), 'Block content/attribution changed during consolidation'
visible = screenplay_output_scenes(new)
assert len(visible) < len(old['scenes']), 'Reported fragmentation was not reduced'
assert all(any(not b.get('suppressInOutput') and (str(b.get('text') or '').strip() or b['type'] == 'transition')
               for b in s['blocks']) for s in visible)
md = script_to_markdown({'result': new})
again = normalize_script(deepcopy(new), r['title'])
assert script_to_markdown({'result': again}) == md, 'Repeated normalization changes rendered content'
headings = [line for line in md.splitlines() if any(line == s['heading'] for s in visible)]
assert headings == [s['heading'] for s in visible], 'Export headings differ from visible scenes'
print(json.dumps(dict(taskId=r['id'], rawScenes=len(raw['scenes']), previousScenes=len(old['scenes']),
                     newScenes=len(new['scenes']), visibleScenes=len(visible),
                     blocks=sum(blocks(new).values()), allBlocksUnchanged=True,
                     repeatNormalizationStable=True, exportVerified=True), ensure_ascii=False))
