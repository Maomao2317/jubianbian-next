"""Deploy the published customer-copy revision to idle Seoul staging only.

Run from a fresh git archive of the pushed commit. Preserve runtime settings,
data mounts and production identity; retain rollback files and an audit record.
No customer tasks or balances are modified by deployment verification.
"""
from pathlib import Path
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen

BASE = Path(__file__).resolve().parent.parent
ROOT = Path('/opt/jubianbian-staging')
REPO = Path('/opt/jubianbian-github-check')
PROJECT = 'jubianbian-staging'
BASE_COMMIT = 'a3f8bb6'
BUILD = 'customer-copy-20261019-r1'
IMAGE = 'jubianbian-release:' + BUILD
FILES = {'backend/app/routes.py', 'backend/app/user_view.py', 'backend/test_user_review.py',
         'web-staging/index.html', 'web-staging/js/app.js', 'web-staging/js/config.js',
         'web-staging/test-user-review.cjs'}
SELF = 'ops/deploy-customer-copy-staging.py'


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def git(*args):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + str(REPO),
                                    '-C', str(REPO), *args])


def inspect(project):
    return json.loads(subprocess.check_output(['docker', 'inspect', project + '-app-1']))[0]


def fingerprint():
    item = inspect('jubianbian')
    return [item['Id'], item['Image'], item['State']['StartedAt']]


def environment(item):
    return dict(line.split('=', 1) for line in item['Config']['Env'] if '=' in line)


def mounts(item):
    return sorted((x['Source'], x['Destination'], x['RW']) for x in item['Mounts'])


def idle():
    code = "import sqlite3; c=sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro',uri=True); print(c.execute(\"select count(*) from tasks where status in ('queued','running','uploading')\").fetchone()[0])"
    count = int(subprocess.check_output(['docker', 'exec', PROJECT + '-app-1', 'python', '-c', code]))
    if count:
        raise RuntimeError(f'{count} active staging tasks; deployment deferred')


def compose(build, *args, override=None):
    command = ['docker', 'compose', '--env-file', ROOT / 'fangzhou.env', '-p', PROJECT,
               '-f', ROOT / 'docker-compose.yml', '-f', ROOT / 'release-image.yml']
    if override:
        command += ['-f', override]
    run(command + list(args), env=dict(os.environ, JBB_BUILD_VERSION=build))


def health(build):
    for _ in range(30):
        try:
            with urlopen('http://127.0.0.1:18000/api/health', timeout=3) as response:
                value = json.load(response)
            if value.get('status') == 'ok' and value.get('build') == build and value.get('environment') == 'staging':
                return value
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Staging health/build check failed')


def main(commit):
    assert re.fullmatch(r'[0-9a-f]{40}', commit)
    lock = Path('/run/lock/jubianbian-staging-deploy.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert git('remote', 'get-url', 'origin').decode().strip() == 'https://github.com/Maomao2317/jubianbian-next.git'
    ref = 'refs/heads/codex/repair-batch-content'
    assert git('ls-remote', 'origin', ref).decode().split()[0] == commit, 'Commit must be pushed first'
    run(['git', '-c', 'safe.directory=' + str(REPO), '-C', REPO, 'merge-base', '--is-ancestor', BASE_COMMIT, commit])
    changed = set(git('diff', '--name-only', BASE_COMMIT, commit).decode().splitlines())
    assert changed == FILES | {SELF}, 'Unexpected release scope'
    digests = {}
    for path in FILES | {SELF}:
        data = (BASE / path).read_bytes()
        assert data == git('show', commit + ':' + path), path + ': differs from pushed source'
        digests[path] = hashlib.sha256(data).hexdigest()
        if path == SELF:
            continue
        exists = bool(git('ls-tree', BASE_COMMIT, '--', path).strip())
        if exists:
            before = git('show', BASE_COMMIT + ':' + path).decode().replace('\r\n', '\n')
            assert (ROOT / path).read_text() == before, path + ': staging baseline differs'
        else:
            assert not (ROOT / path).exists(), path + ': unexpected existing file'
    production = fingerprint()
    previous = inspect(PROJECT)
    old_env = environment(previous)
    assert old_env['JBB_ENVIRONMENT'] == 'staging'
    old_build = old_env['JBB_BUILD_VERSION']
    idle()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    rollback = 'jubianbian-staging-app:customer-copy-before-' + stamp.lower()
    run(['docker', 'tag', previous['Image'], rollback])
    dockerfile = BASE / 'Dockerfile.overlay'
    dockerfile.write_text('FROM ' + rollback + '\nCOPY backend/app /app/backend/app\n'
                          'COPY backend/test_*.py /app/backend/\nCOPY web-staging /app/web-staging\n')
    run(['docker', 'build', '--network=none', '--pull=false', '-t', IMAGE, '-f', dockerfile, BASE])
    run(['docker', 'run', '--rm', '--network', 'none', '-e', 'JBB_DATA_DIR=/tmp/jbb-validation',
         '-w', '/app/backend', '--entrypoint', 'python', IMAGE, '-m', 'unittest', 'discover', '-s', '.'])
    assert production == fingerprint(), 'Production changed externally; aborting'
    idle()
    backup = BASE / ('backup-staging-' + stamp)
    backup.mkdir(mode=0o700)
    targets = sorted(FILES) + ['fangzhou.env', 'release-image.yml', '.staging-deploy-hold']
    existed = {}
    for path in targets:
        original = ROOT / path
        existed[path] = original.exists()
        if original.exists():
            target = backup / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    rollback_override = backup / 'rollback-compose.yml'
    rollback_override.write_text('services:\n  app:\n    image: ' + rollback + '\n')
    record = dict(commit=commit, build=BUILD, files=digests, backup=str(backup),
                  previousImage=previous['Image'], previousBuild=old_build,
                  rollbackImage=rollback, productionBefore=production)
    record_path = BASE / 'staging-deployment.json'
    record_path.write_text(json.dumps(record, indent=2))
    replaced = False
    try:
        for path in FILES:
            target = ROOT / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BASE / path, target)
        envfile = ROOT / 'fangzhou.env'
        lines = [line for line in envfile.read_text().splitlines() if line.partition('=')[0].strip() != 'JBB_BUILD_VERSION']
        envfile.write_text('\n'.join(lines + ['JBB_BUILD_VERSION=' + BUILD]) + '\n')
        envfile.chmod(0o600)
        (ROOT / 'release-image.yml').write_text('services:\n  app:\n    image: ' + IMAGE + '\n')
        (ROOT / '.staging-deploy-hold').write_text('Pinned ' + BUILD + '; commit ' + commit + '; backup ' + str(backup) + '\n')
        compose(BUILD, 'config', '--quiet')
        replaced = True
        compose(BUILD, 'up', '-d', '--no-build', '--no-deps', 'app')
        record['health'] = health(BUILD)
        for path in ['index.html', 'js/app.js', 'js/config.js']:
            with urlopen('http://127.0.0.1:18000/' + path, timeout=5) as response:
                assert hashlib.sha256(response.read()).hexdigest() == digests['web-staging/' + path]
        current = inspect(PROJECT)
        current_env = environment(current)
        assert {k: v for k, v in current_env.items() if k != 'JBB_BUILD_VERSION'} == {k: v for k, v in old_env.items() if k != 'JBB_BUILD_VERSION'}
        assert mounts(current) == mounts(previous), 'Data mount changed'
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings']
        assert production == fingerprint(), 'Production changed externally'
        record.update(productionAfter=fingerprint(), newImage=current['Image'],
                      configurationPreserved=True, dataMountsPreserved=True)
        record_path.write_text(json.dumps(record, indent=2))
        print('STAGING_DEPLOYMENT_VERIFIED', json.dumps(record), flush=True)
    except BaseException:
        for path in targets:
            if existed[path]:
                shutil.copy2(backup / path, ROOT / path)
            elif (ROOT / path).exists():
                (ROOT / path).unlink()
        if replaced:
            compose(old_build, 'up', '-d', '--no-build', '--no-deps', 'app', override=rollback_override)
            health(old_build)
        print('STAGING_ROLLED_BACK', str(backup), flush=True)
        raise


if __name__ == '__main__':
    main(sys.argv[1])
