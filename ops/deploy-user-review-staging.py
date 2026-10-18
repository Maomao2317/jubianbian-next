"""Deploy the reviewed user-confirmation overlay to staging only.

Run as root from a release bundle made from the published Git commit. The
bundle manifest contains SHA256 digests and normalized pre-change digests.
Production is inspected only; its container must remain exactly unchanged.
No business tasks are approved and no recognition providers are called.
"""
from pathlib import Path
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
from urllib.request import urlopen

BASE = Path(__file__).resolve().parent.parent
ROOT = Path('/opt/jubianbian-staging')
PROJECT = 'jubianbian-staging'
BUILD = 'user-review-20261018-r1'
IMAGE = 'jubianbian-release:' + BUILD


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def inspect(project):
    return json.loads(subprocess.check_output(['docker', 'inspect', project + '-app-1']))[0]


def fingerprint(project):
    value = inspect(project)
    return [value['Id'], value['Image'], value['State']['StartedAt']]


def idle():
    code = "import sqlite3; c=sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro',uri=True); print(c.execute(\"select count(*) from tasks where status in ('queued','running','uploading')\").fetchone()[0])"
    count = int(subprocess.check_output(['docker', 'exec', PROJECT + '-app-1', 'python', '-c', code]))
    if count:
        raise RuntimeError(f'{count} active staging tasks; no deployment performed')


def compose(*args, build=BUILD):
    run(['docker', 'compose', '--env-file', ROOT / 'fangzhou.env', '-p', PROJECT,
         '-f', ROOT / 'docker-compose.yml', '-f', ROOT / 'release-image.yml', *args],
        env=dict(os.environ, JBB_BUILD_VERSION=build))


def healthy(build):
    for _ in range(30):
        try:
            with urlopen('http://127.0.0.1:18000/api/health', timeout=3) as response:
                value = json.load(response)
            if value.get('status') == 'ok' and value.get('build') == build:
                return value
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Staging health/build verification failed')


def main():
    lock = Path('/run/lock/jubianbian-staging-deploy.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = json.loads((BASE / 'manifest.json').read_text())
    files = list(manifest['files'])
    allowed = {'backend/app/routes.py', 'backend/app/main.py', 'backend/app/processing.py',
               'backend/app/auth_store.py', 'backend/test_user_review.py', 'backend/test_upload_flow.py', 'web-staging/index.html',
               'web-staging/js/config.js', 'web-staging/js/api.js', 'web-staging/js/app.js',
               'web-staging/css/app.css', 'web-staging/test-user-review.cjs'}
    assert set(files) == allowed
    for relative, digest in manifest['files'].items():
        assert hashlib.sha256((BASE / relative).read_bytes()).hexdigest() == digest, relative
    for relative, digest in manifest['baseFiles'].items():
        assert relative in allowed
        assert hashlib.sha256((ROOT / relative).read_text().encode()).hexdigest() == digest, relative + ': staging baseline differs'
    production = fingerprint('jubianbian')
    previous = inspect(PROJECT)
    old_env = dict(line.split('=', 1) for line in previous['Config']['Env'] if '=' in line)
    old_build = old_env.get('JBB_BUILD_VERSION', 'unknown')
    idle()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    base_tag = 'jubianbian-staging-app:user-review-base-' + stamp.lower()
    run(['docker', 'tag', previous['Image'], base_tag])
    dockerfile = BASE / 'Dockerfile.overlay'
    dockerfile.write_text('FROM ' + base_tag + '\nCOPY backend/app /app/backend/app\n'
                          'COPY backend/test_*.py /app/backend/\n'
                          'COPY web-staging /app/web-staging\n')
    run(['docker', 'build', '--network=none', '--pull=false', '-t', IMAGE, '-f', dockerfile, BASE])
    run(['docker', 'run', '--rm', '--network', 'none', '-e', 'JBB_DATA_DIR=/tmp/jbb-validation',
         '-w', '/app/backend', '--entrypoint', 'python', IMAGE, '-m', 'unittest', 'discover', '-s', '.'])
    assert production == fingerprint('jubianbian'), 'Production changed externally; aborting'
    idle()
    backup = BASE / ('backup-staging-' + stamp)
    backup.mkdir()
    targets = files + ['fangzhou.env', 'release-image.yml', '.staging-deploy-hold']
    existed = {}
    for relative in targets:
        source = ROOT / relative
        existed[relative] = source.exists()
        if source.exists():
            target = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    record = dict(commit=manifest['commit'], productionBefore=production,
                  previousImage=previous['Image'], previousBuild=old_build,
                  rollbackImage=base_tag, existed=existed, backup=str(backup))
    (BASE / 'deployment.json').write_text(json.dumps(record, indent=2))
    replaced = False
    try:
        for relative in files:
            target = ROOT / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BASE / relative, target)
        envfile = ROOT / 'fangzhou.env'
        lines = [line for line in envfile.read_text().splitlines() if line.partition('=')[0].strip() != 'JBB_BUILD_VERSION']
        envfile.write_text('\n'.join(lines + ['JBB_BUILD_VERSION=' + BUILD]) + '\n')
        envfile.chmod(0o600)
        (ROOT / 'release-image.yml').write_text('services:\n  app:\n    image: ' + IMAGE + '\n')
        (ROOT / '.staging-deploy-hold').write_text('Pinned user review ' + BUILD + '; commit ' + manifest['commit'] + '; backup ' + str(backup) + '\n')
        compose('config', '--quiet')
        replaced = True
        compose('up', '-d', '--no-build', '--no-deps', 'app')
        record['health'] = healthy(BUILD)
        for relative in ['index.html', 'js/app.js', 'js/api.js', 'js/config.js', 'css/app.css']:
            with urlopen('http://127.0.0.1:18000/' + relative, timeout=5) as response:
                assert hashlib.sha256(response.read()).hexdigest() == manifest['files']['web-staging/' + relative]
        assert production == fingerprint('jubianbian'), 'Production changed externally'
        current_env = dict(line.split('=', 1) for line in inspect(PROJECT)['Config']['Env'] if '=' in line)
        assert {k: v for k, v in current_env.items() if k != 'JBB_BUILD_VERSION'} == {k: v for k, v in old_env.items() if k != 'JBB_BUILD_VERSION'}, 'Unexpected runtime configuration change'
        record['productionAfter'] = fingerprint('jubianbian')
        record['newImage'] = inspect(PROJECT)['Image']
        (BASE / 'deployment.json').write_text(json.dumps(record, indent=2))
        print('STAGING_DEPLOYMENT_VERIFIED', json.dumps(record), flush=True)
    except BaseException:
        for relative in targets:
            source = backup / relative
            target = ROOT / relative
            if existed[relative]:
                shutil.copy2(source, target)
            elif target.exists():
                target.unlink()
        if replaced:
            compose('up', '-d', '--no-build', '--no-deps', 'app', build=old_build)
            healthy(old_build)
        print('STAGING_ROLLED_BACK', str(backup), flush=True)
        raise


if __name__ == '__main__':
    main()
