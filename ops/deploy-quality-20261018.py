"""Pinned two-environment overlay. Run with sudo from an extracted release.

Validates offline before replacing services; backs up sources/env and exact
images. Never edits business data or invokes a recognition provider.
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
BUILD = "quality-gpu-20261018-r2"
IMAGE = "jubianbian-release:" + BUILD
SITES = [(Path('/opt/jubianbian-staging'), 'jubianbian-staging', 18000),
         (Path('/opt/jubianbian'), 'jubianbian', 8000)]
FILES = [str(p.relative_to(BASE)) for folder, pattern in
         [('backend/app', '*.py'), ('backend', 'test_*.py'), ('ops', '*.py')]
         for p in (BASE / folder).glob(pattern)]
FILES += ['web-staging/index.html', 'web-staging/js/app.js', 'ops/deploy-staging.sh',
          'ops/gpu-ocr-deployment.md']


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def inspect(project):
    return json.loads(subprocess.check_output(['docker', 'inspect', project + '-app-1']))[0]


def assert_idle(project):
    code = "import sqlite3; c=sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro',uri=True); print(c.execute(\"select count(*) from tasks where status in ('queued','running','uploading')\").fetchone()[0])"
    active = int(subprocess.check_output(['docker', 'exec', project + '-app-1', 'python', '-c', code]))
    if active:
        raise RuntimeError(f'{project}: {active} active tasks; aborting deployment')


def compose(root, project, *args):
    run(['docker', 'compose', '--env-file', root / 'fangzhou.env', '-p', project,
         '-f', root / 'docker-compose.yml', '-f', root / 'release-image.yml', *args],
        env=dict(os.environ, JBB_BUILD_VERSION=BUILD))


def health(port):
    for _ in range(30):
        try:
            with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3) as response:
                value = json.load(response)
            if value.get('status') == 'ok' and value.get('build') == BUILD:
                return value
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f'health/build check failed on {port}')


def main():
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    tokenfile = Path('/home/ubuntu/jbb-gpu-20261018.env')
    secrets = dict(line.split('=', 1) for line in tokenfile.read_text().splitlines() if '=' in line)
    assert len(secrets['JBB_GPU_OCR_TOKEN']) >= 24
    locks = []
    for project in ('jubianbian', 'jubianbian-staging'):
        lock = Path('/run/lock/' + project + '-deploy.lock').open('w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    for _, project, _ in SITES:
        assert_idle(project)
    records = []
    try:
        for root, project, port in SITES:
            backup = BASE / ('backup-' + project + '-' + stamp)
            backup.mkdir()
            targets = FILES + ['docker-compose.yml', 'fangzhou.env', 'release-image.yml', '.staging-deploy-hold']
            existed = {}
            for relative in targets:
                source = root / relative
                existed[relative] = source.exists()
                if source.exists():
                    target = backup / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            old = inspect(project)
            rollback = project + '-app:before-' + stamp.lower()
            run(['docker', 'tag', old['Image'], rollback])
            record = dict(root=str(root), project=project, port=port, backup=str(backup),
                          rollback=rollback, existed=existed, previousImage=old['Image'])
            records.append(record)
            for relative in FILES:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(BASE / relative, target)
            shutil.copy2(BASE / ('docker-compose.staging.yml' if project.endswith('-staging') else 'docker-compose.yml'), root / 'docker-compose.yml')
            gateway = '172.19.0.1' if project.endswith('-staging') else '172.18.0.1'
            values = dict(JBB_BUILD_VERSION=BUILD, JBB_UPLOAD_RESERVATION_TTL_SECONDS='21600',
                          JBB_OCR_REMOTE_URL=f'http://{gateway}:18867',
                          JBB_ASR_REMOTE_URL=f'http://{gateway}:18867',
                          JBB_OCR_REMOTE_TOKEN=secrets['JBB_GPU_OCR_TOKEN'],
                          JBB_ASR_REMOTE_TOKEN=secrets['JBB_GPU_OCR_TOKEN'])
            # Match verified staging throughput settings, never credentials,
            # domains, storage prefixes, ports or data volumes.
            source_env = dict(x.split('=', 1) for x in inspect('jubianbian-staging')['Config']['Env'] if '=' in x)
            for key, value in source_env.items():
                if key.startswith(('JBB_WORKER_', 'JBB_ARK_', 'JBB_EVIDENCE_', 'JBB_DOWNLOAD_',
                                   'JBB_LOCAL_ASR_', 'JBB_OCR_PROCESS_', 'JBB_SHARED_MODEL_', 'ARK_')):
                    if not any(part in key for part in ('KEY', 'TOKEN', 'URL', 'DEVICE', 'COMPUTE_TYPE')):
                        values[key] = value
            envfile = root / 'fangzhou.env'
            lines = [line for line in envfile.read_text().splitlines() if line.partition('=')[0].strip() not in values]
            envfile.write_text('\n'.join(lines + [k+'='+v for k,v in values.items()]) + '\n')
            envfile.chmod(0o600)
            (root / 'release-image.yml').write_text('services:\n  app:\n    image: '+IMAGE+'\n')
            (root / '.staging-deploy-hold').write_text('Pinned verified overlay '+BUILD+'; backup '+str(backup)+'\n')
            compose(root, project, 'config', '--quiet')
            if len(records) == 1:
                compose(root, project, 'build', 'app')
                run(['docker', 'run', '--rm', '--network', 'none', '-e', 'JBB_DATA_DIR=/tmp/jbb-validation',
                     '-w', '/app/backend', '--entrypoint', 'python', IMAGE,
                     '-m', 'unittest', 'discover', '-s', '.'])
            assert_idle(project)
            compose(root, project, 'up', '-d', '--no-build', '--no-deps', 'app')
            record['health'] = health(port)
            record['image'] = inspect(project)['Image']
            print('ENVIRONMENT_HEALTHY', project, json.dumps(record['health']), flush=True)
        assert len({r['image'] for r in records}) == 1
        print('BOTH_ENVIRONMENTS_ALIGNED', BUILD, flush=True)
    except Exception:
        for record in reversed(records):
            root = Path(record['root'])
            backup = Path(record['backup'])
            for relative, present in record['existed'].items():
                target = root / relative
                if present:
                    shutil.copy2(backup / relative, target)
                else:
                    target.unlink(missing_ok=True)
            override = backup / 'rollback-compose.yml'
            override.write_text('services:\n  app:\n    image: '+record['rollback']+'\n')
            run(['docker', 'compose', '--env-file', root / 'fangzhou.env', '-p', record['project'],
                 '-f', root / 'docker-compose.yml', '-f', override, 'up', '-d', '--no-build', '--no-deps', 'app'])
        raise
    finally:
        (BASE / 'deployment.json').write_text(json.dumps(records, indent=2))


if __name__ == '__main__':
    main()
