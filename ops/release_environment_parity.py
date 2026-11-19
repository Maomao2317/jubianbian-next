"""Promote one pushed application revision through staging to production.

Usage: release_environment_parity.py prepare|staging|production SHA
Only the app image, build label, and application source mirror are changed.
Environment settings, credentials, data volumes, ports, and GPU endpoints stay
specific to each site.  Production requires a verified staging deployment.
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
from release_gpu_transport import stop_when_compute_idle


REF = "codex/multilingual-performance-quality"
BASELINE = "e18dba831668d7a68a8d02de204c218ef74d637a"
STAGING_BASE_BUILD = "video-retention-20261119-93901ed"
PRODUCTION_BASE_BUILD = "environment-parity-20261119-e18dba8"
BUILD_PREFIX = "video-retention-fix-20261119-"
RELEASE_FILES = {
    ".env.example",
    "docker-compose.yml",
    "docker-compose.staging.yml",
    "ops/README.md",
    "ops/cleanup_data.py",
    "ops/video_retention.py",
    "ops/test_video_retention.py",
    "ops/video-retention.cron",
    "ops/release_environment_parity.py",
    "ops/test_release_environment_parity.py",
    "ops/video-retention-release-2026-11-19.md",
}
SITES = {
    "staging": ("jubianbian-staging", 18000, STAGING_BASE_BUILD),
    "production": ("jubianbian", 8000, PRODUCTION_BASE_BUILD),
}


def app_files(commit: str) -> list[str]:
    paths = common.git("ls-tree", "-rz", "--name-only", commit, "--", "backend/app", "web-staging")
    found = paths.decode().strip("\0").split("\0")
    return sorted(path for path in found if (
        path.startswith("backend/app/") and path.endswith(".py")
        or path == "web-staging/index.html"
        or path.startswith("web-staging/js/") and path.endswith(".js")
        or path.startswith("web-staging/css/") and path.endswith(".css")
    ))


def test_files(commit: str) -> list[str]:
    paths = common.git("ls-tree", "-rz", "--name-only", commit, "--", "backend")
    return sorted(path for path in paths.decode().strip("\0").split("\0")
                  if re.fullmatch(r"backend/test_[^/]+\.py", path))


def image_id(image: str) -> str:
    return json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"]


def fetch_revision(commit: str) -> None:
    assert common.git("remote", "get-url", "origin").decode().strip() == (
        "https://github.com/Maomao2317/jubianbian-next.git"
    )
    common.git("fetch", "--no-tags", "origin", REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit, (
        "Revision is not the pushed branch head"
    )


def verify_config(old: dict, new: dict, image: str, build: str) -> None:
    adjusted = copy.deepcopy(new)
    app = adjusted["services"]["app"]
    old_app = old["services"]["app"]
    assert app["image"] == image
    assert app["environment"]["JBB_BUILD_VERSION"] == build
    assert app["environment"]["JBB_RETENTION_DAYS"] == "14"
    app["image"] = old_app["image"]
    app["environment"]["JBB_BUILD_VERSION"] = old_app["environment"]["JBB_BUILD_VERSION"]
    app["environment"]["JBB_RETENTION_DAYS"] = old_app["environment"]["JBB_RETENTION_DAYS"]
    assert adjusted == old, "Unrelated Compose configuration changed"


def prepare(commit: str, build: str, base: Path) -> None:
    fetch_revision(commit)
    changed = set(common.git("diff", "--name-only", BASELINE, commit).decode().splitlines())
    assert changed == RELEASE_FILES, "Unexpected release files: " + str(changed ^ RELEASE_FILES)
    before = {site: common.fingerprint(project) for site, (project, _, _) in SITES.items()}
    for site, (project, port, expected) in SITES.items():
        env = common.environment(common.inspect(project))
        assert env["JBB_ENVIRONMENT"] == site and env["JBB_BUILD_VERSION"] == expected
        common.health(port, site, expected)

    files = app_files(commit)
    checks = test_files(commit)
    assert files and checks
    hashes = {}
    for path in files + checks + ["ops/cleanup_data.py", "ops/video_retention.py",
                                 "ops/test_video_retention.py"]:
        data = common.normalized(common.git("show", commit + ":" + path))
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path in files or path in {"ops/cleanup_data.py", "ops/video_retention.py"}:
            hashes[path] = common.digest(data)

    # Staging is the already verified base. Every tracked application source
    # is copied from the pushed revision so both sites receive identical code.
    source_image = common.inspect(SITES["staging"][0])["Image"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", source_image)
    source_tag = "jubianbian-parity-base:" + source_image[7:19]
    common.run(["docker", "tag", source_image, source_tag])
    image = "jubianbian-release:" + build
    dockerfile = base / "Dockerfile.parity"
    dockerfile.write_text(
        "FROM " + source_tag + "\n"
        + "".join("COPY " + p + " /app/" + p + "\n" for p in files)
        + "COPY ops/cleanup_data.py /app/ops/cleanup_data.py\n"
        + "COPY ops/video_retention.py /app/ops/video_retention.py\n"
    )
    common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image,
                "-f", dockerfile, base])
    cmd = ["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1",
           "-e", "JBB_DATA_DIR=/tmp/jbb-parity-tests"]
    for path in checks:
        cmd += ["-v", str(base / path) + ":/app/" + path + ":ro"]
    common.run(cmd + ["-w", "/app/backend", "--entrypoint", "python", image,
                      "-m", "unittest", *(Path(path).stem for path in checks)])
    common.run(["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1",
                "-e", "PYTHONPATH=/app/backend:/app/ops", "-e", "JBB_DATA_DIR=/tmp/jbb-retention-tests",
                "-v", str(base / "ops/test_video_retention.py") + ":/app/ops/test_video_retention.py:ro",
                "-w", "/app/ops", "--entrypoint", "python", image,
                "-m", "unittest", "test_video_retention"])
    assert {site: common.fingerprint(project) for site, (project, _, _) in SITES.items()} == before
    record = {"commit": commit, "build": build, "files": hashes, "image": image,
              "imageId": image_id(image), "fingerprintsBefore": before,
              "isolatedRegressionPassed": True}
    (base / "prepared.json").write_text(json.dumps(record, indent=2))
    print("PREPARED", commit, "isolated regression passed; environments unchanged", flush=True)


def restore(paths: list[Path], existed: set[Path], backup: Path, root: Path) -> None:
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
    project, port, expected = SITES[site]
    other = "production" if site == "staging" else "staging"
    other_project, other_port, _ = SITES[other]
    if site == "production":
        staged = json.loads((base / "staging-deployment.json").read_text())
        assert staged["verified"] and staged["commit"] == commit
        assert common.fingerprint(other_project) == staged["fingerprint"]
        common.health(other_port, other, build)
    assert common.fingerprint(project) == record["fingerprintsBefore"][site]
    protected_before = common.fingerprint(other_project)
    root = Path("/opt") / project
    before = common.inspect(project)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == site and old_env["JBB_BUILD_VERSION"] == expected
    files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(path).parent == root for path in files)
    overlay = root / ("release-environment-parity-" + commit[:7] + ".yml")
    assert str(overlay) not in files
    compose = common.compose_args(project, root, files)
    new_compose = common.compose_args(project, root, files + [str(overlay)])
    old_config = json.loads(common.output(compose + ["config", "--format", "json"]))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = base / ("backup-" + site + "-" + stamp)
    backup.mkdir(mode=0o700)
    hold = root / ".staging-deploy-hold"
    paths = [overlay, *(root / path for path in record["files"])]
    if site == "staging":
        paths.append(hold)
    existed = {path for path in paths if path.exists()}
    for path in existed:
        target = backup / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    deployment = dict(record, site=site, backup=str(backup), previousBuild=expected)
    stopped = False
    try:
        overlay.write_text(json.dumps({"services": {"app": {
            "image": record["image"], "environment": {
                "JBB_BUILD_VERSION": build, "JBB_RETENTION_DAYS": "14"},
        }}}, indent=2))
        new_config = json.loads(common.output(new_compose + ["config", "--format", "json"]))
        verify_config(old_config, new_config, record["image"], build)
        if site == "staging":
            hold.write_text("Pinned " + build + "; commit " + commit + "\n")
        deployment["tasksAtSwitch"] = stop_when_compute_idle(project)
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
        assert common.environment(current) == dict(old_env, JBB_BUILD_VERSION=build,
                                                   JBB_RETENTION_DAYS="14")
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        common.verify_files(project, port, record["files"])
        assert common.fingerprint(other_project) == protected_before
        deployment.update(verified=True, configurationPreserved=True, mountsPreserved=True,
                          portsPreserved=True, fingerprint=common.fingerprint(project),
                          protectedFingerprint=protected_before, tasksAfter=common.counts(project))
        (base / (site + "-deployment.json")).write_text(json.dumps(deployment, indent=2))
        print("DEPLOYMENT_VERIFIED", site, commit, deployment["tasksAtSwitch"],
              deployment["tasksAfter"], flush=True)
    except BaseException:
        if stopped and common.inspect(project)["State"]["Running"]:
            try:
                stop_when_compute_idle(project)
            except RuntimeError:
                deployment["rollbackDeferredActiveTasks"] = True
                (base / (site + "-deployment.json")).write_text(json.dumps(deployment, indent=2))
                raise
        restore(paths if stopped else [overlay, hold] if site == "staging" else [overlay],
                existed, backup, root)
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(port, site, expected)
        raise


def main(argv: list[str] | None = None) -> None:
    import fcntl
    args = sys.argv if argv is None else argv
    assert len(args) == 3 and args[1] in {"prepare", *SITES}
    commit = args[2]
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    build = BUILD_PREFIX + commit[:7]
    base = Path("/opt/jubianbian-" + build)
    with Path("/run/lock/jubianbian-quality-release.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base.mkdir(mode=0o700, exist_ok=True)
        if args[1] == "prepare":
            prepare(commit, build, base)
        else:
            deploy(args[1], commit, build, base)


if __name__ == "__main__":
    main()
