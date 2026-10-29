"""Release the pushed GLM trial to idle staging; never promote production."""
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
PARENT = 'd2daf99575f708702cf5811242973d8e8f347e75'
FILES = {'backend/app/config.py', 'backend/app/logging_setup.py', 'backend/app/providers.py',
         'backend/app/processing.py', 'backend/app/evidence.py', 'backend/app/glm_provider.py', 'backend/test_glm_provider.py'}
SUPPORT = {'ops/deploy-glm-trial.py', 'ops/verify-glm-trial.py'}


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def git(*args):
    return subprocess.check_output(['git', '-c', 'safe.directory=' + str(REPO), '-C', str(REPO), *args])


def inspect(name=PROJECT):
    return json.loads(subprocess.check_output(['docker', 'inspect', name + '-app-1']))[0]


def fingerprint():
    item = inspect('jubianbian')
    return [item['Id'], item['Image'], item['State']['StartedAt']]


def environment(item):
    return dict(line.split('=', 1) for line in item['Config']['Env'])


def idle():
    code = "import sqlite3; c=sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro',uri=True); print(c.execute(\"select count(*) from tasks where status in ('queued','running','uploading')\").fetchone()[0])"
    count = int(subprocess.check_output(['docker', 'exec', PROJECT + '-app-1', 'python', '-c', code]))
    if count:
        raise RuntimeError(f'{count} active staging tasks; deployment deferred')


def compose(build, *args):
    run('docker', 'compose', '--env-file', ROOT / 'fangzhou.env', '-p', PROJECT,
        '-f', ROOT / 'docker-compose.yml', '-f', ROOT / 'release-image.yml', *args,
        env=dict(os.environ, JBB_BUILD_VERSION=build))


def health(build):
    for _ in range(30):
        try:
            with urlopen('http://127.0.0.1:18000/api/health', timeout=3) as response:
                result = json.load(response)
            if result.get('status') == 'ok' and result.get('environment') == 'staging' and result.get('build') == build:
                return result
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Staging health/build check failed')


def main(commit):
    assert re.fullmatch('[0-9a-f]{40}', commit)
    lock = Path('/run/lock/jubianbian-staging-deploy.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert git('ls-remote', 'origin', 'refs/heads/codex/repair-batch-content').decode().split()[0] == commit, 'Push first'
    assert set(git('diff', '--name-only', PARENT, commit).decode().splitlines()) == FILES | SUPPORT
    digests = {}
    for name in FILES | SUPPORT:
        data = (BASE / name).read_bytes()
        assert data == git('show', commit + ':' + name), name + ': not pushed source'
        digests[name] = hashlib.sha256(data).hexdigest()
    production = fingerprint()
    previous = inspect()
    old_env = environment(previous)
    assert old_env['JBB_ENVIRONMENT'] == 'staging'
    assert not old_env.get('JBB_VIDEO_PROVIDER') or old_env['JBB_VIDEO_PROVIDER'] == 'ark'
    keyfile = ROOT / 'glm-trial.env'
    assert keyfile.is_file() and keyfile.stat().st_mode & 0o077 == 0, 'Private GLM configuration required'
    assert any(line.startswith('GLM_API_KEY=') and len(line.partition('=')[2]) > 20 for line in keyfile.read_text().splitlines())
    # Validate the actual running baseline, not an obsolete host checkout.
    for name in FILES:
        if git('ls-tree', PARENT, '--', name).strip():
            active = subprocess.check_output(['docker', 'exec', PROJECT + '-app-1', 'cat', '/app/' + name])
            assert active.replace(b'\r\n', b'\n') == git('show', PARENT + ':' + name).replace(b'\r\n', b'\n'), name + ': baseline changed'
    idle()
    build = 'glm-trial-' + commit[:12]
    image = 'jubianbian-glm-trial:' + commit[:12]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    rollback = 'jubianbian-staging-rollback:glm-before-' + stamp.lower()
    run('docker', 'tag', previous['Image'], rollback)
    dockerfile = BASE / 'Dockerfile.glm-overlay'
    dockerfile.write_text('FROM ' + rollback + '\nCOPY backend/app /app/backend/app\nCOPY backend/test_*.py /app/backend/\nCOPY ops/verify-glm-trial.py /app/ops/verify-glm-trial.py\n')
    run('docker', 'build', '--network=none', '--pull=false', '-t', image, '-f', dockerfile, BASE)
    with (BASE / 'backend-tests.log').open('w') as output:
        run('docker', 'run', '--rm', '--network=none', '-e', 'JBB_DATA_DIR=/tmp/jbb-validation',
            '-w', '/app/backend', '--entrypoint', 'python', image, '-m', 'unittest', 'discover', '-s', '.', stdout=output, stderr=subprocess.STDOUT)
    print('ISOLATED_BACKEND_TESTS_PASSED', flush=True)
    assert fingerprint() == production
    idle()
    backup = BASE / ('backup-staging-' + stamp)
    backup.mkdir(mode=0o700)
    targets = sorted(FILES) + ['fangzhou.env', 'release-image.yml', '.staging-deploy-hold']
    existed = {}
    for name in targets:
        original = ROOT / name
        existed[name] = original.exists()
        if original.exists():
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
    record = dict(commit=commit, build=build, files=digests, backup=str(backup),
                  previousImage=previous['Image'], rollbackImage=rollback,
                  productionBefore=production, tests='backend-tests.log')
    receipt = BASE / 'staging-deployment.json'
    receipt.write_text(json.dumps(record, indent=2))
    replaced = False
    try:
        for name in FILES:
            target = ROOT / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BASE / name, target)
        envfile = ROOT / 'fangzhou.env'
        lines = [line for line in envfile.read_text().splitlines() if line.partition('=')[0].strip() != 'JBB_BUILD_VERSION']
        envfile.write_text('\n'.join(lines + ['JBB_BUILD_VERSION=' + build]) + '\n')
        envfile.chmod(0o600)
        settings = {'JBB_VIDEO_PROVIDER': 'glm', 'GLM_MODEL': 'glm-5.3-flash', 'GLM_REASONING_EFFORT': 'max',
                    'GLM_MAX_OUTPUT_TOKENS': '131072', 'JBB_GLM_MAX_IN_FLIGHT': '1',
                    'GLM_BASE_URL': 'https://open.bigmodel.cn/api/paas/v4',
                    'GLM_RESPONSE_TIMEOUT_SECONDS': '1200', 'GLM_STREAM_IDLE_TIMEOUT_SECONDS': '180',
                    'GLM_RESPONSE_ATTEMPTS': '2'}
        # Retain all Ark/OpenAI and GPU settings; selection explicitly isolates GLM.
        override = 'services:\n  app:\n    image: ' + image + '\n    env_file:\n      - ' + str(keyfile) + '\n    environment:\n'
        override += ''.join('      ' + k + ': ' + json.dumps(v) + '\n' for k, v in settings.items())
        (ROOT / 'release-image.yml').write_text(override)
        (ROOT / '.staging-deploy-hold').write_text('Pinned ' + build + '; pushed commit ' + commit + '; backup ' + str(backup) + '\n')
        compose(build, 'config', '--quiet')
        replaced = True
        compose(build, 'up', '-d', '--no-build', '--no-deps', 'app')
        record['health'] = health(build)
        current = inspect()
        current_env = environment(current)
        permitted = set(settings) | {'GLM_API_KEY', 'JBB_BUILD_VERSION'}
        assert {k: v for k, v in old_env.items() if k not in permitted} == {k: v for k, v in current_env.items() if k not in permitted}, 'Other settings changed'
        assert all(current_env[k] == v for k, v in settings.items())
        assert current_env.get('GLM_API_KEY')
        assert current['Mounts'] == previous['Mounts'], 'Data mounts changed'
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings']
        for name in FILES:
            actual = subprocess.check_output(['docker', 'exec', PROJECT + '-app-1', 'cat', '/app/' + name])
            assert hashlib.sha256(actual).hexdigest() == digests[name], name + ': runtime differs'
        run('docker', 'exec', PROJECT + '-app-1', 'python', '/app/ops/verify-glm-trial.py')
        proof = subprocess.check_output(['docker', 'exec', PROJECT + '-app-1', 'cat', '/tmp/glm-trial-verification.json'])
        record['liveVerification'] = json.loads(proof)
        assert fingerprint() == production, 'Production changed externally'
        record.update(productionAfter=fingerprint(), newImage=current['Image'], settings=settings,
                      arkConfigurationPreserved=True, dataMountsPreserved=True,
                      verifiedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
        receipt.write_text(json.dumps(record, indent=2))
        print('GLM_STAGING_VERIFIED', json.dumps(record), flush=True)
    except BaseException:
        for name in targets:
            if existed[name]:
                shutil.copy2(backup / name, ROOT / name)
            elif (ROOT / name).exists():
                (ROOT / name).unlink()
        if replaced:
            compose(old_env['JBB_BUILD_VERSION'], 'up', '-d', '--no-build', '--no-deps', 'app')
            health(old_env['JBB_BUILD_VERSION'])
        print('GLM_STAGING_ROLLED_BACK', flush=True)
        raise


if __name__ == '__main__':
    main(sys.argv[1])
