"""Offline scene regression checks, then the same image to staging/production.

Run as root inside a manifest-verified release bundle from a published commit.
Business databases are mounted read-only for replay. No provider is called.
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
BUILD = 'scene-consolidation-20261018-r1'
ALLOWED = {'backend/app/script.py', 'backend/app/content_coverage.py',
           'backend/test_scene_consolidation.py', 'web-staging/index.html',
           'web-staging/js/app.js', 'web-staging/test-user-review.cjs'}
TARGETS = [('jubianbian-staging', 18000, 'staging'), ('jubianbian', 8000, 'production')]


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def inspect(project):
    return json.loads(subprocess.check_output(['docker', 'inspect', project + '-app-1']))[0]


def environment(item):
    return dict(line.split('=', 1) for line in item['Config']['Env'] if '=' in line)


def mounts(item):
    return sorted((x['Source'], x['Destination'], x['RW']) for x in item['Mounts'])


def idle(project):
    code = "import sqlite3; c=sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro',uri=True); print(c.execute(\"select count(*) from tasks where status in ('queued','running','uploading')\").fetchone()[0])"
    count = int(subprocess.check_output(['docker', 'exec', project + '-app-1', 'python', '-c', code]))
    if count:
        raise RuntimeError(f'{count} active tasks in {project}; stop before replacing its container')


def compose(project, root, build, *args, override=None):
    command = ['docker', 'compose', '--env-file', root / 'fangzhou.env', '-p', project,
               '-f', root / 'docker-compose.yml', '-f', root / 'release-image.yml']
    if override:
        command += ['-f', override]
    run(command + list(args), env=dict(os.environ, JBB_BUILD_VERSION=build))


def health(port, build, site):
    for _ in range(30):
        try:
            with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3) as response:
                result = json.load(response)
            if result.get('status') == 'ok' and result.get('build') == build and result.get('environment') == site:
                return result
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f'Health verification failed: {site}')


def baseline(root, manifest):
    for relative, digest in manifest['baseFiles'].items():
        assert relative in ALLOWED
        assert hashlib.sha256((root / relative).read_text().encode()).hexdigest() == digest, relative + ': unexpected baseline'


def deploy(project, port, site, image, manifest):
    root = Path('/opt/' + project)
    baseline(root, manifest)
    idle(project)
    previous = inspect(project)
    old_env = environment(previous)
    assert old_env['JBB_ENVIRONMENT'] == site
    old_build = old_env['JBB_BUILD_VERSION']
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = BASE / ('backup-' + site + '-' + stamp)
    backup.mkdir(mode=0o700)
    rollback = project + '-app:scene-before-' + stamp.lower()
    run(['docker', 'tag', previous['Image'], rollback])
    targets = sorted(ALLOWED) + ['fangzhou.env', 'release-image.yml', '.staging-deploy-hold']
    existed = {}
    for relative in targets:
        source = root / relative
        existed[relative] = source.exists()
        if source.exists():
            target = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    rollback_override = backup / 'rollback-compose.yml'
    rollback_override.write_text('services:\n  app:\n    image: ' + rollback + '\n')
    record = dict(commit=manifest['commit'], build=BUILD, image=image, backup=str(backup),
                  previousImage=previous['Image'], previousBuild=old_build, rollbackImage=rollback,
                  existed=existed)
    recordfile = BASE / (site + '-deployment.json')
    recordfile.write_text(json.dumps(record, indent=2))
    replaced = False
    try:
        for relative in sorted(ALLOWED):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BASE / relative, target)
        envfile = root / 'fangzhou.env'
        lines = [line for line in envfile.read_text().splitlines() if line.partition('=')[0].strip() != 'JBB_BUILD_VERSION']
        envfile.write_text('\n'.join(lines + ['JBB_BUILD_VERSION=' + BUILD]) + '\n')
        envfile.chmod(0o600)
        (root / 'release-image.yml').write_text('services:\n  app:\n    image: ' + image + '\n')
        (root / '.staging-deploy-hold').write_text('Pinned scene release ' + BUILD + '; commit ' + manifest['commit'] + '; backup ' + str(backup) + '\n')
        compose(project, root, BUILD, 'config', '--quiet')
        idle(project)
        replaced = True
        compose(project, root, BUILD, 'up', '-d', '--no-build', '--no-deps', 'app')
        record['health'] = health(port, BUILD, site)
        current = inspect(project)
        assert current['Image'] == image
        assert {k: v for k, v in environment(current).items() if k != 'JBB_BUILD_VERSION'} == {k: v for k, v in old_env.items() if k != 'JBB_BUILD_VERSION'}, 'Runtime environment changed'
        assert mounts(current) == mounts(previous), 'Data mounts changed'
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings'], 'Port bindings changed'
        for relative in ['index.html', 'js/app.js']:
            with urlopen(f'http://127.0.0.1:{port}/' + relative, timeout=5) as response:
                assert hashlib.sha256(response.read()).hexdigest() == manifest['files']['web-staging/' + relative], relative
        record.update(configurationPreserved=True, dataMountsPreserved=True)
        recordfile.write_text(json.dumps(record, indent=2))
        print(site.upper() + '_DEPLOYMENT_VERIFIED', json.dumps(record), flush=True)
    except BaseException:
        for relative in targets:
            target = root / relative
            if existed[relative]:
                shutil.copy2(backup / relative, target)
            elif target.exists():
                target.unlink()
        if replaced:
            compose(project, root, old_build, 'up', '-d', '--no-build', '--no-deps', 'app', override=rollback_override)
            health(port, old_build, site)
        print(site.upper() + '_ROLLED_BACK', str(backup), flush=True)
        raise


def main():
    locks = []
    for project, _, _ in TARGETS:
        lock = Path('/run/lock/' + project + '-deploy.lock').open('w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    manifest = json.loads((BASE / 'manifest.json').read_text())
    assert set(manifest['files']) == ALLOWED
    for relative, digest in manifest['files'].items():
        assert hashlib.sha256((BASE / relative).read_bytes()).hexdigest() == digest, relative
    for project, _, _ in TARGETS:
        baseline(Path('/opt/' + project), manifest)
        idle(project)
    base_image = inspect('jubianbian-staging')['Image']
    assert base_image == inspect('jubianbian')['Image'], 'Environment base images differ'
    base_tag = 'jubianbian-release:scene-base-20261018-r1'
    run(['docker', 'tag', base_image, base_tag])
    tag = 'jubianbian-release:' + BUILD
    dockerfile = BASE / 'Dockerfile.overlay'
    dockerfile.write_text('FROM ' + base_tag + '\nCOPY backend/app /app/backend/app\n'
                         'COPY backend/test_*.py /app/backend/\nCOPY web-staging /app/web-staging\n'
                         'COPY ops/verify-scene-consolidation.py /app/verify-scene-consolidation.py\n')
    run(['docker', 'build', '--network=none', '--pull=false', '-t', tag, '-f', dockerfile, BASE])
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag]))[0]['Id']
    with (BASE / 'offline-tests.log').open('w') as log:
        tests = subprocess.run(['docker', 'run', '--rm', '--network=none', '-e', 'JBB_DATA_DIR=/tmp/jbb-validation',
                    '-w', '/app/backend', '--entrypoint', 'python', image, '-m', 'unittest', 'discover', '-s', '.'],
                    stdout=log, stderr=subprocess.STDOUT)
    print('\n'.join((BASE / 'offline-tests.log').read_text().splitlines()[-5:]), flush=True)
    tests.check_returncode()
    # Read production data only, using no runtime credentials and no network.
    replay = subprocess.check_output(['docker', 'run', '--rm', '--network=none',
             '--volumes-from', 'jubianbian-app-1:ro',
             '-v', '/opt/jubianbian/backend/app/script.py:/baseline_script.py:ro',
             '-e', 'JBB_DATA_DIR=/tmp/jbb-replay', '-e', 'PYTHONPATH=/app/backend',
             '-w', '/app/backend', '--entrypoint', 'python', image, '/app/verify-scene-consolidation.py'])
    (BASE / 'replay.json').write_bytes(replay)
    print('PRODUCTION_READONLY_REPLAY', replay.decode().strip(), flush=True)
    for project, port, site in TARGETS:
        deploy(project, port, site, image, manifest)
    assert inspect('jubianbian')['Image'] == inspect('jubianbian-staging')['Image'] == image
    print('BOTH_ENVIRONMENTS_VERIFIED', BUILD, manifest['commit'], image, flush=True)


if __name__ == '__main__':
    main()
