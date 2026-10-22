"""Verify pushed source, test offline, then promote the same image idle only."""
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
import textwrap
from urllib.request import urlopen
from urllib.parse import quote

BASELINE = '4f1a11864ad520448d696a6fbcea5774d70e8137'
BUILD = 'content-reliability-20261022-r1'
SELF = 'ops/deploy-stability-completeness.py'
VERIFY = 'ops/verify-stability-completeness.py'
SUPPORT = 'ops/restore-gpu-routing.py'
# Preserve the verified upload deadline and every other runtime setting,
# including upload concurrency and the Files fallback.
DEFAULTS = {'JBB_MEDIA_PROXY_UPLOAD_TIMEOUT_SECONDS': '900'}
APP_CHANGES = {'backend/app/storage.py', 'backend/app/providers.py', 'backend/app/processing.py'}
FRONTEND_TEST_CHANGES = set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=['prepare', 'staging', 'production'])
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    assert re.fullmatch('[0-9a-f]{40}', args.commit)
    support = Path(__file__).with_name(Path(SUPPORT).name)
    spec = importlib.util.spec_from_file_location('routing', support)
    routing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routing)
    git, output = routing.git, routing.output
    assert git('rev-parse', 'FETCH_HEAD').decode().strip() == args.commit
    for relative, path in [(SELF, Path(__file__)), (SUPPORT, support)]:
        assert git('show', args.commit+':'+relative) == path.read_bytes()
    subprocess.run(['git', '-c', 'safe.directory='+str(routing.REPO), '-C', routing.REPO,
                    'merge-base', '--is-ancestor', BASELINE, args.commit], check=True)
    runtime_changes = set(git('diff', '--name-only', BASELINE, args.commit, '--',
                              'backend/app', 'web-staging').decode().splitlines())
    expected_runtime_changes = {path for path in APP_CHANGES | FRONTEND_TEST_CHANGES
                                if path.startswith(('backend/app/', 'web-staging/'))}
    assert runtime_changes == expected_runtime_changes, 'Unexpected application release scope'
    locks = []
    for project in ['jubianbian', 'jubianbian-staging']:
        lock = Path('/run/lock/'+project+'-deploy.lock').open('w')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locks.append(lock)
    release = Path('/opt/jbb-content-reliability-20261022-'+args.commit[:12])
    release.mkdir(mode=0o700, exist_ok=True)
    release.chmod(0o700)
    files = [p for p in git('ls-tree', '-rz', '--name-only', args.commit, '--', 'backend', 'web-staging').decode().split('\0') if p]
    manifest = {p: hashlib.sha256(git('show', args.commit+':'+p)).hexdigest()
                for p in files if p.startswith(('backend/app/', 'web-staging/'))}
    check = "import sys,json,hashlib; from pathlib import Path; m=json.load(sys.stdin); bad=[p for p,v in m.items() if not (Path('/app')/p).is_file() or hashlib.sha256((Path('/app')/p).read_bytes()).hexdigest()!=v]; assert not bad, 'source mismatch: '+repr(bad); print('SOURCE_VERIFIED',len(m))"
    def verify_files(project, hashes):
        return output(['docker', 'exec', '-i', project+'-app-1', 'python', '-c', check], input=json.dumps(hashes).encode()).decode().strip()
    prepared_file = release/'prepared.json'
    if args.step == 'prepare':
        stage, prod = routing.inspect('jubianbian-staging'), routing.inspect('jubianbian')
        assert stage['Image'] == prod['Image'], 'Unexpected different deployed baselines'
        base_manifest = {p: hashlib.sha256(git('show', BASELINE+':'+p)).hexdigest()
                         for p in git('ls-tree', '-rz', '--name-only', BASELINE, '--', 'backend/app', 'web-staging').decode().split('\0') if p}
        for project in ['jubianbian-staging', 'jubianbian']:
            print(project, verify_files(project, base_manifest), flush=True)
        context = release/'context'
        context.mkdir(exist_ok=True)
        for path in files:
            target = context/path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git('show', args.commit+':'+path))
        base_tag = 'jubianbian-app:stability-base-'+args.commit[:12]
        subprocess.run(['docker', 'tag', prod['Image'], base_tag], check=True)
        (context/'Dockerfile').write_text('FROM '+base_tag+'\nCOPY backend /app/backend\nCOPY web-staging /app/web-staging\n')
        tag = 'jubianbian-app:stability-'+args.commit[:12]
        subprocess.run(['docker', 'build', '--pull=false', '--network=none', '-t', tag, context], check=True)
        image = json.loads(output(['docker', 'image', 'inspect', tag]))[0]['Id']
        with (release/'backend-tests.log').open('wb') as log:
            subprocess.run(['docker', 'run', '--rm', '--network', 'none', '-e',
                            'JBB_DATA_DIR=/tmp/jbb-validation', '-e', 'JBB_RESOURCE_DIR=/tmp/jbb-limits',
                            '-w', '/app/backend', '--entrypoint', 'python', image,
                            '-m', 'unittest', 'discover', '-s', '.'], check=True, timeout=240,
                           stdout=log, stderr=subprocess.STDOUT)
        baseline = release/'baseline_script.py'
        baseline.write_bytes(git('show', BASELINE+':backend/app/script.py'))
        verifier = git('show', args.commit+':'+VERIFY)
        replays = {}
        for project in ['jubianbian-staging', 'jubianbian']:
            db_mount = next(x['Source'] for x in routing.inspect(project)['Mounts'] if x['Destination'] == '/app/data')
            result = output(['docker', 'run', '--rm', '--network', 'none', '-i',
                             '-e', 'JBB_DATA_DIR=/tmp/jbb-audit', '-e', 'PYTHONPATH=/app/backend',
                             '-v', str(db_mount)+':/audit:ro',
                             '-v', str(baseline)+':/baseline_script.py:ro',
                             '--entrypoint', 'python', image, '-'], input=verifier, timeout=300)
            replays[project] = json.loads(result)
        prepared = dict(sourceCommit=args.commit, build=BUILD, baseImage=prod['Image'], image=image,
                        offlineTestsPassed=True, replays=replays, files=manifest)
        prepared_file.write_text(json.dumps(prepared, indent=2))
        print('PREPARED', json.dumps({k:v for k,v in prepared.items() if k != 'files'}), flush=True)
        return
    prepared = json.loads(prepared_file.read_text())
    assert prepared['sourceCommit'] == args.commit and prepared['offlineTestsPassed']
    if args.step == 'production':
        staged = json.loads((release/'staging.json').read_text())
        assert staged['verified'] and staged['sourceCommit'] == args.commit
        assert routing.inspect('jubianbian-staging')['Image'] == prepared['image']
        routing.health(18000, BUILD, 'staging')
    project = 'jubianbian-staging' if args.step == 'staging' else 'jubianbian'
    root = Path('/opt')/project
    port = 18000 if args.step == 'staging' else 8000
    previous = routing.inspect(project)
    assert previous['Image'] == prepared['baseImage'], 'Deployment baseline changed'
    old_env = routing.environment(previous)
    old_build = old_env['JBB_BUILD_VERSION']
    assert old_env['JBB_ENVIRONMENT'] == args.step
    routing.health(port, old_build, args.step)
    routing.assert_idle(project)
    backup = release/(args.step+'-backup')
    backup.mkdir(mode=0o700)
    targets = sorted(APP_CHANGES)+['fangzhou.env', 'release-image.yml', '.staging-deploy-hold']
    existed = {}
    for path in targets:
        source = root/path
        existed[path] = source.exists()
        if source.exists():
            target = backup/path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if path == 'fangzhou.env':
                target.chmod(0o600)
    rollback = backup/'rollback-image.yml'
    rollback.write_text('services:\n  app:\n    image: '+previous['Image']+'\n')
    record = dict(sourceCommit=args.commit, build=BUILD, image=prepared['image'], site=args.step,
                  previousImage=previous['Image'], previousBuild=old_build,
                  backup=str(backup), existed=existed, verified=False)
    recordfile = release/(args.step+'.json')
    recordfile.write_text(json.dumps(record, indent=2))
    process_env = {k:v for k,v in os.environ.items() if k not in old_env and k not in DEFAULTS}
    process_env['JBB_BUILD_VERSION'] = BUILD
    command = ['docker', 'compose', '--env-file', root/'fangzhou.env', '-p', project,
               '-f', root/'docker-compose.yml', '-f', root/'release-image.yml']
    replaced = False
    try:
        for path in sorted(APP_CHANGES):
            target = root/path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git('show', args.commit+':'+path))
        envfile = root/'fangzhou.env'
        lines = [line for line in envfile.read_text().splitlines()
                 if line.removeprefix('export ').partition('=')[0].strip() != 'JBB_BUILD_VERSION']
        envfile.write_text('\n'.join(lines+['JBB_BUILD_VERSION='+BUILD])+'\n')
        envfile.chmod(0o600)
        # Earlier releases may supply settings solely through this override.
        # Snapshot the effective environment so replacing the file cannot drop
        # those settings. Escape Compose interpolation and keep secrets private.
        pinned_env = {**old_env, **DEFAULTS, 'JBB_BUILD_VERSION': BUILD}
        override = {'services': {'app': {'image': prepared['image'], 'environment': {
            key: value.replace('$', '$$') for key, value in pinned_env.items()
        }}}}
        override_file = root/'release-image.yml'
        override_file.touch(mode=0o600, exist_ok=True)
        override_file.chmod(0o600)
        override_file.write_text(json.dumps(override, ensure_ascii=True, indent=2)+'\n')
        (root/'.staging-deploy-hold').write_text('Pinned '+BUILD+'; source '+args.commit+'; backup '+str(backup)+'\n')
        planned = json.loads(output(command+['config', '--format', 'json'], env=process_env))
        # Compose's rendered config keeps escaped dollars; the container gets
        # the literal value after one interpolation escape is removed.
        planned_env = {k:str(v).replace('$$', '$')
                       for k,v in planned['services']['app']['environment'].items()}
        assert planned_env == pinned_env, 'Effective configuration changed unexpectedly'
        assert planned_env['JBB_BUILD_VERSION'] == BUILD and planned['services']['app']['image'] == prepared['image']
        assert all(planned_env.get(k) == v for k, v in DEFAULTS.items())
        routing.assert_idle(project)
        assert routing.inspect(project)['Id'] == previous['Id']
        replaced = True
        subprocess.run([str(x) for x in command+['up', '-d', '--no-build', '--no-deps', 'app']], check=True, env=process_env)
        record['health'] = routing.health(port, BUILD, args.step)
        current = routing.inspect(project)
        assert current['Image'] == prepared['image']
        assert routing.mounts(current) == routing.mounts(previous)
        assert current['HostConfig']['PortBindings'] == previous['HostConfig']['PortBindings']
        assert {k:v for k,v in routing.environment(current).items() if k not in DEFAULTS and k != 'JBB_BUILD_VERSION'} == {k:v for k,v in old_env.items() if k not in DEFAULTS and k != 'JBB_BUILD_VERSION'}
        assert all(routing.environment(current).get(k) == v for k, v in DEFAULTS.items())
        timeout_check = "from app import config; assert config.MEDIA_PROXY_UPLOAD_TIMEOUT_SECONDS == 900; print('UPLOAD_DEADLINE_VERIFIED', config.MEDIA_PROXY_UPLOAD_TIMEOUT_SECONDS)"
        print(output(['docker', 'exec', '-e', 'PYTHONPATH=/app/backend', project+'-app-1', 'python', '-c', timeout_check]).decode().strip(), flush=True)
        print(args.step, verify_files(project, manifest), flush=True)
        for relative, digest in manifest.items():
            if relative.startswith('web-staging/') and not relative.endswith('.cjs'):
                with urlopen('http://127.0.0.1:'+str(port)+'/'+quote(relative.removeprefix('web-staging/'), safe='/'), timeout=5) as response:
                    assert hashlib.sha256(response.read()).hexdigest() == digest
        smoke = textwrap.dedent("""
            import asyncio
            import tempfile
            from pathlib import Path
            from unittest.mock import AsyncMock, patch
            from app import processing, remote_circuit as circuit, storage
            from app.errors import ArkError

            rich = {"sources": [
                {"kind": "audio", "status": "available", "items": [{"text": "完整对白"}]},
                {"kind": "ocr", "status": "available", "items": []},
                {"kind": "keyframes", "status": "available", "items": [{"type": "keyframe"}]},
            ]}
            route = processing.select_ark_route(158, rich)
            assert route["band"] == "complex" and "duration>=120s" in route["reasons"]

            with tempfile.TemporaryDirectory() as root:
                media = Path(root) / "clip.mp4"
                media.write_bytes(b"video")
                with patch.object(storage, "OBJECT_STORAGE_BUCKET", "fixture"), \
                     patch.object(storage, "MEDIA_TRANSFER_QUEUE_TIMEOUT_SECONDS", 600), \
                     patch.object(storage, "capacity") as capacity, \
                     patch.object(storage, "_bounded_transfer"):
                    storage.upload_recognition_proxy(media, "fixture", queue_timeout=30)
                assert capacity.call_args.kwargs["timeout"] == 30

            original = {"title": "第1集", "characters": ["甲"], "scenes": [{
                "heading": "1-1 日 内 客厅", "location": "客厅",
                "blocks": [{"type": "action", "text": "仅有结尾。",
                            "startSec": 50, "endSec": 60}],
            }]}
            repair_quality = processing._evaluate_quality(
                original, "ark", {"durationSec": 60},
                {"model": "lite", "mode": "complexity", "band": "simple",
                 "score": 0, "reasons": []}, recover_evidence=False,
            )[0]
            repair_quality["contentRepair"] = {
                "attemptedRanges": [{"startSec": 0, "endSec": 50}],
                "completedRanges": [], "errors": [],
                "remainingRanges": [{"startSec": 0, "endSec": 50}],
            }

            async def verify_incomplete_requeues():
                route = {"model": "lite", "mode": "complexity", "band": "simple",
                         "score": 0, "reasons": []}
                with patch.object(processing, "mark_task_stage"), \
                     patch.object(processing, "ARK_API_KEYS", ("fixture",)), \
                     patch.object(processing, "ARK_TURBO_MODEL", "turbo"), \
                     patch.object(processing, "ark_recognize", return_value=(original, {})), \
                     patch.object(processing, "repair_content_gaps",
                                  AsyncMock(return_value=(original, repair_quality, {}))):
                    try:
                        await processing._run_recognizer_with_media(
                            {"id": "fixture", "title": "第1集", "attempts": 0},
                            Path("unused"), 60, {"durationSec": 60}, route, {}, None,
                        )
                    except ArkError as exc:
                        assert exc.provider_code == "content_repair_incomplete"
                        assert not processing._is_non_retryable_provider_error(exc)
                        return
                    raise AssertionError("Incomplete screenplay was deliverable")

            asyncio.run(verify_incomplete_requeues())

            with tempfile.TemporaryDirectory() as root:
                isolated = circuit.RemoteCircuit(Path(root))
                with patch.object(circuit, "circuit", isolated), \
                     patch.object(circuit, "_probe", side_effect=TimeoutError()) as probe:
                    try:
                        with circuit.remote_attempt("asr", "http://offline", "fixture", 1):
                            raise AssertionError("First failure was not admitted")
                    except TimeoutError:
                        pass
                    try:
                        with circuit.remote_attempt("asr", "http://offline", "fixture", 1):
                            raise AssertionError("Open circuit was admitted")
                    except circuit.CircuitOpen:
                        pass
                    assert probe.call_count == 1

            print("CONTENT_RELIABILITY_VERIFIED")
        """)
        print(output(['docker', 'exec', '-e', 'PYTHONPATH=/app/backend', project+'-app-1', 'python', '-c', smoke]).decode().strip(), flush=True)
        record.update(verified=True, configurationPreserved=True, dataMountsPreserved=True,
                      changedConfiguration=DEFAULTS,
                      sourceFilesVerified=len(manifest), sameImageAsStaging=args.step=='production',
                      verifiedAt=datetime.datetime.now(datetime.timezone.utc).isoformat())
        recordfile.write_text(json.dumps(record, indent=2))
        print('DEPLOYMENT_VERIFIED', json.dumps({k:v for k,v in record.items() if k != 'existed'}), flush=True)
    except BaseException:
        for path in targets:
            if existed[path]:
                shutil.copy2(backup/path, root/path)
            else:
                (root/path).unlink(missing_ok=True)
        if replaced:
            try:
                routing.assert_idle(project)
            except RuntimeError:
                record.update(rollbackDeferred=True, configurationFileRestored=True)
                recordfile.write_text(json.dumps(record, indent=2))
                print('ROLLBACK_DEFERRED_ACTIVE_TASKS', str(backup), flush=True)
                raise
            subprocess.run([str(x) for x in command+['-f', rollback, 'up', '-d', '--no-build', '--no-deps', 'app']], check=True,
                           env=dict(process_env, JBB_BUILD_VERSION=old_build))
            routing.health(port, old_build, args.step)
        print('DEPLOYMENT_ROLLED_BACK', args.step, str(backup), flush=True)
        raise


if __name__ == '__main__':
    main()
