"""Resume the verified release in Seoul without discarding Beijing writes.

Credentials are supplied through stdin; archives are encrypted in transit.
Invoke prepare/fetch-build before freeze/fetch-restore, then start staging,
start production, activate Seoul, and forward Beijing. DNS is changed last.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from urllib.request import urlopen

ROOT = Path('/opt/jbb-return-seoul-20261019')
RELEASE = Path('/opt/jbb-repair-20261019-r1')
VOLUMES = ['jubianbian_jbb-data', 'jubianbian-staging_jbb-staging-data']
PROJECTS = ['jubianbian', 'jubianbian-staging']
BUILD = 'repair-recovery-20261019-r1'
BASE_IMAGE = 'sha256:7ad7fda761470885bee19e56c0055c0ed9564866bec78f84ed7d0bf524331952'
SUFFIX = '-seoul-20261019r2'


def run(*args, **kw):
    return subprocess.run([str(x) for x in args], check=True, **kw)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(4194304), b''):
            h.update(b)
    return h.hexdigest()


def files(root):
    return {str(p.relative_to(root)): {'sha256': digest(p), 'size': p.stat().st_size}
            for p in sorted(root.rglob('*')) if p.is_file()}


def volume(name):
    return Path('/var/lib/docker/volumes') / name / '_data'


def caddy(text):
    path = Path('/opt/jubianbian/Caddyfile')
    path.write_text(text)
    run('docker', 'cp', path, 'jubianbian-proxy-1:/tmp/return-seoul-Caddyfile')
    run('docker', 'exec', 'jubianbian-proxy-1', 'caddy', 'validate', '--config',
        '/tmp/return-seoul-Caddyfile', '--adapter', 'caddyfile')
    run('docker', 'exec', 'jubianbian-proxy-1', 'caddy', 'reload', '--config',
        '/tmp/return-seoul-Caddyfile', '--adapter', 'caddyfile')


def seal(name, password):
    target = ROOT/'serve'/(name+'.enc')
    run('openssl', 'enc', '-aes-256-cbc', '-salt', '-pbkdf2', '-iter', '200000',
        '-in', ROOT/(name+'.tar.gz'), '-out', target, '-pass', 'stdin', input=password.encode())
    info = {'size': target.stat().st_size, 'sha256': digest(target)}
    (ROOT/'serve'/(name+'.json')).write_text(json.dumps(info))
    print('SEALED', name, json.dumps(info), flush=True)


def fetch(name, token, password):
    # Encrypted archives also work when the source hostname's TLS is blocked.
    url = 'http://82.156.205.129/_jbb_return_'+token+'/'+name
    curl = ['curl', '--fail', '--silent', '--show-error']
    info = json.loads(subprocess.check_output(curl+['--max-time', '30', url+'.json']))
    enc = ROOT/(name+'.enc')
    run(*curl, '--retry', '3', '-C', '-', '-o', enc, url+'.enc')
    assert enc.stat().st_size == info['size'] and digest(enc) == info['sha256']
    run('openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2', '-iter', '200000',
        '-in', enc, '-out', ROOT/(name+'.tar.gz'), '-pass', 'stdin', input=password.encode())
    target = ROOT/name
    target.mkdir()
    with tarfile.open(ROOT/(name+'.tar.gz')) as archive:
        archive.extractall(target, filter='data')
    return target


def prepare(token, password):
    (ROOT/'serve').mkdir(mode=0o700)
    bundle = ROOT/'config'
    bundle.mkdir()
    for project in PROJECTS:
        shutil.copytree(Path('/opt')/project, bundle/project)
    release = bundle/'release'
    release.mkdir()
    manifest = json.loads((RELEASE/'manifest.json').read_text())
    for name in list(manifest['files'])+['manifest.json', 'release-source.json',
                                          'verified-image.json', 'ops/verify-repair-recovery.py']:
        target = release/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RELEASE/name, target)
    assert all(digest(release/p) == h for p, h in manifest['files'].items())
    original = Path('/opt/jubianbian/Caddyfile').read_text()
    (ROOT/'Caddyfile.original').write_text(original)
    with tarfile.open(ROOT/'config.tar.gz', 'w:gz', compresslevel=1) as archive:
        for p in bundle.iterdir():
            archive.add(p, arcname=p.name)
    seal('config', password)
    route = ('\n    handle /_jbb_return_'+token+'/* {\n        uri strip_prefix /_jbb_return_'+token+
             '\n        reverse_proxy 172.18.0.1:19020\n    }\n')
    blocks = []
    for block in original.split('\n\n'):
        if '{' in block and 'reverse_proxy' in block:
            opening, body = block.split('{', 1)
            blocks.append(opening+'{'+route+'    handle {'+body.rsplit('}', 1)[0]+'    }\n}')
        else:
            blocks.append(block)
    run('systemd-run', '--unit=jbb-return-transfer', '--property=RuntimeMaxSec=7200',
        '/usr/bin/python3', '-m', 'http.server', '19020', '--bind', '172.18.0.1',
        '--directory', ROOT/'serve')
    caddy('\n\n'.join(blocks))
    print('CONFIG_READY_LIVE_SERVICE_UNCHANGED', flush=True)


def build(token, password):
    bundle = ROOT/'config'
    if not bundle.exists():
        bundle = fetch('config', token, password)
    release = bundle/'release'
    manifest = json.loads((release/'manifest.json').read_text())
    assert all(digest(release/p) == h for p, h in manifest['files'].items())
    assert json.loads(subprocess.check_output(['docker', 'image', 'inspect', BASE_IMAGE]))[0]['Id'] == BASE_IMAGE
    dockerfile = release/'Dockerfile.return'
    base_tag = 'jubianbian-return:verified-base'
    run('docker', 'tag', BASE_IMAGE, base_tag)
    lines = ['FROM '+base_tag]
    lines += ['COPY '+p+' /app/'+p for p in sorted(manifest['files'])]
    lines += ['COPY ops/verify-repair-recovery.py /app/verify-repair-recovery.py']
    dockerfile.write_text('\n'.join(lines)+'\n')
    run('docker', 'build', '--network=none', '--pull=false', '-t', 'jubianbian-return:'+BUILD,
        '-f', dockerfile, release)
    code = 'import hashlib,json; m=json.loads('+repr(json.dumps(manifest['files']))+'); assert all(hashlib.sha256(open("/app/"+p,"rb").read()).hexdigest()==h for p,h in m.items()); print("RUNTIME_FILES_VERIFIED")'
    run('docker', 'run', '--rm', '--network=none', '--entrypoint', 'python',
        'jubianbian-return:'+BUILD, '-c', code)
    print('TARGET_BUILD_READY', flush=True)


def freeze(password):
    original = Path('/opt/jubianbian/Caddyfile').read_text()
    guard = ('\n    @return_writes method POST PUT PATCH DELETE\n    handle @return_writes {\n'
             '        header Retry-After 120\n        respond "Server migration in progress. Please retry shortly." 503\n    }\n')
    guarded = original if '@return_writes method' in original else original.replace('\n    handle /_jbb_return_', guard+'    handle /_jbb_return_')
    assert guarded.count('@return_writes method') in (2, 3)
    caddy(guarded)
    for name in VOLUMES:
        with sqlite3.connect('file:'+str(volume(name)/'jubianbian.sqlite3')+'?mode=ro', uri=True) as db:
            assert db.execute("select count(*) from tasks where status in ('queued','running','uploading')").fetchone()[0] == 0, 'Active tasks; defer freeze'
    run('systemctl', 'stop', 'jubianbian-staging-deploy.timer', 'jubianbian-staging-monitor.timer')
    run('docker', 'stop', '-t', '60', *[p+'-app-1' for p in PROJECTS])
    baseline = Path('/opt/jbb-migration-20261019/base-manifest.json')
    if not baseline.exists():
        baseline = Path('/opt/jbb-migration-20261019/initial/manifest.json')
    before = json.loads(baseline.read_text())
    after = {}
    changed = []
    target = ROOT/'final'
    (target/'volumes').mkdir(parents=True)
    for name in VOLUMES:
        for relative, entry in files(volume(name)).items():
            key = name+'/'+relative
            after[key] = entry
            if before.get(key) != entry:
                dest = target/'volumes'/key
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(volume(name)/relative, dest)
                changed.append(key)
    (target/'delta.json').write_text(json.dumps({'manifest': after, 'changed': changed}))
    with tarfile.open(ROOT/'final.tar.gz', 'w:gz', compresslevel=1) as archive:
        archive.add(target/'volumes', arcname='volumes')
        archive.add(target/'delta.json', arcname='delta.json')
    seal('final', password)
    print('FROZEN_FINAL_READY', len(after), 'files;', len(changed), 'changed', flush=True)


def restore(token, password):
    target = fetch('final', token, password)
    meta = json.loads((target/'delta.json').read_text())
    changed = set(meta['changed'])
    for name in VOLUMES:
        run('docker', 'volume', 'create', name+SUFFIX)
        assert not any(volume(name+SUFFIX).iterdir()), 'Target volume must be empty'
    for key, entry in meta['manifest'].items():
        name, relative = key.split('/', 1)
        assert name in VOLUMES and '..' not in Path(relative).parts
        source = target/'volumes'/key if key in changed else volume(name)/relative
        assert source.is_file() and source.stat().st_size == entry['size'] and digest(source) == entry['sha256'], 'Source mismatch: '+key
        dest = volume(name+SUFFIX)/relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    actual = {name+'/'+p: value for name in VOLUMES for p, value in files(volume(name+SUFFIX)).items()}
    assert actual == meta['manifest'], 'Restored files mismatch'
    for name in VOLUMES:
        with sqlite3.connect('file:'+str(volume(name+SUFFIX)/'jubianbian.sqlite3')+'?mode=ro', uri=True) as db:
            assert db.execute('pragma integrity_check').fetchone() == ('ok',)
            print('DATABASE_VERIFIED', name, 'users', db.execute('select count(*) from users').fetchone()[0],
                  'tasks', db.execute('select count(*) from tasks').fetchone()[0], flush=True)
    backup = ROOT/'previous-opt'
    backup.mkdir()
    for project, logical, name in zip(PROJECTS, ['jbb-data', 'jbb-staging-data'], VOLUMES):
        root = Path('/opt')/project
        old_caddy = (root/'Caddyfile').read_text() if project == 'jubianbian' else None
        root.rename(backup/project)
        shutil.copytree(ROOT/'config'/project, root)
        if old_caddy is not None:
            (root/'Caddyfile').write_text(old_caddy)
        config = root/'docker-compose.yml'
        text = config.read_text()
        text, count = re.subn(r'(?m)^  '+logical+r':[ \t]*$', '  '+logical+':\n    name: '+name+SUFFIX, text)
        assert count == 1, 'Unexpected compose volume declaration'
        config.write_text(text)
        (root/'release-image.yml').write_text('services:\n  app:\n    image: jubianbian-return:'+BUILD+'\n')
        run('docker', 'tag', 'jubianbian-return:'+BUILD, project+'-app:latest')
    (ROOT/'data-verified.json').write_text(json.dumps({'files': len(actual), 'status': 'verified'}))
    print('LATEST_DATA_RESTORED', len(actual), flush=True)


def start(site):
    project, port = ('jubianbian-staging', 18000) if site == 'staging' else ('jubianbian', 8000)
    if site == 'production':
        assert (ROOT/'staging-verified.json').exists()
    root = Path('/opt')/project
    run('docker', 'compose', '--env-file', root/'fangzhou.env', '-p', project,
        '-f', root/'docker-compose.yml', '-f', root/'release-image.yml', 'up', '-d', '--no-build', '--no-deps', 'app')
    for attempt in range(40):
        try:
            with urlopen('http://127.0.0.1:'+str(port)+'/api/health', timeout=3) as response:
                health = json.load(response)
            assert health['status'] == 'ok' and health['build'] == BUILD and health['environment'] == site
            (ROOT/(site+'-verified.json')).write_text(json.dumps(health))
            print('ENVIRONMENT_VERIFIED', site, json.dumps(health), flush=True)
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError('Health failed: '+site)


def activate():
    assert all((ROOT/(site+'-verified.json')).exists() for site in ['staging', 'production'])
    # Recreate the proxy to bind the replaced host Caddyfile inode.
    text = (ROOT/'config/jubianbian/Caddyfile').read_text()
    Path('/opt/jubianbian/Caddyfile').write_text(text)
    root = Path('/opt/jubianbian')
    run('docker', 'compose', '--env-file', root/'fangzhou.env', '-p', 'jubianbian',
        '-f', root/'docker-compose.yml', 'up', '-d', '--no-build', '--no-deps', '--force-recreate', 'proxy')
    print('SEOUL_LOCAL_ROUTES_ACTIVE', flush=True)


def forward():
    # Only invoke after direct HTTPS to Seoul is verified, preventing proxy loops.
    blocks = []
    for domain in ['jubianbian.com', 'www.jubianbian.com', 'test.jubianbian.com']:
        blocks.append(domain+' {\n    reverse_proxy https://43.155.134.30 {\n        header_up Host '+domain+
                      '\n        transport http {\n            tls_server_name '+domain+'\n        }\n    }\n}\n')
    caddy('\n'.join(blocks))
    run('systemctl', 'stop', 'jbb-return-transfer.service')
    print('BEIJING_FORWARDS_TO_SINGLE_SEOUL_WRITER', flush=True)


def transfer_http(token):
    text = Path('/opt/jubianbian/Caddyfile').read_text()
    assert 'http://82.156.205.129' not in text
    text += ('\nhttp://82.156.205.129 {\n    handle /_jbb_return_'+token+'/* {\n'
             '        uri strip_prefix /_jbb_return_'+token+'\n'
             '        reverse_proxy 172.18.0.1:19020\n    }\n'
             '    handle {\n        respond 404\n    }\n}\n')
    caddy(text)
    print('ENCRYPTED_ARCHIVE_HTTP_ROUTE_READY', flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, exist_ok=True)
    action = sys.argv[1]
    if action in ['prepare', 'build', 'freeze', 'restore', 'transfer-http']:
        credentials = json.loads(sys.stdin.read())
        if action == 'transfer-http':
            transfer_http(credentials['token'])
        elif action == 'freeze':
            freeze(credentials['password'])
        else:
            {'prepare': prepare, 'build': build, 'restore': restore}[action](credentials['token'], credentials['password'])
    elif action == 'start':
        start(sys.argv[2])
    elif action == 'activate':
        activate()
    elif action == 'forward':
        forward()
    else:
        raise SystemExit('Unknown action')
