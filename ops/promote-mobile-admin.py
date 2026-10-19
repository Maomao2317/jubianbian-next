"""Promote the exact verified mobile staging image, preserving production GPU/data.

Run only after this tool is pushed and fetched. No image rebuild, customer task
creation, balance changes or staging restart. Keep private rollback materials.
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
from urllib.parse import quote
from urllib.request import urlopen

SOURCE = '81d53abb40c108fa6662d57ecaf1023913fa1266'
BUILD = 'mobile-admin-20261019-r1'
BASELINE = 'a3f8bb6'
ROOT = Path('/opt/jubianbian')
STAGED = Path('/opt/jbb-mobile-admin-wvkpifm3/staging-deployment.json')
SELF = 'ops/promote-mobile-admin.py'
SUPPORT = 'ops/restore-gpu-routing.py'
FILES = {'backend/app/routes.py', 'backend/app/user_view.py', 'backend/test_user_review.py',
         'web-staging/css/app.css', 'web-staging/index.html', 'web-staging/js/app.js',
         'web-staging/js/config.js', 'web-staging/test-user-review.cjs'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool-commit', required=True)
    args = parser.parse_args()
    assert re.fullmatch(r'[0-9a-f]{40}', args.tool_commit)
    support = Path(__file__).with_name(Path(SUPPORT).name)
    spec = importlib.util.spec_from_file_location('routing', support)
    routing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routing)
    assert routing.git('rev-parse', 'FETCH_HEAD').decode().strip() == args.tool_commit
    for relative, path in [(SELF, Path(__file__)), (SUPPORT, support)]:
        assert routing.git('show', args.tool_commit+':'+relative) == path.read_bytes()
    subprocess.run(['git', '-c', 'safe.directory='+str(routing.REPO), '-C', routing.REPO,
                    'merge-base', '--is-ancestor', SOURCE, args.tool_commit], check=True)
    changed = set(routing.git('diff', '--name-only', BASELINE, SOURCE, '--',
                             'backend', 'web-staging').decode().splitlines())
    assert changed == FILES, 'Unexpected application release scope'
    locks = []
    for project in ['jubianbian', 'jubianbian-staging']:
        lock = Path('/run/lock/'+project+'-deploy.lock').open('w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    staged = json.loads(STAGED.read_text())
    assert staged['commit'] == SOURCE and staged['build'] == BUILD
    assert staged['configurationPreserved'] and staged['dataMountsPreserved']
    stage = routing.inspect('jubianbian-staging')
    assert stage['Image'] == staged['newImage']
    routing.health(18000, BUILD, 'staging')
    paths = [p for p in routing.git('ls-tree', '-rz', '--name-only', SOURCE, '--',
                                    'backend/app', 'web-staging').decode().split('\0') if p]
    manifest = {p: hashlib.sha256(routing.git('show', SOURCE+':'+p)).hexdigest() for p in paths}
    check = "import sys,json,hashlib; from pathlib import Path; m=json.load(sys.stdin); bad=[p for p,v in m.items() if not (Path('/app')/p).is_file() or hashlib.sha256((Path('/app')/p).read_bytes()).hexdigest()!=v]; assert not bad, 'source mismatch: '+repr(bad); print('APPLICATION_SOURCE_VERIFIED',len(m))"
    def verify_files(project):
        return routing.output(['docker', 'exec', '-i', project+'-app-1', 'python', '-c', check],
                              input=json.dumps(manifest).encode()).decode().strip()
    print('STAGING', verify_files('jubianbian-staging'), flush=True)
    previous = routing.inspect('jubianbian')
    old_env = routing.environment(previous)
    old_build = old_env['JBB_BUILD_VERSION']
    assert old_env['JBB_ENVIRONMENT'] == 'production' and old_build == 'repair-recovery-20261019-r1'
    routing.health(8000, old_build, 'production')
    routing.assert_idle('jubianbian')
    for path in FILES:
        exists = bool(routing.git('ls-tree', BASELINE, '--', path).strip())
        if exists:
            assert (ROOT/path).read_text() == routing.git('show', BASELINE+':'+path).decode().replace('\r\n', '\n'), path+': production source changed'
        else:
            assert not (ROOT/path).exists(), path+': unexpected production file'
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path('/opt/jbb-mobile-admin-production-'+stamp)
    backup.mkdir(mode=0o700)
    with (backup/'backend-tests.log').open('wb') as log:
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '-e',
                        'JBB_DATA_DIR=/tmp/jbb-validation', '-w', '/app/backend',
                        '--entrypoint', 'python', stage['Image'], '-m', 'unittest',
                        'discover', '-s', '.'], check=True, timeout=180,
                       stdout=log, stderr=subprocess.STDOUT)
    print('OFFLINE_BACKEND_TESTS_PASSED', str(backup), flush=True)
    targets = sorted(FILES)+['fangzhou.env', 'release-image.yml', '.staging-deploy-hold']
    existed = {}
    for path in targets:
        source = ROOT/path
        existed[path] = source.exists()
        if source.exists():
            dest = backup/path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
            if path == 'fangzhou.env':
                dest.chmod(0o600)
    rollback = backup/'rollback-compose.yml'
    rollback.write_text('services:\n  app:\n    image: '+previous['Image']+'\n')
    process_env = {k:v for k,v in os.environ.items() if k not in old_env}
    process_env['JBB_BUILD_VERSION'] = BUILD
    command = ['docker', 'compose', '--env-file', ROOT/'fangzhou.env', '-p', 'jubianbian',
               '-f', ROOT/'docker-compose.yml', '-f', ROOT/'release-image.yml']
    record = dict(sourceCommit=SOURCE, toolCommit=args.tool_commit, build=BUILD,
                  previousBuild=old_build, previousImage=previous['Image'],
                  image=stage['Image'], backup=str(backup), files=manifest, verified=False)
    recordfile = backup/'production-deployment.json'
    recordfile.write_text(json.dumps(record, indent=2))
    replaced = False
    try:
        for path in FILES:
            dest = ROOT/path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(routing.git('show', SOURCE+':'+path))
        envfile = ROOT/'fangzhou.env'
        lines = [x for x in envfile.read_text().splitlines()
                 if x.removeprefix('export ').partition('=')[0].strip() != 'JBB_BUILD_VERSION']
        envfile.write_text('\n'.join(lines+['JBB_BUILD_VERSION='+BUILD])+'\n')
        envfile.chmod(0o600)
        (ROOT/'release-image.yml').write_text('services:\n  app:\n    image: '+stage['Image']+'\n')
        (ROOT/'.staging-deploy-hold').write_text('Pinned '+BUILD+'; source '+SOURCE+'; backup '+str(backup)+'\n')
        planned = json.loads(routing.output(command+['config', '--format', 'json'], env=process_env))
        planned_env = {k:str(v) for k,v in planned['services']['app']['environment'].items()}
        assert all(old_env.get(k) == v for k,v in planned_env.items() if k != 'JBB_BUILD_VERSION'), 'Production configuration would change'
        assert planned_env['JBB_BUILD_VERSION'] == BUILD
        assert planned['services']['app']['image'] == stage['Image']
        assert routing.inspect('jubianbian-staging')['Id'] == stage['Id']
        routing.assert_idle('jubianbian')
        assert routing.inspect('jubianbian')['Id'] == previous['Id']
        replaced = True
        subprocess.run(command+['up', '-d', '--no-build', '--no-deps', 'app'],
                       check=True, env=process_env)
        record['health'] = routing.health(8000, BUILD, 'production')
        current = routing.inspect('jubianbian')
        assert current['Image'] == stage['Image']
        assert routing.mounts(current) == routing.mounts(previous)
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings']
        assert {k:v for k,v in routing.environment(current).items() if k != 'JBB_BUILD_VERSION'} == {k:v for k,v in old_env.items() if k != 'JBB_BUILD_VERSION'}
        print('PRODUCTION', verify_files('jubianbian'), flush=True)
        for relative, digest in manifest.items():
            if relative.startswith('web-staging/') and not relative.endswith('.cjs'):
                path = quote(relative.removeprefix('web-staging/'), safe='/')
                with urlopen('http://127.0.0.1:8000/'+path, timeout=5) as response:
                    assert hashlib.sha256(response.read()).hexdigest() == digest, relative
        gpu = "import os,json,urllib.request; u=os.environ['JBB_OCR_REMOTE_URL']; assert os.environ['JBB_ASR_REMOTE_URL']==u; r=urllib.request.Request(u+'/health',headers={'Authorization':'Bearer '+os.environ['JBB_OCR_REMOTE_TOKEN']}); h=json.load(urllib.request.urlopen(r,timeout=10)); assert h['ready'] and h['asrReady']; print(json.dumps(h))"
        record['gpuHealth'] = json.loads(routing.output(['docker', 'exec', 'jubianbian-app-1', 'python', '-c', gpu]))
        assert routing.inspect('jubianbian-staging')['Id'] == stage['Id']
        record.update(verified=True, configurationPreserved=True, dataMountsPreserved=True,
                      stagingUnchanged=True, sameImageAsStaging=True,
                      verifiedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
        recordfile.write_text(json.dumps(record, indent=2))
        print('PRODUCTION_MOBILE_VERIFIED', json.dumps({k:v for k,v in record.items() if k != 'files'}), flush=True)
    except BaseException:
        for path in targets:
            if existed[path]:
                shutil.copy2(backup/path, ROOT/path)
            else:
                (ROOT/path).unlink(missing_ok=True)
        if replaced:
            try:
                routing.assert_idle('jubianbian')
            except RuntimeError:
                record.update(rollbackDeferred=True, configurationFileRestored=True)
                recordfile.write_text(json.dumps(record, indent=2))
                print('ROLLBACK_DEFERRED_ACTIVE_TASKS', str(backup), flush=True)
                raise
            subprocess.run(command+['-f', rollback, 'up', '-d', '--no-build', '--no-deps', 'app'],
                           check=True, env=dict(process_env, JBB_BUILD_VERSION=old_build))
            routing.health(8000, old_build, 'production')
        print('PRODUCTION_MOBILE_ROLLED_BACK', str(backup), flush=True)
        raise


if __name__ == '__main__':
    main()
