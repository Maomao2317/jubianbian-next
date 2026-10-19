"""Build and replay offline; deploy idle only after verifying pushed Git source."""
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
BUILD = 'repair-recovery-20261019-r1'
ALLOWED = {'backend/app/config.py', 'backend/app/content_coverage.py',
           'backend/app/processing.py', 'backend/app/providers.py',
           'backend/app/recognition_cache.py', 'backend/app/script.py',
           'backend/app/task_store.py', 'backend/test_scene_consolidation.py',
           'backend/test_script_quality.py', 'web-staging/js/app.js', 'web-staging/js/config.js'}
TARGETS = [('jubianbian-staging', 18000, 'staging'), ('jubianbian', 8000, 'production')]


def run(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def inspect(project):
    return json.loads(subprocess.check_output(['docker', 'inspect', project+'-app-1']))[0]


def environment(item):
    return dict(line.split('=', 1) for line in item['Config']['Env'] if '=' in line)


def mounts(item):
    return sorted((x['Source'], x['Destination'], x['RW']) for x in item['Mounts'])


def idle(project):
    code = "import sqlite3; c=sqlite3.connect('file:/app/data/jubianbian.sqlite3?mode=ro',uri=True); print(c.execute(\"select count(*) from tasks where status in ('queued','running','uploading')\").fetchone()[0])"
    count = int(subprocess.check_output(['docker','exec',project+'-app-1','python','-c',code]))
    if count:
        raise RuntimeError(f'{count} active tasks in {project}; deployment deferred')


def compose(project, root, build, *args, override=None):
    command = ['docker','compose','--env-file',root/'fangzhou.env','-p',project,
               '-f',root/'docker-compose.yml','-f',root/'release-image.yml']
    if override:
        command += ['-f', override]
    run(command+list(args), env=dict(os.environ,JBB_BUILD_VERSION=build))


def health(port, build, site):
    for _ in range(30):
        try:
            with urlopen(f'http://127.0.0.1:{port}/api/health',timeout=3) as r:
                data = json.load(r)
            if data.get('status') == 'ok' and data.get('build') == build and data.get('environment') == site:
                return data
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Health verification failed: '+site)


def baseline(root, manifest):
    for path, digest in manifest['baseFiles'].items():
        assert path in ALLOWED
        assert hashlib.sha256((root/path).read_text(encoding='utf-8').encode()).hexdigest() == digest, path+': unexpected baseline'


def published_source(manifest):
    source = json.loads((BASE/'release-source.json').read_text())
    commit, ref = source['commit'], source['ref']
    assert re.fullmatch(r'[0-9a-f]{40}', commit), 'Invalid source commit'
    assert ref.startswith('refs/heads/codex/') and '..' not in ref, 'Invalid release ref'
    repo = Path('/opt/jubianbian-github-check')
    git = ['git', '-c', 'safe.directory='+str(repo), '-c', 'http.version=HTTP/1.1',
           '-c', 'http.lowSpeedLimit=1', '-c', 'http.lowSpeedTime=30', '-C', str(repo)]
    env = {k:v for k,v in os.environ.items() if k.lower() not in
           {'http_proxy', 'https_proxy', 'all_proxy'}}
    remote = subprocess.check_output(git+['remote', 'get-url', 'origin'], env=env).decode().strip()
    assert remote == 'https://github.com/Maomao2317/jubianbian-next.git', 'Unexpected release repository'
    for attempt in range(3):
        try:
            run(git+['fetch', '--no-tags', 'origin', ref], env=env, timeout=90)
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == 2:
                raise
            print('PUBLISHED_GIT_FETCH_RETRY', attempt+1, flush=True)
    fetched = subprocess.check_output(git+['rev-parse', 'FETCH_HEAD'], env=env).decode().strip()
    assert fetched == commit, 'Pushed Git revision does not match release'
    run(git+['merge-base', '--is-ancestor', manifest['baseCommit'], commit], env=env)
    for path in sorted(ALLOWED | {'ops/deploy-repair-recovery.py', 'ops/verify-repair-recovery.py'}):
        blob = subprocess.check_output(git+['show', commit+':'+path], env=env).decode('utf-8').replace('\r\n','\n').encode()
        assert blob == (BASE/path).read_text(encoding='utf-8').encode(), path+': differs from pushed source'
    print('PUBLISHED_GIT_SOURCE_VERIFIED', commit, ref, flush=True)
    return source


def deploy(project,port,site,image,manifest,source):
    root = Path('/opt/'+project)
    baseline(root,manifest)
    idle(project)
    previous = inspect(project)
    old_env = environment(previous)
    assert old_env['JBB_ENVIRONMENT'] == site
    old_build = old_env['JBB_BUILD_VERSION']
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = BASE/('backup-'+site+'-'+stamp)
    backup.mkdir(mode=0o700)
    rollback = project+'-app:repair-before-'+stamp.lower()
    run(['docker','tag',previous['Image'],rollback])
    paths = sorted(ALLOWED)+['fangzhou.env','release-image.yml','.staging-deploy-hold']
    existed = {}
    for path in paths:
        original = root/path
        existed[path] = original.exists()
        if original.exists():
            target = backup/path
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(original,target)
    rollback_override = backup/'rollback-compose.yml'
    rollback_override.write_text('services:\n  app:\n    image: '+rollback+'\n')
    record = dict(baseCommit=manifest['baseCommit'],patch=manifest['patch'],build=BUILD,image=image,
                  sourceCommit=source['commit'],sourceRef=source['ref'],
                  backup=str(backup),previousImage=previous['Image'],previousBuild=old_build,
                  rollbackImage=rollback,existed=existed)
    recordfile = BASE/(site+'-deployment.json')
    recordfile.write_text(json.dumps(record,indent=2))
    replaced = False
    try:
        for path in sorted(ALLOWED):
            target = root/path
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(BASE/path,target)
        envfile = root/'fangzhou.env'
        lines = [s for s in envfile.read_text().splitlines() if s.partition('=')[0].strip() != 'JBB_BUILD_VERSION']
        envfile.write_text('\n'.join(lines+['JBB_BUILD_VERSION='+BUILD])+'\n')
        envfile.chmod(0o600)
        (root/'release-image.yml').write_text('services:\n  app:\n    image: '+image+'\n')
        (root/'.staging-deploy-hold').write_text('Pinned '+BUILD+'; patch '+manifest['patch']+'; backup '+str(backup)+'\n')
        compose(project,root,BUILD,'config','--quiet')
        idle(project)
        # The ordinary compose command uses this default tag without overrides.
        run(['docker','tag',image,project+'-app:latest'])
        replaced = True
        compose(project,root,BUILD,'up','-d','--no-build','--no-deps','app')
        record['health'] = health(port,BUILD,site)
        current = inspect(project)
        assert current['Image'] == image
        assert {k:v for k,v in environment(current).items() if k != 'JBB_BUILD_VERSION'} == {k:v for k,v in old_env.items() if k != 'JBB_BUILD_VERSION'}, 'Runtime environment changed'
        assert mounts(current) == mounts(previous), 'Data mounts changed'
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings'], 'Port bindings changed'
        for path in ['js/app.js','js/config.js']:
            with urlopen(f'http://127.0.0.1:{port}/'+path,timeout=5) as response:
                assert hashlib.sha256(response.read()).hexdigest() == manifest['files']['web-staging/'+path], path
        default = json.loads(subprocess.check_output(['docker','image','inspect',project+'-app:latest']))[0]['Id']
        assert default == image, 'Ordinary compose image not updated'
        record.update(configurationPreserved=True,dataMountsPreserved=True,ordinaryComposeVerified=True)
        recordfile.write_text(json.dumps(record,indent=2))
        print(site.upper()+'_DEPLOYMENT_VERIFIED',json.dumps(record),flush=True)
    except BaseException:
        for path in paths:
            target = root/path
            if existed[path]:
                shutil.copy2(backup/path,target)
            elif target.exists():
                target.unlink()
        if replaced:
            run(['docker','tag',previous['Image'],project+'-app:latest'])
            compose(project,root,old_build,'up','-d','--no-build','--no-deps','app',override=rollback_override)
            health(port,old_build,site)
        print(site.upper()+'_ROLLED_BACK',str(backup),flush=True)
        raise


def main():
    locks=[]
    for project,_,_ in TARGETS:
        lock=Path('/run/lock/'+project+'-deploy.lock').open('w')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        locks.append(lock)
    manifest=json.loads((BASE/'manifest.json').read_text())
    assert set(manifest['files']) == ALLOWED
    for path,digest in manifest['files'].items():
        assert hashlib.sha256((BASE/path).read_bytes()).hexdigest() == digest,path
    if sys.argv[-1] != 'deploy':
        for project,_,_ in TARGETS:
            baseline(Path('/opt/'+project),manifest)
        base_image=inspect('jubianbian-staging')['Image']
        assert base_image == inspect('jubianbian')['Image'],'Environment base images differ'
        base_tag='jubianbian-release:repair-base-20261019'
        run(['docker','tag',base_image,base_tag])
        dockerfile=BASE/'Dockerfile.overlay'
        lines=['FROM '+base_tag]
        for path in sorted(ALLOWED):
            lines.append('COPY '+path+' /app/'+path)
        lines.append('COPY ops/verify-repair-recovery.py /app/verify-repair-recovery.py')
        dockerfile.write_text('\n'.join(lines)+'\n')
        tag='jubianbian-release:'+BUILD
        run(['docker','build','--network=none','--pull=false','-t',tag,'-f',dockerfile,BASE])
        image=json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]['Id']
        with (BASE/'offline-tests.log').open('w') as log:
            tests=subprocess.run(['docker','run','--rm','--network=none','-e','JBB_DATA_DIR=/tmp/jbb-validation',
                    '-w','/app/backend','--entrypoint','python',image,'-m','unittest','discover','-s','.'],stdout=log,stderr=subprocess.STDOUT)
        print('\n'.join((BASE/'offline-tests.log').read_text().splitlines()[-8:]),flush=True)
        tests.check_returncode()
        for project,_,site in TARGETS:
            replay=subprocess.run(['docker','run','--rm','--network=none','--volumes-from',project+'-app-1:ro',
                    '-v','/opt/'+project+'/backend/app/script.py:/baseline_script.py:ro',
                    '-e','JBB_DATA_DIR=/tmp/jbb-replay','-e','PYTHONPATH=/app/backend','-w','/app/backend',
                    '--entrypoint','python',image,'/app/verify-repair-recovery.py',site],capture_output=True)
            (BASE/(site+'-replay.log')).write_bytes(replay.stdout+replay.stderr)
            print(site.upper()+'_REPLAY',replay.stdout.decode().strip(),replay.stderr.decode().strip(),flush=True)
            replay.check_returncode()
        (BASE/'verified-image.json').write_text(json.dumps(dict(image=image,patch=manifest['patch'],build=BUILD)))
        print('READY_TO_DEPLOY',image,BUILD,flush=True)
        return
    verified=json.loads((BASE/'verified-image.json').read_text())
    assert verified['patch'] == manifest['patch'] and verified['build'] == BUILD
    image=verified['image']
    source=published_source(manifest)
    for project,port,site in TARGETS:
        deploy(project,port,site,image,manifest,source)
    assert inspect('jubianbian')['Image'] == inspect('jubianbian-staging')['Image'] == image
    print('BOTH_ENVIRONMENTS_VERIFIED',BUILD,image,flush=True)


if __name__ == '__main__':
    main()
