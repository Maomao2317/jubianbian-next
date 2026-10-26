"""Enable the existing staging Ark account without changing application code.

Run the same pushed revision in staging, then production. Secrets stay on the
server, backups are private, and application recreation requires an idle gate.
"""
import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap

SELF = 'ops/enable-secondary-ark-account.py'
SUPPORT = 'ops/restore-gpu-routing.py'
BASELINE = 'e90d4e1a256abd8865cbe72e212df1cd6cf7f05f'
BUILD = 'ark-cost-control-20261024-r2'
KEY = 'ARK_API_KEY_2'

SMOKE = textwrap.dedent('''
    import json, time
    from app import config, providers
    keys = config.ARK_API_KEYS
    assert len(keys) == 2 and len(set(keys)) == 2
    assert [providers.ark_account_key() for _ in range(4)] == list(keys) * 2
    results = []
    for account, key in enumerate(keys, 1):
        for model in (config.ARK_LITE_MODEL, config.ARK_TURBO_MODEL):
            started = time.monotonic()
            try:
                result = providers.ark_stream_response({
                    'model': model, 'max_output_tokens': 16,
                    'thinking': {'type': 'disabled'},
                    'input': [{'role': 'user', 'content': [
                        {'type': 'input_text', 'text': 'Reply with OK only.'}]}],
                }, key)
                assert result.get('status') == 'completed'
                assert any(c.get('text', '').strip() for item in result.get('output', [])
                           for c in item.get('content', []))
            except Exception as error:
                raise RuntimeError('Ark smoke failed: account=%s model=%s type=%s status=%s' % (
                    account, model, type(error).__name__, getattr(error, 'status_code', None))) from None
            results.append(dict(account=account, model=model, passed=True,
                                seconds=round(time.monotonic()-started, 3),
                                totalTokens=result.get('usage', {}).get('total_tokens')))
    print(json.dumps(dict(distinctAccounts=2, roundRobinVerified=True, calls=results,
                         workerConcurrency=config.WORKER_CONCURRENCY,
                         modelConcurrency=config.SHARED_MODEL_CONCURRENCY)))
''')

GPU = textwrap.dedent('''
    import json, os, urllib.request
    results = {}
    for kind in ('OCR', 'ASR'):
        url = os.environ['JBB_'+kind+'_REMOTE_URL'].rstrip('/') + '/health'
        token = os.environ['JBB_'+kind+'_REMOTE_TOKEN']
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer '+token})
        with urllib.request.urlopen(request, timeout=15) as response:
            value = json.load(response)
        assert value.get('ready') and value.get('asrReady')
        results[kind] = {'ready': True, 'asrReady': True}
    print(json.dumps(results))
''')


def update_env(text, key, value):
    # Keep all unrelated lines; quote only a verified simple API token.
    if not re.fullmatch(r'[A-Za-z0-9_.~-]+', value):
        raise ValueError('Unexpected API token format')
    lines = [line for line in text.splitlines()
             if line.removeprefix('export ').partition('=')[0].strip() != key]
    return '\n'.join(lines + [key + '=' + value]) + '\n'


def override(image, values):
    return {'services': {'app': {'image': image, 'environment': {
        key: value.replace('$', '$$') for key, value in values.items()
    }}}}


def private_write(path, text):
    temporary = path.with_name(path.name + '.secondary-account.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('site', choices=('staging', 'production'))
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    assert re.fullmatch('[0-9a-f]{40}', args.commit)
    support = Path(__file__).with_name(Path(SUPPORT).name)
    spec = importlib.util.spec_from_file_location('routing', support)
    routing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routing)
    git, output = routing.git, routing.output
    assert git('rev-parse', 'FETCH_HEAD').decode().strip() == args.commit
    for relative, path in ((SELF, Path(__file__)), (SUPPORT, support)):
        assert git('show', args.commit+':'+relative) == path.read_bytes()
    subprocess.run(['git', '-c', 'safe.directory='+str(routing.REPO), '-C', routing.REPO,
                    'merge-base', '--is-ancestor', BASELINE, args.commit], check=True)
    assert not git('diff', '--name-only', BASELINE, args.commit, '--',
                   'backend/app', 'web-staging').strip(), 'Application source changed'
    locks = []
    for project in ('jubianbian-staging', 'jubianbian'):
        lock = Path('/run/lock/'+project+'-deploy.lock').open('w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    release = Path('/opt/jbb-secondary-ark-'+args.commit[:12])
    release.mkdir(mode=0o700, exist_ok=True)
    release.chmod(0o700)
    stage = routing.inspect('jubianbian-staging')
    stage_env = routing.environment(stage)
    secondary = stage_env.get(KEY, '')
    assert secondary and secondary != stage_env['ARK_API_KEY'], 'Two distinct keys required'
    manifest = {p: hashlib.sha256(git('show', args.commit+':'+p)).hexdigest()
                for p in git('ls-tree', '-rz', '--name-only', args.commit, '--',
                             'backend/app', 'web-staging').decode().split('\0') if p}

    def verify_source(project):
        code = "import sys,json,hashlib; from pathlib import Path; m=json.load(sys.stdin); assert all((Path('/app')/p).is_file() and hashlib.sha256((Path('/app')/p).read_bytes()).hexdigest()==v for p,v in m.items()); print(len(m))"
        return int(output(['docker', 'exec', '-i', project+'-app-1', 'python', '-c', code],
                          input=json.dumps(manifest).encode()))

    def probe(project, code):
        # Capture provider logging and never echo unfiltered errors or secrets.
        result = subprocess.run(['docker', 'exec', '-i', '-e', 'PYTHONPATH=/app/backend',
                                 project+'-app-1', 'python', '-'], input=code.encode(),
                                capture_output=True, timeout=240)
        if result.returncode:
            message = result.stderr.decode(errors='replace').splitlines()
            safe = next((line for line in reversed(message)
                         if line.startswith('RuntimeError: Ark smoke failed:')), '')
            raise RuntimeError('Container verification failed '+project+' '+safe)
        return json.loads(result.stdout)

    if args.site == 'production':
        staged = json.loads((release/'staging.json').read_text())
        assert staged['verified'] and staged['sourceCommit'] == args.commit
        assert stage['Id'] == staged['container'] and stage['Image'] == staged['image']
        routing.health(18000, BUILD, 'staging')
        verify_source('jubianbian-staging')
    project = 'jubianbian-staging' if args.site == 'staging' else 'jubianbian'
    root = Path('/opt')/project
    port = 18000 if args.site == 'staging' else 8000
    previous = routing.inspect(project)
    old_env = routing.environment(previous)
    assert old_env['JBB_BUILD_VERSION'] == BUILD and old_env['JBB_ENVIRONMENT'] == args.site
    assert old_env['ARK_API_KEY'] == stage_env['ARK_API_KEY'], 'Primary accounts differ'
    assert old_env['ARK_LITE_MODEL'] == stage_env['ARK_LITE_MODEL']
    assert old_env['ARK_TURBO_MODEL'] == stage_env['ARK_TURBO_MODEL']
    assert old_env['ARK_BASE_URL'] == stage_env['ARK_BASE_URL']
    assert not old_env.get(KEY) or old_env[KEY] == secondary, 'Unexpected secondary account'
    routing.health(port, BUILD, args.site)
    verify_source(project)
    probe(project, GPU)
    routing.assert_idle(project)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = release/(args.site+'-'+stamp)
    backup.mkdir(mode=0o700)
    for name in ('fangzhou.env', 'release-image.yml'):
        shutil.copy2(root/name, backup/name)
        (backup/name).chmod(0o600)
    expected = {**old_env, KEY: secondary}
    process_env = {k:v for k,v in os.environ.items() if k not in expected}
    command = ['docker', 'compose', '--env-file', root/'fangzhou.env', '-p', project,
               '-f', root/'docker-compose.yml', '-f', root/'release-image.yml']
    record = dict(sourceCommit=args.commit, runtimeSource=BASELINE, site=args.site,
                  build=BUILD, image=previous['Image'], backup=str(backup), verified=False)
    recordfile = release/(args.site+'.json')
    private_write(recordfile, json.dumps(record, indent=2)+'\n')
    replaced = False
    try:
        private_write(root/'fangzhou.env', update_env((root/'fangzhou.env').read_text(), KEY, secondary))
        private_write(root/'release-image.yml', json.dumps(override(previous['Image'], expected), indent=2)+'\n')
        planned = json.loads(output(command+['config', '--format', 'json'], env=process_env))
        planned_env = {k:str(v).replace('$$', '$')
                       for k,v in planned['services']['app']['environment'].items()}
        assert planned_env == expected and planned['services']['app']['image'] == previous['Image']
        routing.assert_idle(project)
        assert routing.inspect(project)['Id'] == previous['Id'], 'Container changed concurrently'
        replaced = True
        subprocess.run([str(x) for x in command+['up', '-d', '--force-recreate', '--no-build', '--no-deps', 'app']],
                       check=True, env=process_env, capture_output=True, timeout=120)
        routing.health(port, BUILD, args.site)
        current = routing.inspect(project)
        assert current['Image'] == previous['Image']
        assert routing.environment(current) == expected
        assert routing.mounts(current) == routing.mounts(previous)
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings']
        record.update(sourceFilesVerified=verify_source(project), gpuHealth=probe(project, GPU),
                      accounts=probe(project, SMOKE), container=current['Id'], verified=True,
                      imagePreserved=True, mountsPreserved=True, portsPreserved=True,
                      verifiedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
        private_write(recordfile, json.dumps(record, indent=2)+'\n')
        print('SECONDARY_ARK_VERIFIED', json.dumps(record), flush=True)
    except BaseException:
        for name in ('fangzhou.env', 'release-image.yml'):
            private_write(root/name, (backup/name).read_text())
        if replaced:
            try:
                routing.assert_idle(project)
            except RuntimeError:
                record['rollbackDeferredActiveTasks'] = True
                private_write(recordfile, json.dumps(record, indent=2)+'\n')
                raise RuntimeError('Configuration restored; container rollback deferred for active tasks') from None
            subprocess.run([str(x) for x in command+['up', '-d', '--force-recreate', '--no-build', '--no-deps', 'app']],
                           check=True, env=process_env, capture_output=True, timeout=120)
            routing.health(port, BUILD, args.site)
        print('SECONDARY_ARK_ROLLED_BACK', args.site, flush=True)
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Docker/HTTP failures can embed private configuration: expose type only
        # except for our explicitly sanitized verification message.
        detail = str(error) if isinstance(error, RuntimeError) else ''
        print('SECONDARY_ARK_FAILED', type(error).__name__, detail, file=sys.stderr)
        sys.exit(1)
