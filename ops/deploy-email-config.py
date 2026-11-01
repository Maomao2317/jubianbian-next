"""Deploy only the pushed Compose email fix, preserving the live application."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen


REPO = Path('/opt/jubianbian-github-check')
REF = 'refs/heads/codex/repair-batch-content'
MAIL = {'TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY', 'TENCENTCLOUD_REGION',
        'TENCENTCLOUD_SES_ENDPOINT', 'TENCENTCLOUD_SES_FROM_EMAIL',
        'TENCENTCLOUD_SES_FROM_NAME', 'TENCENTCLOUD_SES_TEMPLATE_ID'}


def output(args, **kwargs):
    kwargs.setdefault('timeout', 60)
    return subprocess.check_output([str(x) for x in args], **kwargs)


def git(*args):
    return output(['git', '-c', 'safe.directory='+str(REPO), '-C', REPO, *args])


def inspect(name):
    return json.loads(output(['docker', 'inspect', name]))[0]


def environment(item):
    return dict(line.split('=', 1) for line in item['Config']['Env'])


def mounts(item):
    return sorted((x['Source'], x['Destination'], x['RW']) for x in item['Mounts'])


def idle(container):
    code = ('import sqlite3; c=sqlite3.connect("file:/app/data/jubianbian.sqlite3?mode=ro",uri=True); '
            'print(c.execute("select count(*) from tasks where status in '
            '(\'queued\',\'running\',\'uploading\')").fetchone()[0])')
    assert int(output(['docker', 'exec', container, 'python', '-c', code])) == 0, 'Active tasks; defer deployment'


def health(site, commit):
    domain = 'test.jubianbian.com' if site == 'staging' else 'jubianbian.com'
    for attempt in range(30):
        try:
            with urlopen('https://'+domain+'/api/health', timeout=5) as response:
                result = json.load(response)
            if result.get('build') == commit and result.get('environment') == site and result.get('status') == 'ok':
                return result
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Public health verification failed')


def main():
    site, commit = sys.argv[1:]
    assert site in {'staging', 'production'} and re.fullmatch('[a-f0-9]{40}', commit)
    assert git('rev-parse', 'FETCH_HEAD').decode().strip() == commit, 'Fetch the pushed revision first'
    with urlopen('https://api.github.com/repos/Maomao2317/jubianbian-next/git/ref/'+REF.removeprefix('refs/'), timeout=20) as response:
        assert json.load(response)['object']['sha'] == commit, 'Pushed revision changed'
    project = 'jubianbian-staging' if site == 'staging' else 'jubianbian'
    root = Path('/opt')/project
    container = project+'-app-1'
    lock = Path('/run/lock/'+project+'-deploy.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    base = Path('/opt/jbb-email-config-'+commit[:12])
    base.mkdir(mode=0o700, exist_ok=True)
    if site == 'production':
        staged = json.loads((base/'staging.json').read_text())
        assert staged['commit'] == commit and staged['verified']
    previous = inspect(container)
    before = environment(previous)
    assert before['JBB_ENVIRONMENT'] == site and before['JBB_AUTH_ALLOW_DEV_CODE'] == '0'
    # Verify every tracked application source against the pushed revision.
    files = git('ls-tree', '-r', '--name-only', commit, '--', 'backend/app', 'web-staging').decode().splitlines()
    expected = {p: hashlib.sha256(git('show', commit+':'+p).replace(b'\r\n', b'\n')).hexdigest()
                for p in files}
    code = ('import sys,json,hashlib; from pathlib import Path; data=json.load(sys.stdin); '
            'print(json.dumps([p for p,h in data.items() if '
            'hashlib.sha256((Path("/app")/p).read_bytes().replace(b"\\r\\n",b"\\n")).hexdigest()!=h]))')
    differing = json.loads(output(['docker', 'exec', '-i', container, 'python', '-c', code],
                                 input=json.dumps(expected).encode()))
    assert not differing, 'Live application differs from pushed source: '+str(differing)
    source = 'docker-compose.staging.yml' if site == 'staging' else 'docker-compose.yml'
    candidate = git('show', commit+':'+source)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = base/(site+'-'+stamp)
    backup.mkdir(mode=0o700)
    paths = ['docker-compose.yml', 'fangzhou.env', 'release-image.yml']
    existed = {p: (root/p).exists() for p in paths}
    for p in paths:
        if existed[p]:
            shutil.copy2(root/p, backup/p)
    idle(container)
    # Pin every existing interpolation input to its live value. Do not change
    # worker limits, provider routing, credentials, storage, or listener ports.
    runenv = {k: v for k, v in os.environ.items() if not k.startswith('TENCENTCLOUD_')}
    for key in re.findall(rb'\$\{([A-Z][A-Z0-9_]*)', candidate):
        key = key.decode()
        if key in before:
            runenv[key] = before[key]
    runenv['JBB_BUILD_VERSION'] = commit
    replaced = False
    try:
        (root/'docker-compose.yml').write_bytes(candidate)
        (root/'release-image.yml').write_text(json.dumps({'services': {'app': {'image': previous['Image']}}}))
        command = ['docker', 'compose', '--env-file', str(root/'fangzhou.env'), '-p', project,
                   '-f', str(root/'docker-compose.yml'), '-f', str(root/'release-image.yml')]
        plan = json.loads(output(command+['config', '--format', 'json'], env=runenv))
        app = plan['services']['app']
        defaults = environment(json.loads(output(['docker', 'image', 'inspect', previous['Image']]))[0])
        planned = dict(defaults, **app['environment'])
        changes = sorted(k for k in before.keys() | planned.keys() if before.get(k) != planned.get(k))
        assert set(changes) <= MAIL | {'JBB_BUILD_VERSION'}, 'Unexpected changed keys: '+str(changes)
        for key in ['TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY', 'TENCENTCLOUD_SES_FROM_EMAIL']:
            assert planned.get(key), 'Missing mail configuration: '+key
        assert int(planned.get('TENCENTCLOUD_SES_TEMPLATE_ID', '0')) > 0
        planned_mounts = sorted((plan['volumes'][v['source']]['name'], v['target'], not v.get('read_only', False))
                                for v in app['volumes'])
        actual_mounts = sorted((v['Name'], v['Destination'], v['RW']) for v in previous['Mounts'])
        assert planned_mounts == actual_mounts, 'Data volumes would change'
        planned_ports = sorted((str(p['target'])+'/tcp', p.get('host_ip', ''), str(p['published'])) for p in app.get('ports', []))
        old_ports = sorted((k, p.get('HostIp', ''), p['HostPort'])
                           for k, values in previous['HostConfig']['PortBindings'].items() for p in values)
        assert planned_ports == old_ports, 'Port bindings would change'
        idle(container)
        replaced = True
        subprocess.run(command+['up', '-d', '--no-build', '--no-deps', 'app'], env=runenv, check=True)
        public = health(site, commit)
        current = inspect(container)
        assert environment(current) == planned and mounts(current) == mounts(previous)
        assert current['Image'] == previous['Image']
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings']
        # Make the verified revision survive the next ordinary compose run.
        envfile = root/'fangzhou.env'
        lines = [line for line in envfile.read_text().splitlines() if line.partition('=')[0].strip() != 'JBB_BUILD_VERSION']
        envfile.write_text('\n'.join(lines+['JBB_BUILD_VERSION='+commit])+'\n')
        envfile.chmod(0o600)
        (root/'.deployed_commit').write_text(commit+'\n')
        record = dict(commit=commit, site=site, verified=True, health=public, changedKeys=changes,
                      appSourceFilesVerified=len(expected), imageUnchanged=True, volumesPreserved=True,
                      nonMailEnvironmentPreserved=True, backup=str(backup))
        (base/(site+'.json')).write_text(json.dumps(record, indent=2))
        print(json.dumps(record, indent=2), flush=True)
    except BaseException:
        for p in paths:
            if existed[p]:
                shutil.copy2(backup/p, root/p)
            elif (root/p).exists():
                (root/p).unlink()
        if replaced:
            # The exact previous Compose files are restored; pin the original
            # live environment as a private rollback override as well.
            rollback = backup/'rollback.json'
            rollback.write_text(json.dumps({'services': {'app': {'image': previous['Image'], 'environment': before}}}))
            rollback.chmod(0o600)
            subprocess.run(['docker', 'compose', '-p', project, '-f', str(root/'docker-compose.yml'),
                            '-f', str(rollback), 'up', '-d', '--no-build', '--no-deps', 'app'], check=True)
            health(site, before['JBB_BUILD_VERSION'])
        raise


if __name__ == '__main__':
    main()
