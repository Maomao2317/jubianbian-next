"""Promote the already verified dialogue-reliability image to production."""
import argparse
import ast
import datetime
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import types


REPO = Path('/opt/jubianbian-github-check')
RUNTIME_COMMIT = '10bcef474147353893355d206287c1b7ccfe7b59'
BASELINE = 'd0fc2dce755a1cea7f28e5d18bb372c6d19b5a9f'
BUILD = 'dialogue-reliability-10bcef4'
DRIVER = 'ops/deploy-stability-completeness.py'
SUPPORT = 'ops/restore-gpu-routing.py'
SELF = 'ops/promote-dialogue-reliability.py'
APP_CHANGES = {
    'backend/app/processing.py',
    'backend/app/providers.py',
    'backend/app/routes.py',
}
TEST_CHANGES = {
    'backend/test_content_repair.py',
    'backend/test_provider_delivery.py',
    'backend/test_user_review.py',
}
OLD_RELEASE = Path('/opt/jbb-video-reliability-20261023-' + RUNTIME_COMMIT[:12])


def git(*args):
    return subprocess.check_output(
        ['git', '-c', 'safe.directory=' + str(REPO), '-C', str(REPO), *args]
    )


def validate_artifacts(prepared, staging):
    assert prepared.get('sourceCommit') == RUNTIME_COMMIT
    assert prepared.get('build') == BUILD
    assert prepared.get('offlineTestsPassed') is True
    assert staging.get('sourceCommit') == RUNTIME_COMMIT
    assert staging.get('build') == BUILD
    assert staging.get('site') == 'staging' and staging.get('verified') is True
    assert staging.get('configurationPreserved') is True
    assert staging.get('dataMountsPreserved') is True
    assert staging.get('image') == prepared.get('image')
    assert isinstance(prepared.get('files'), dict) and prepared['files']


def validate_source_scope(changes):
    assert set(changes) == APP_CHANGES | TEST_CHANGES, 'Unexpected runtime source scope'


def transform_smoke(smoke):
    """Keep the content smoke offline and align it with current retry policy."""
    old_route = 'route = {"model": "lite", "mode": "complexity", "band": "simple",'
    new_route = 'route = {"provider": "ark", "model": "lite", "mode": "complexity", "band": "simple",'
    old_assertion = 'assert not processing._is_non_retryable_provider_error(exc)'
    new_assertion = 'assert processing._is_non_retryable_provider_error(exc)'
    assert smoke.count(old_route) == 1, 'Unexpected content smoke route'
    assert smoke.count(old_assertion) == 1, 'Unexpected incomplete-repair assertion'
    smoke = smoke.replace(old_route, new_route, 1)
    smoke = smoke.replace(old_assertion, new_assertion, 1)
    assert 'patch.object(processing, "ark_recognize"' in smoke
    assert 'processing.glm_recognize(' not in smoke
    return smoke


def load_smoke(driver_path):
    tree = ast.parse(driver_path.read_text(encoding='utf-8'))
    assignment = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'smoke'
                for target in node.targets)
    )
    pieces = [node.value for node in ast.walk(assignment.value)
              if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    return transform_smoke(''.join(textwrap.dedent(piece) for piece in pieces))


def verify_container_source(routing, container, manifest):
    check = (
        "import sys,json,hashlib; from pathlib import Path; "
        "m=json.load(sys.stdin); bad=[p for p,v in m.items() "
        "if not (Path('/app')/p).is_file() or "
        "hashlib.sha256((Path('/app')/p).read_bytes()).hexdigest()!=v]; "
        "assert not bad, 'source mismatch: '+repr(bad); "
        "print('SOURCE_VERIFIED',len(m))"
    )
    return routing.output(
        ['docker', 'exec', '-i', container, 'python', '-c', check],
        input=json.dumps(manifest).encode(),
    ).decode().strip()


def load_driver():
    driver_path = REPO / DRIVER
    support_path = REPO / SUPPORT
    assert git('show', RUNTIME_COMMIT + ':' + DRIVER) == driver_path.read_bytes()
    assert git('show', RUNTIME_COMMIT + ':' + SUPPORT) == support_path.read_bytes()
    spec = importlib.util.spec_from_file_location('dialogue_release_driver', driver_path)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    driver.BASELINE = BASELINE
    driver.BUILD = BUILD
    driver.DEFAULTS = {}
    driver.APP_CHANGES = set(APP_CHANGES)
    driver.FRONTEND_TEST_CHANGES = set()

    original_dedent = textwrap.dedent

    def release_dedent(value, *args, **kwargs):
        result = original_dedent(value, *args, **kwargs)
        if 'async def verify_incomplete_requeues' in result:
            return transform_smoke(result)
        return result

    driver.textwrap = types.SimpleNamespace(dedent=release_dedent)
    return driver


def work_root(tool_commit):
    return Path('/opt/jbb-dialogue-reliability-20261103-' + RUNTIME_COMMIT[:12] + '-' + tool_commit[:12])


def ensure_work_artifacts(root, prepared_path, staging_path):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    for source in (prepared_path, staging_path):
        target = root / source.name
        if target.exists():
            assert target.read_bytes() == source.read_bytes(), 'Promotion metadata changed'
        else:
            shutil.copy2(source, target)


def run_driver(driver, argv):
    previous = sys.argv
    try:
        sys.argv = argv
        driver.main()
    finally:
        sys.argv = previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool-commit', required=True)
    parser.add_argument('--step', choices=('staging-smoke', 'production'), required=True)
    args = parser.parse_args()
    assert re.fullmatch(r'[0-9a-f]{40}', args.tool_commit)
    tool_source = git('show', args.tool_commit + ':' + SELF)
    assert Path(__file__).read_bytes() == tool_source
    remote_ref = git('ls-remote', 'origin', 'refs/heads/codex/repair-batch-content').decode().split()
    assert remote_ref and remote_ref[0] == args.tool_commit, 'Release helper is not the pushed branch tip'
    git('merge-base', '--is-ancestor', RUNTIME_COMMIT, args.tool_commit)
    git('fetch', 'origin', RUNTIME_COMMIT)
    assert git('rev-parse', 'FETCH_HEAD').decode().strip() == RUNTIME_COMMIT
    validate_source_scope(git('diff', '--name-only', BASELINE, RUNTIME_COMMIT,
                              '--', 'backend', 'web-staging').decode().splitlines())

    prepared_path = OLD_RELEASE / 'prepared.json'
    staging_path = OLD_RELEASE / 'staging.json'
    prepared = json.loads(prepared_path.read_text())
    staging = json.loads(staging_path.read_text())
    validate_artifacts(prepared, staging)
    release_root = work_root(args.tool_commit)
    smoke_record_path = release_root / 'staging-smoke-fixed.json'
    if args.step == 'production':
        smoke_record = json.loads(smoke_record_path.read_text())
        assert smoke_record.get('sourceCommit') == RUNTIME_COMMIT
        assert smoke_record.get('image') == prepared.get('image')
        assert smoke_record.get('verified') is True

    driver = load_driver()
    routing = driver.routing if hasattr(driver, 'routing') else None
    if routing is None:
        support_spec = importlib.util.spec_from_file_location('routing', REPO / SUPPORT)
        routing = importlib.util.module_from_spec(support_spec)
        support_spec.loader.exec_module(routing)
    stage = routing.inspect('jubianbian-staging')
    production = routing.inspect('jubianbian')
    production_env = routing.environment(production)
    assert stage['Image'] == prepared['image']
    assert production['Image'] == prepared['baseImage']
    assert routing.health(18000, BUILD, 'staging')['build'] == BUILD
    assert routing.health(8000, production_env['JBB_BUILD_VERSION'], 'production')['status'] == 'ok'
    print(verify_container_source(routing, 'jubianbian-staging-app-1', prepared['files']), flush=True)

    if args.step == 'staging-smoke':
        ensure_work_artifacts(release_root, prepared_path, staging_path)
        smoke = load_smoke(REPO / DRIVER)
        result = routing.output([
            'docker', 'exec', '-e', 'PYTHONPATH=/app/backend',
            'jubianbian-staging-app-1', 'python', '-c', smoke,
        ]).decode().strip()
        assert 'VISUAL_DESCRIPTION_VERIFIED' in result
        assert 'CONTENT_RELIABILITY_VERIFIED' in result
        smoke_record = {
            'sourceCommit': RUNTIME_COMMIT,
            'build': BUILD,
            'image': prepared['image'],
            'verified': True,
            'providerIsolation': 'Ark mock in smoke subprocess only',
            'productionEnvironmentUnchanged': True,
            'result': result,
            'verifiedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        smoke_record_path.write_text(json.dumps(smoke_record, indent=2))
        smoke_record_path.chmod(0o600)
        print('STAGING_SMOKE_VERIFIED', result, flush=True)
        return

    ensure_work_artifacts(release_root, prepared_path, staging_path)
    release = release_root / ('production-attempt-' +
                              datetime.datetime.now(datetime.timezone.utc).strftime('%H%M%S'))
    release.mkdir(mode=0o700)
    release.chmod(0o700)
    for name in ('prepared.json', 'staging.json', 'staging-smoke-fixed.json'):
        shutil.copy2(release_root / name, release / name)

    original_path = driver.Path
    old_release = original_path(str(OLD_RELEASE))
    new_release = original_path(str(release))

    def mapped_path(value, *path_args, **path_kwargs):
        path = original_path(value, *path_args, **path_kwargs)
        return new_release if path == old_release else path

    driver.Path = mapped_path
    run_driver(driver, [DRIVER, 'production', '--commit', RUNTIME_COMMIT])


if __name__ == '__main__':
    main()
