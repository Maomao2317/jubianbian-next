"""Restore GPU routing only, preserving each deployed application's exact image.

Run staging first, then production, from the same fetched/pushed Git revision.
Refuse active tasks, validate real GPU OCR/ASR and CPU fallback, and roll back
configuration on failure. Credentials and customer data stay on the server.
"""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
from urllib.request import urlopen

REPO = Path('/opt/jubianbian-github-check')
RECORDS = Path('/opt/jubianbian-gpu-restore-20261019')
SELF = 'ops/restore-gpu-routing.py'
VERIFIER = 'ops/verify_gpu_asr.py'
GPU_KEYS = {f'JBB_{kind}_REMOTE_{field}' for kind in ('ASR', 'OCR')
            for field in ('URL', 'TOKEN', 'TIMEOUT_SECONDS')}


def output(args, **kwargs):
    return subprocess.check_output([str(x) for x in args], **kwargs)


def git(*args):
    return output(['git', '-c', 'safe.directory='+str(REPO), '-C', REPO, *args])


def inspect(project):
    return json.loads(output(['docker', 'inspect', project+'-app-1']))[0]


def environment(item):
    return dict(line.split('=', 1) for line in item['Config']['Env'])


def mounts(item):
    return sorted((x['Source'], x['Destination'], x['RW']) for x in item['Mounts'])


def assert_idle(project):
    code = "import sqlite3; c=sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro',uri=True); print(c.execute(\"select count(*) from tasks where status in ('queued','running','uploading')\").fetchone()[0])"
    count = int(output(['docker', 'exec', project+'-app-1', 'python', '-c', code]))
    if count:
        raise RuntimeError(f'{project}: {count} active tasks; retry after they finish')


def health(port, build, site):
    for _ in range(30):
        try:
            with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3) as response:
                value = json.load(response)
            if value.get('status') == 'ok' and value.get('build') == build and value.get('environment') == site:
                return value
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Application health/build verification failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('site', choices=('staging', 'production'))
    parser.add_argument('--commit', required=True)
    parser.add_argument('--video', required=True, help='Existing server-local smoke video')
    parser.add_argument('--duration', type=float, required=True)
    args = parser.parse_args()
    assert re.fullmatch(r'[0-9a-f]{40}', args.commit)
    assert args.video.startswith('/') and Path(args.video).is_file() and args.duration > 0
    assert git('rev-parse', 'FETCH_HEAD').decode().strip() == args.commit
    assert git('show', args.commit+':'+SELF).replace(b'\r\n', b'\n') == Path(__file__).read_bytes().replace(b'\r\n', b'\n')
    verifier = git('show', args.commit+':'+VERIFIER)
    RECORDS.mkdir(mode=0o700, exist_ok=True)
    RECORDS.chmod(0o700)
    if args.site == 'production':
        staged = json.loads((RECORDS/'staging.json').read_text())
        assert staged['sourceCommit'] == args.commit and staged['verified']
    project = 'jubianbian-staging' if args.site == 'staging' else 'jubianbian'
    root = Path('/opt')/project
    port = 18000 if args.site == 'staging' else 8000
    lock = Path('/run/lock/'+project+'-deploy.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert_idle(project)
    previous = inspect(project)
    old_env = environment(previous)
    build = old_env['JBB_BUILD_VERSION']
    assert old_env['JBB_ENVIRONMENT'] == args.site
    health(port, build, args.site)
    gateways = {v['Gateway'] for v in previous['NetworkSettings']['Networks'].values()}
    assert len(gateways) == 1 and next(iter(gateways)) in {'172.18.0.1', '172.19.0.1'}
    url = 'http://'+next(iter(gateways))+':18867'
    secretfile = Path('/home/ubuntu/jbb-gpu-20261018.env')
    assert not secretfile.stat().st_mode & 0o077
    secrets = {}
    for line in secretfile.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            key, value = line.removeprefix('export ').split('=', 1)
            secrets[key.strip()] = shlex.split(value)[0]
    token = secrets['JBB_GPU_OCR_TOKEN']
    assert len(token) >= 24 and re.fullmatch(r'[A-Za-z0-9_.~-]+', token)
    values = {f'JBB_{kind}_REMOTE_{field}': value
              for kind, timeout in [('ASR', '60'), ('OCR', '90')]
              for field, value in [('URL', url), ('TOKEN', token), ('TIMEOUT_SECONDS', timeout)]}
    # Validate the authenticated Docker-to-GPU path before changing anything.
    check = "import sys,json,urllib.request; d=json.load(sys.stdin); r=urllib.request.Request(d['url']+'/health',headers={'Authorization':'Bearer '+d['token']}); h=json.load(urllib.request.urlopen(r,timeout=10)); assert h['ready'] and h['asrReady']; assert h['provider']=='paddleocr-gpu' and h['asrProvider']=='faster-whisper-gpu'; print(json.dumps(h))"
    gpu_health = json.loads(output(['docker', 'exec', '-i', project+'-app-1', 'python', '-c', check],
                                   input=json.dumps(dict(url=url, token=token)).encode()))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = RECORDS/(args.site+'-'+stamp)
    backup.mkdir(mode=0o700)
    envfile = root/'fangzhou.env'
    shutil.copy2(envfile, backup/'fangzhou.env')
    (backup/'fangzhou.env').chmod(0o600)
    override = backup/'image.yml'
    override.write_text('services:\n  app:\n    image: '+previous['Image']+'\n')
    record = dict(sourceCommit=args.commit, site=args.site, build=build,
                  image=previous['Image'], previousContainer=previous['Id'],
                  backup=str(backup), gpuHealth=gpu_health, verified=False)
    recordfile = RECORDS/(args.site+'.json')
    recordfile.write_text(json.dumps(record, indent=2))
    command = ['docker', 'compose', '--env-file', envfile, '-p', project,
               '-f', root/'docker-compose.yml', '-f', override]
    process_env = {k:v for k,v in os.environ.items() if k not in GPU_KEYS}
    process_env['JBB_BUILD_VERSION'] = build
    replaced = False
    try:
        lines = [line for line in envfile.read_text().splitlines()
                 if line.removeprefix('export ').partition('=')[0].strip() not in values]
        envfile.write_text('\n'.join(lines+[k+'='+v for k,v in values.items()])+'\n')
        envfile.chmod(0o600)
        planned = json.loads(output(command+['config', '--format', 'json'], env=process_env))
        planned_env = {k:str(v) for k,v in planned['services']['app']['environment'].items()}
        assert all(planned_env.get(k) == v for k,v in values.items()), 'Compose omits GPU settings'
        # Docker injects PATH etc. from the image; compare Compose-managed keys.
        assert all(old_env.get(k) == v for k,v in planned_env.items() if k not in GPU_KEYS), 'Unrelated runtime settings would change'
        assert_idle(project)
        replaced = True
        subprocess.run([str(x) for x in command+['up', '-d', '--no-build', '--no-deps', 'app']],
                       check=True, env=process_env)
        record['health'] = health(port, build, args.site)
        current = inspect(project)
        assert current['Image'] == previous['Image']
        assert mounts(current) == mounts(previous), 'Data mounts changed'
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings']
        assert {k:v for k,v in environment(current).items() if k not in GPU_KEYS} == {k:v for k,v in old_env.items() if k not in GPU_KEYS}
        assert all(environment(current).get(k) == v for k,v in values.items())
        # No business writes or paid API calls: reuse a local sample and execute
        # the published verifier in a separate process inside the real app.
        smoke = '/tmp/gpu-restore-verification.json'
        sample = '/tmp/gpu-restore-sample.mp4'
        output(['docker', 'cp', args.video, project+'-app-1:'+sample])
        with (backup/'verification.log').open('wb') as log:
            subprocess.run(['docker', 'exec', '-i', '-e', 'OPENAI_API_KEY=',
                            '-e', 'ARK_API_KEY=', '-e', 'ARK_API_KEY_2=',
                            '-e', 'PYTHONPATH=/app/backend', project+'-app-1',
                            'python', '-', sample, '--duration', str(args.duration),
                            '--output', smoke], input=verifier, stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=600)
        record['smoke'] = json.loads(output(['docker', 'exec', project+'-app-1', 'cat', smoke]))
        record.update(verified=True, dataMountsPreserved=True, applicationImagePreserved=True,
                      container=current['Id'], verifiedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
        recordfile.write_text(json.dumps(record, indent=2))
        print('GPU_ROUTING_VERIFIED', json.dumps(record), flush=True)
    except BaseException:
        shutil.copy2(backup/'fangzhou.env', envfile)
        if replaced:
            # Refuse to kill any customer task that arrived during validation.
            try:
                assert_idle(project)
            except RuntimeError:
                record.update(rollbackDeferred=True, configurationFileRestored=True)
                recordfile.write_text(json.dumps(record, indent=2))
                print('GPU_ROLLBACK_DEFERRED_ACTIVE_TASKS', args.site, str(backup), flush=True)
                raise
            subprocess.run([str(x) for x in command+['up', '-d', '--no-build', '--no-deps', 'app']],
                           check=True, env=process_env)
            health(port, build, args.site)
        print('GPU_ROUTING_ROLLED_BACK', args.site, str(backup), flush=True)
        raise


if __name__ == '__main__':
    main()
