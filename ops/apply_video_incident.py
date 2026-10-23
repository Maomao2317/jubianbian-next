"""Apply a private reviewed incident plan with row guards and recoverable backups.

Plans contain business data and must stay outside Git. Each entry includes the
exact preflight task row; retries additionally require a verified size/etag.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


def repair_captions(script, selectors):
    result = deepcopy(script)
    assert selectors and len({tuple(x) for x in selectors}) == len(selectors)
    for scene, index in selectors:
        block = result['scenes'][scene]['blocks'][index]
        assert block.get('type') in {'screen_text', 'subtitle', 'caption'}
        assert block.get('screenType', 'subtitle') == 'subtitle'
        assert block.get('text') and block.get('startSec') is not None
        block.update(type='vo', speaker='未知说话人', voKind='unknown',
                     textSource='audio', sourceText=block['text'], subtitleRecovered=True)
        block.pop('screenType', None)
        block.pop('suppressInOutput', None)
    return result


def private_json(path, payload):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    from app import config, storage
    from app.auth_store import now_iso
    from app.rechecks import latest_pending_recheck
    from app.task_store import _insert_task_event, row_to_task
    from app.script import script_to_markdown

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('mode', choices=['caption', 'retry'])
    parser.add_argument('--task', action='append')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    assert plan['version'] == 1
    assert os.environ.get('JBB_BUILD_VERSION') == plan['requiredBuild']
    entries = plan['captions' if args.mode == 'caption' else 'retries']
    if args.task:
        entries = [entry for entry in entries if entry['before']['id'] in args.task]
        assert len(entries) == len(set(args.task))
    assert entries and (args.mode != 'retry' or len(entries) <= 2), 'Retry at most two tasks per batch'
    connection = sqlite3.connect(config.DB_PATH)
    connection.row_factory = sqlite3.Row
    candidates = []
    for entry in entries:
        before = entry['before']
        current = connection.execute('SELECT * FROM tasks WHERE id=?', (before['id'],)).fetchone()
        assert current and dict(current) == before, 'Task changed since review'
        assert not latest_pending_recheck(connection, before['id'])
        if args.mode == 'caption':
            assert before['status'] in {'done', 'review'} and before['result_json']
            result = repair_captions(json.loads(before['result_json']), entry['blocks'])
            values = dict(result_json=json.dumps(result, ensure_ascii=False), updated_at=now_iso())
            task = row_to_task({**before, **values})
            for scene, index in entry['blocks']:
                original = json.loads(before['result_json'])['scenes'][scene]['blocks'][index]
                assert any(b.get('type') == 'vo' and b.get('startSec') == original['startSec']
                           for s in task['result']['scenes'] for b in s['blocks']), 'API lost recovered voice'
            assert script_to_markdown(task)
        else:
            assert before['status'] == 'failed' and before['credits_used'] == 0 and not before['result_json']
            expected = entry['media']
            assert expected['etag'] and expected['size'] == before['file_size']
            assert storage.media_info(before['stored_path']) == expected, 'Original media changed or missing'
            values = dict(status='queued', stage='queued', progress_percent=4, stage_progress_percent=None,
                          error=None, attempts=0, completed_at=None, updated_at=now_iso())
        candidates.append((before, values))
    print(json.dumps({'mode': args.mode, 'tasks': [x[0]['id'] for x in candidates],
                      'guarded': True, 'apply': args.apply}), flush=True)
    if not args.apply:
        return
    folder = Path(config.DATA_DIR) / 'incident-recovery' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder.mkdir(parents=True, mode=0o700)
    private_json(folder / 'before.json', [x[0] for x in candidates])
    connection.execute('BEGIN IMMEDIATE')
    try:
        if args.mode == 'retry':
            active = connection.execute("SELECT count(*) FROM tasks WHERE status IN ('running','queued')").fetchone()[0]
            assert active + len(candidates) <= 2, 'Wait for the current small recovery batch'
        users = {row['user_id'] for row, _ in candidates}
        def billing():
            return {user: (list(connection.execute('SELECT * FROM users WHERE id=?', (user,)).fetchone()),
                           [list(x) for x in connection.execute('SELECT * FROM credit_ledger WHERE user_id=? ORDER BY id', (user,))])
                    for user in users}
        before_billing = billing()
        for before, values in candidates:
            current = connection.execute('SELECT * FROM tasks WHERE id=?', (before['id'],)).fetchone()
            assert dict(current) == before and not latest_pending_recheck(connection, before['id'])
            connection.execute('UPDATE tasks SET '+','.join(key+'=?' for key in values)+' WHERE id=?',
                               (*values.values(), before['id']))
            _insert_task_event(connection, before['id'], 'incident_'+args.mode,
                               'Reviewed incident recovery; original row backed up; balance and ledger unchanged',
                               status=values.get('status', before['status']), stage=values.get('stage', before['stage']))
        assert billing() == before_billing, 'Billing changed'
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    print('INCIDENT_APPLIED', json.dumps({'mode': args.mode, 'count': len(candidates),
                                        'backup': str(folder), 'billingUnchanged': True}), flush=True)


if __name__ == '__main__':
    main()
