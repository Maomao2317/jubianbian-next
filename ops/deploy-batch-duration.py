"""Deploy the pushed batch-duration UI, staging first, preserving runtime settings."""
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

BASELINE = 'c2a0cd6e0a57ab4ebaebf17491bbac422d4d87af'
BUILD = 'batch-duration-20261026-r1'
SELF = 'ops/deploy-batch-duration.py'
SUPPORT = 'ops/restore-gpu-routing.py'
CHANGES = {'web-staging/js/app.js', 'web-staging/index.html',
           'web-staging/test-compression-progress.cjs'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=('prepare', 'staging', 'production'))
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    assert re.fullmatch(r'[0-9a-f]{40}', args.commit)
    spec = importlib.util.spec_from_file_location('routing', Path(__file__).with_name(Path(SUPPORT).name))
    routing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routing)
    git, output = routing.git, routing.output
    assert git('remote', 'get-url', 'origin').decode().strip() == 'https://github.com/Maomao2317/jubianbian-next.git'
    assert git('rev-parse', 'FETCH_HEAD').decode().strip() == args.commit
    subprocess.run(['git', '-c', 'safe.directory='+str(routing.REPO), '-C', routing.REPO,
                    'merge-base', '--is-ancestor', BASELINE, args.commit], check=True)
    for relative in (SELF, SUPPORT):
        assert git('show', args.commit+':'+relative) == Path(__file__).with_name(Path(relative).name).read_bytes()
    changed = set(git('diff', '--name-only', BASELINE, args.commit).decode().splitlines())
    assert changed == CHANGES | {SELF}, 'Unexpected release scope'
    locks = []
    for project in ('jubianbian-staging', 'jubianbian'):
        lock = Path('/run/lock/'+project+'-deploy.lock').open('w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    release = Path('/opt/jbb-batch-duration-'+args.commit[:12])
    release.mkdir(mode=0o700, exist_ok=True)
    release.chmod(0o700)
    paths = [p for p in git('ls-tree', '-rz', '--name-only', args.commit, '--',
                           'backend/app', 'web-staging').decode().split('\0') if p]
    manifest = {p: hashlib.sha256(git('show', args.commit+':'+p)).hexdigest() for p in paths}
    check = "import sys,json,hashlib; from pathlib import Path; m=json.load(sys.stdin); bad=[p for p,v in m.items() if hashlib.sha256((Path('/app')/p).read_bytes()).hexdigest()!=v]; assert not bad,repr(bad); print('SOURCE_VERIFIED',len(m))"

    def verify(project, hashes):
        return output(['docker', 'exec', '-i', project+'-app-1', 'python', '-c', check],
                      input=json.dumps(hashes).encode()).decode().strip()

    prepared_file = release/'prepared.json'
    if args.step == 'prepare':
        stage, prod = routing.inspect('jubianbian-staging'), routing.inspect('jubianbian')
        assert stage['Image'] == prod['Image'], 'Different deployed baselines'
        base_manifest = {p: hashlib.sha256(git('show', BASELINE+':'+p)).hexdigest() for p in paths}
        for project in ('jubianbian-staging', 'jubianbian'):
            print(project, verify(project, base_manifest), flush=True)
        context = release/'context'
        context.mkdir(exist_ok=True)
        for relative in sorted(CHANGES):
            target = context/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git('show', args.commit+':'+relative))
        base_tag = 'jubianbian-app:duration-base-'+args.commit[:12]
        subprocess.run(['docker', 'tag', prod['Image'], base_tag], check=True)
        (context/'Dockerfile').write_text('FROM '+base_tag+'\n'+''.join(
            'COPY '+p+' /app/'+p+'\n' for p in sorted(CHANGES)))
        tag = 'jubianbian-app:duration-'+args.commit[:12]
        subprocess.run(['docker', 'build', '--pull=false', '--network=none', '-t', tag, context], check=True)
        image = json.loads(output(['docker', 'image', 'inspect', tag]))[0]['Id']
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '-i',
                        '--entrypoint', 'python', image, '-c', check],
                       input=json.dumps(manifest).encode(), check=True)
        prepared_file.write_text(json.dumps(dict(sourceCommit=args.commit, image=image,
                                                baseImage=prod['Image'], files=manifest), indent=2))
        print('PREPARED', args.commit, image, flush=True)
        return
    prepared = json.loads(prepared_file.read_text())
    assert prepared['sourceCommit'] == args.commit and prepared['files'] == manifest
    if args.step == 'production':
        staged = json.loads((release/'staging.json').read_text())
        assert staged['verified'] and staged['sourceCommit'] == args.commit
        assert staged['image'] == prepared['image'] == routing.inspect('jubianbian-staging')['Image']
        routing.health(18000, BUILD, 'staging')
        verify('jubianbian-staging', manifest)
    project = 'jubianbian-staging' if args.step == 'staging' else 'jubianbian'
    root, port = Path('/opt')/project, 18000 if args.step == 'staging' else 8000
    previous = routing.inspect(project)
    assert previous['Image'] == prepared['baseImage'], 'Deployment baseline changed'
    old_env = routing.environment(previous)
    old_build = old_env['JBB_BUILD_VERSION']
    assert old_env['JBB_ENVIRONMENT'] == args.step
    routing.health(port, old_build, args.step)
    routing.assert_idle(project)
    backup = release/(args.step+'-backup')
    backup.mkdir(mode=0o700)
    targets = sorted(CHANGES)+['fangzhou.env', 'release-image.yml', '.staging-deploy-hold']
    existed = {}
    for relative in targets:
        source = root/relative
        existed[relative] = source.exists()
        if source.exists():
            target = backup/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    rollback = backup/'rollback-image.yml'
    rollback.write_text('services:\n  app:\n    image: '+previous['Image']+'\n')
    record = dict(sourceCommit=args.commit, build=BUILD, site=args.step,
                  image=prepared['image'], previousImage=previous['Image'],
                  previousBuild=old_build, backup=str(backup), verified=False)
    recordfile = release/(args.step+'.json')
    recordfile.write_text(json.dumps(record, indent=2))
    process_env = {k:v for k,v in os.environ.items() if k not in old_env}
    process_env['JBB_BUILD_VERSION'] = BUILD
    command = ['docker', 'compose', '--env-file', root/'fangzhou.env', '-p', project,
               '-f', root/'docker-compose.yml', '-f', root/'release-image.yml']
    replaced = False
    try:
        for relative in sorted(CHANGES):
            target = root/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git('show', args.commit+':'+relative))
        envfile = root/'fangzhou.env'
        lines = [s for s in envfile.read_text().splitlines()
                 if s.removeprefix('export ').partition('=')[0].strip() != 'JBB_BUILD_VERSION']
        envfile.write_text('\n'.join(lines+['JBB_BUILD_VERSION='+BUILD])+'\n')
        envfile.chmod(0o600)
        pinned = dict(old_env, JBB_BUILD_VERSION=BUILD)
        override = root/'release-image.yml'
        override.touch(mode=0o600, exist_ok=True)
        override.chmod(0o600)
        override.write_text(json.dumps({'services': {'app': {'image': prepared['image'],
            'environment': {k:v.replace('$', '$$') for k,v in pinned.items()}}}}, indent=2)+'\n')
        (root/'.staging-deploy-hold').write_text('Pinned '+BUILD+'; source '+args.commit+'\n')
        planned = json.loads(output(command+['config', '--format', 'json'], env=process_env))['services']['app']
        assert {k:str(v).replace('$$', '$') for k,v in planned['environment'].items()} == pinned
        assert planned['image'] == prepared['image']
        routing.assert_idle(project)
        assert routing.inspect(project)['Id'] == previous['Id']
        replaced = True
        subprocess.run([str(x) for x in command+['up', '-d', '--no-build', '--no-deps', 'app']],
                       check=True, env=process_env)
        record['health'] = routing.health(port, BUILD, args.step)
        current = routing.inspect(project)
        assert current['Image'] == prepared['image']
        assert routing.environment(current) == pinned
        assert routing.mounts(current) == routing.mounts(previous)
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings']
        print(project, verify(project, manifest), flush=True)
        for relative, digest in manifest.items():
            if relative.startswith('web-staging/') and not relative.endswith('.cjs'):
                with urlopen('http://127.0.0.1:'+str(port)+'/'+quote(relative.removeprefix('web-staging/'), safe='/'), timeout=5) as response:
                    assert hashlib.sha256(response.read()).hexdigest() == digest, relative
        record.update(verified=True, configurationPreserved=True, dataMountsPreserved=True,
                      verifiedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
        recordfile.write_text(json.dumps(record, indent=2))
        print(args.step.upper()+'_VERIFIED', json.dumps(record), flush=True)
    except BaseException:
        for relative in targets:
            if existed[relative]:
                shutil.copy2(backup/relative, root/relative)
            else:
                (root/relative).unlink(missing_ok=True)
        if replaced:
            routing.assert_idle(project)
            subprocess.run([str(x) for x in command+['-f', rollback, 'up', '-d', '--no-build', '--no-deps', 'app']],
                           check=True, env=dict(process_env, JBB_BUILD_VERSION=old_build))
            routing.health(port, old_build, args.step)
        print(args.step.upper()+'_ROLLED_BACK', str(backup), flush=True)
        raise


if __name__ == '__main__':
    main()
