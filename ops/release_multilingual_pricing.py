"""Promote the language-pricing revision through staging and production.

Run on the application host from the checked-out repository:
    python3 ops/release_multilingual_pricing.py prepare SHA
    python3 ops/release_multilingual_pricing.py staging SHA
    python3 ops/release_multilingual_pricing.py production SHA

Preparation builds one overlay image and runs isolated regressions. Deployment
changes only the image, build label, and application source mirror; each site's
environment, retention policy, ports, volumes, credentials, and data remain
site-specific. Production requires a verified staging deployment of the same
image and commit.
"""
from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path
import re
import shutil
import sys

import release_quality_ledger as common


REF = "codex/multilingual-performance-quality"
BASELINE = "2eab63675119428b93fc043cc2431d1aca57b7c8"
BUILD_PREFIX = "multilingual-pricing-20261120-"
SITES = {
    "staging": ("jubianbian-staging", 18000),
    "production": ("jubianbian", 8000),
}
APP_FILES = [
    "backend/app/auth_store.py",
    "backend/app/processing.py",
    "backend/app/pricing.py",
    "backend/app/routes.py",
    "backend/app/task_store.py",
    "backend/app/user_view.py",
    "web-staging/js/api.js",
    "web-staging/js/app.js",
]
CHECK_FILES = [
    "backend/test_admin_console.py",
    "backend/test_large_batch.py",
    "backend/test_upload_flow.py",
    "backend/test_language_pricing.py",
    "backend/test_route_queue.py",
    "backend/test_account_balance.py",
    "backend/test_user_review.py",
    "backend/test_ark_cost_control.py",
    "web-staging/test-user-review.cjs",
    "web-staging/test-language-pricing.cjs",
]
SUPPORT_FILES = ["ops/release_multilingual_pricing.py", "ops/test_release_multilingual_pricing.py"]
ALLOWED = set(APP_FILES + CHECK_FILES + SUPPORT_FILES)


def image_id(image: str) -> str:
    return json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"]


def parse_args(argv: list[str]) -> tuple[str, str]:
    if len(argv) != 3 or argv[1] not in {"prepare", *SITES}:
        raise ValueError("usage: release_multilingual_pricing.py prepare|staging|production SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", argv[2]):
        raise ValueError("SHA must be a full lowercase Git commit id")
    return argv[1], argv[2]


def fetch_revision(commit: str) -> None:
    assert common.git("remote", "get-url", "origin").decode().strip() == (
        "https://github.com/Maomao2317/jubianbian-next.git"
    )
    common.git("fetch", "--no-tags", "origin", REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit, (
        "remote branch head is not the requested pushed revision"
    )


def verify_config(old: dict, new: dict, image: str, build: str) -> None:
    adjusted = copy.deepcopy(new)
    app = adjusted["services"]["app"]
    old_app = old["services"]["app"]
    assert app["image"] == image
    assert app["environment"]["JBB_BUILD_VERSION"] == build
    if "image" in old_app:
        app["image"] = old_app["image"]
    else:
        app.pop("image")
    app["environment"]["JBB_BUILD_VERSION"] = old_app["environment"]["JBB_BUILD_VERSION"]
    assert adjusted == old, "compose changed beyond image and build label"


def _changed_files(commit: str) -> set[str]:
    return set(common.git("diff", "--name-only", BASELINE, commit).decode().splitlines())


def verify_release_scope(commit: str) -> None:
    changed = _changed_files(commit)
    unexpected = changed - ALLOWED
    assert not unexpected, "unexpected release files: " + str(unexpected)


def _write_revision_files(commit: str, base: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in APP_FILES + CHECK_FILES:
        data = common.normalized(common.git("show", f"{commit}:{path}"))
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path in APP_FILES:
            hashes[path] = common.digest(data)
    return hashes


def prepare(commit: str, build: str, base: Path) -> None:
    fetch_revision(commit)
    verify_release_scope(commit)
    before = {site: common.fingerprint(project) for site, (project, _) in SITES.items()}
    for site, (project, port) in SITES.items():
        environment = common.environment(common.inspect(project))
        assert environment["JBB_ENVIRONMENT"] == site
        common.health(port, site, environment["JBB_BUILD_VERSION"])

    hashes = _write_revision_files(commit, base)
    source_image = common.inspect(SITES["staging"][0])["Image"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", source_image)
    base_image = "jubianbian-pricing-base:" + source_image[7:19]
    common.run(["docker", "tag", source_image, base_image])
    image = "jubianbian-release:" + build
    dockerfile = base / "Dockerfile.pricing"
    dockerfile.write_text(
        "FROM " + base_image + "\n"
        + "".join(f"COPY {path} /app/{path}\n" for path in APP_FILES)
    )
    common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])

    command = ["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1",
               "-e", "JBB_DATA_DIR=/tmp/jbb-pricing-tests"]
    for path in CHECK_FILES:
        command += ["-v", str(base / path) + ":/app/" + path + ":ro"]
    command += ["-w", "/app/backend", "--entrypoint", "python", image, "-m", "unittest"]
    command += [Path(path).stem for path in CHECK_FILES if path.startswith("backend/")]
    common.run(command)
    assert {site: common.fingerprint(project) for site, (project, _) in SITES.items()} == before
    record = {
        "commit": commit,
        "build": build,
        "files": hashes,
        "image": image,
        "imageId": image_id(image),
        "fingerprintsBefore": before,
        "isolatedRegressionPassed": True,
    }
    (base / "prepared.json").write_text(json.dumps(record, indent=2))
    print("PREPARED", commit, "isolated regression passed; runtimes unchanged", flush=True)


def _restore(paths: list[Path], existed: set[Path], backup: Path, root: Path) -> None:
    for path in paths:
        if path in existed:
            shutil.copy2(backup / path.relative_to(root), path)
        elif path.exists():
            path.unlink()


def deploy(site: str, commit: str, build: str, base: Path) -> None:
    fetch_revision(commit)
    record = json.loads((base / "prepared.json").read_text())
    assert record["commit"] == commit and record["build"] == build
    assert record["isolatedRegressionPassed"] and image_id(record["image"]) == record["imageId"]
    project, port = SITES[site]
    other = "production" if site == "staging" else "staging"
    other_project, other_port = SITES[other]
    if site == "production":
        staged = json.loads((base / "staging-deployment.json").read_text())
        assert staged["verified"] and staged["commit"] == commit and staged["imageId"] == record["imageId"]
        common.health(other_port, other, build)
        assert common.fingerprint(other_project) == staged["fingerprint"]
    assert common.fingerprint(project) == record["fingerprintsBefore"][site]
    protected_before = common.fingerprint(other_project)

    root = Path("/opt") / project
    before = common.inspect(project)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == site
    files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(path).parent == root for path in files)
    compose = common.compose_args(project, root, files)
    old_config = json.loads(common.output(compose + ["config", "--format", "json"]))
    overlay = root / ("release-multilingual-pricing-" + commit[:7] + ".yml")
    assert str(overlay) not in files, "pricing release overlay is already active"
    new_compose = common.compose_args(project, root, files + [str(overlay)])
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = base / ("backup-" + site + "-" + stamp)
    backup.mkdir(mode=0o700)
    paths = [overlay, *(root / path for path in record["files"])]
    existed = {path for path in paths if path.exists()}
    for path in existed:
        target = backup / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    deployment = dict(record, site=site, backup=str(backup), previousBuild=old_env["JBB_BUILD_VERSION"])
    stopped = False
    try:
        overlay.write_text(json.dumps({"services": {"app": {
            "image": record["image"], "environment": {"JBB_BUILD_VERSION": build}
        }}}, indent=2))
        new_config = json.loads(common.output(new_compose + ["config", "--format", "json"]))
        verify_config(old_config, new_config, record["image"], build)
        deployment["tasksAtSwitch"] = common.stop_only_when_idle(project)
        stopped = True
        for path, digest in record["files"].items():
            source = base / path
            assert common.digest(source.read_bytes()) == digest
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        common.run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])
        deployment["health"] = common.health(port, site, build)
        current = common.inspect(project)
        assert current["Image"] == record["imageId"]
        assert common.environment(current) == dict(old_env, JBB_BUILD_VERSION=build)
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        common.verify_files(project, port, record["files"])
        assert common.fingerprint(other_project) == protected_before
        deployment.update(verified=True, configurationPreserved=True, mountsPreserved=True,
                          portsPreserved=True, fingerprint=common.fingerprint(project),
                          protectedFingerprint=protected_before, tasksAfter=common.counts(project))
        (base / (site + "-deployment.json")).write_text(json.dumps(deployment, indent=2))
        print("DEPLOYMENT_VERIFIED", site, commit, deployment["tasksAtSwitch"], deployment["tasksAfter"], flush=True)
    except BaseException:
        if stopped and common.inspect(project)["State"]["Running"]:
            common.stop_only_when_idle(project)
        _restore(paths if stopped else [overlay], existed, backup, root)
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(port, site, old_env["JBB_BUILD_VERSION"])
        raise


def main(argv: list[str] | None = None) -> None:
    import fcntl

    mode, commit = parse_args(sys.argv if argv is None else argv)
    build = BUILD_PREFIX + commit[:7]
    base = Path("/opt/jubianbian-" + build)
    with Path("/run/lock/jubianbian-quality-release.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base.mkdir(mode=0o700, exist_ok=True)
        if mode == "prepare":
            prepare(commit, build, base)
        else:
            deploy(mode, commit, build, base)


if __name__ == "__main__":
    main()
