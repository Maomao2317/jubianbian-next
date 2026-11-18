"""Deploy translation resilience and screenplay-focus changes to staging only.

Run on the application host:
    python3 release_multilingual_quality.py prepare SHA
    python3 release_multilingual_quality.py deploy SHA

Preparation builds and tests an isolated image without restarting staging.
Production is fingerprint-protected and is never a deployment target here.
"""
import copy
import datetime
import json
from pathlib import Path
import re
import shutil
import sys

import release_quality_ledger as common


BASELINE = "24775e67df221bacd47ba9f2b51c86eaa1b8ea7c"
BASE_BUILD = "multilingual-quality-20261118-24775e6"
REF = "codex/multilingual-performance-quality"
BUILD_PREFIX = "translation-screenplay-focus-20261118-"
PROJECT = "jubianbian-staging"
PROTECTED_PROJECT = "jubianbian"
PORT = 18000
SITE = "staging"
APP_FILES = [
    "backend/app/processing.py",
    "backend/app/providers.py",
    "backend/app/script.py",
]
CHANGED_TEST_FILES = [
    "backend/test_process_task_translation.py",
    "backend/test_provider_delivery.py",
    "backend/test_script_quality.py",
    "backend/test_translation.py",
    "backend/test_visual_description.py",
]
RELEASE_SUPPORT_FILES = [
    "ops/release_multilingual_quality.py",
    "ops/test_release_multilingual_quality.py",
]
ALLOWED = set(APP_FILES + CHANGED_TEST_FILES + RELEASE_SUPPORT_FILES)


def image_id(image):
    return json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"]


def verify_config(old, new, image, build):
    adjusted = copy.deepcopy(new)
    app = adjusted["services"]["app"]
    old_app = old["services"]["app"]
    assert app["image"] == image
    assert app["environment"]["JBB_BUILD_VERSION"] == build
    app["image"] = old_app["image"]
    app["environment"]["JBB_BUILD_VERSION"] = old_app["environment"]["JBB_BUILD_VERSION"]
    assert adjusted == old, "Unrelated staging Compose configuration changed"


def parse_args(argv):
    if len(argv) != 3 or argv[1] not in {"prepare", "deploy"}:
        raise ValueError("usage: release_multilingual_quality.py prepare|deploy SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", argv[2]):
        raise ValueError("SHA must be a full lowercase Git commit id")
    return argv[1], argv[2]


def fetch_revision(commit):
    assert common.git("remote", "get-url", "origin").decode().strip() == (
        "https://github.com/Maomao2317/jubianbian-next.git"
    )
    common.git("fetch", "--no-tags", "origin", REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit, (
        "Revision is not the pushed branch head"
    )


def test_files(commit):
    paths = common.git(
        "ls-tree", "-rz", "--name-only", commit, "--", "backend"
    ).decode().split("\0")
    return sorted(path for path in paths if re.fullmatch(r"backend/test_[^/]+\.py", path))


def prepare(commit, build, base):
    fetch_revision(commit)
    changed = set(common.git("diff", "--name-only", BASELINE, commit).decode().splitlines())
    assert changed == ALLOWED, "Unexpected release files: " + str(changed.symmetric_difference(ALLOWED))

    staging_before = common.fingerprint(PROJECT)
    production_before = common.fingerprint(PROTECTED_PROJECT)
    current = common.inspect(PROJECT)
    old_env = common.environment(current)
    assert old_env["JBB_ENVIRONMENT"] == SITE
    assert old_env["JBB_BUILD_VERSION"] == BASE_BUILD, "Staging runtime baseline changed"
    common.health(PORT, SITE, BASE_BUILD)
    common.verify_files(PROJECT, PORT, {
        path: common.digest(common.git("show", BASELINE + ":" + path))
        for path in APP_FILES
    })

    checks = test_files(commit)
    hashes = {}
    for path in APP_FILES + checks:
        data = common.normalized(common.git("show", commit + ":" + path))
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path in APP_FILES:
            hashes[path] = common.digest(data)

    build_image = "jubianbian-release:" + SITE + "-" + build
    previous_image = build_image + "-before"
    common.run(["docker", "tag", current["Image"], previous_image])
    dockerfile = base / "Dockerfile.staging"
    dockerfile.write_text(
        "FROM " + previous_image + "\n"
        + "".join("COPY " + path + " /app/" + path + "\n" for path in APP_FILES)
    )
    common.run([
        "docker", "build", "--network=none", "--pull=false", "-q", "-t", build_image,
        "-f", dockerfile, base,
    ])

    command = [
        "docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1",
        "-e", "JBB_DATA_DIR=/tmp/jbb-release-tests",
    ]
    for path in checks:
        command += ["-v", str(base / path) + ":/app/" + path + ":ro"]
    modules = [Path(path).stem for path in checks]
    common.run(command + [
        "-w", "/app/backend", "--entrypoint", "python", build_image,
        "-m", "unittest", *modules,
    ])
    assert common.fingerprint(PROJECT) == staging_before, (
        "Staging runtime changed during preparation"
    )
    assert common.fingerprint(PROTECTED_PROJECT) == production_before, (
        "Production runtime changed during preparation"
    )

    record = {
        "commit": commit,
        "build": build,
        "files": hashes,
        "image": build_image,
        "imageId": image_id(build_image),
        "stagingFingerprintBefore": staging_before,
        "productionFingerprintBefore": production_before,
        "checks": checks,
        "isolatedRegressionPassed": True,
    }
    (base / "staging-prepared.json").write_text(json.dumps(record, indent=2))
    print(
        "PREPARED", SITE, commit,
        "full isolated backend regression passed; runtimes unchanged", flush=True,
    )


def _restore(paths, existed, backup, root):
    for path in paths:
        if path in existed:
            shutil.copy2(backup / path.relative_to(root), path)
        elif path.exists():
            path.unlink()


def deploy(commit, build, base):
    fetch_revision(commit)
    record = json.loads((base / "staging-prepared.json").read_text())
    assert record["commit"] == commit and record["build"] == build
    assert record["isolatedRegressionPassed"]
    assert record["imageId"] == image_id(record["image"]), "Prepared image changed"
    assert record["stagingFingerprintBefore"] == common.fingerprint(PROJECT), (
        "Staging changed after preparation"
    )
    assert record["productionFingerprintBefore"] == common.fingerprint(PROTECTED_PROJECT), (
        "Production changed after preparation"
    )

    root = Path("/opt") / PROJECT
    before = common.inspect(PROJECT)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == SITE
    assert old_env["JBB_BUILD_VERSION"] == BASE_BUILD, "Staging runtime baseline changed"
    files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(path).parent == root for path in files), "Unexpected staging Compose path"
    overlay = root / "release-translation-screenplay-focus.yml"
    assert str(overlay) not in files, "Translation/screenplay release overlay is already active"
    compose = common.compose_args(PROJECT, root, files)
    new_compose = common.compose_args(PROJECT, root, files + [str(overlay)])
    old_config = json.loads(common.output(compose + ["config", "--format", "json"]))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = base / ("backup-staging-" + stamp)
    backup.mkdir(mode=0o700)
    hold = root / ".staging-deploy-hold"
    paths = [overlay, hold, *(root / path for path in APP_FILES)]
    existed = {path for path in paths if path.exists()}
    for path in existed:
        target = backup / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    deployment = copy.deepcopy(record)
    deployment.update(backup=str(backup), previousBuild=BASE_BUILD)
    stopped = False
    try:
        overlay.write_text(json.dumps({"services": {"app": {
            "image": record["image"],
            "environment": {"JBB_BUILD_VERSION": build},
        }}}, indent=2))
        config = json.loads(common.output(new_compose + ["config", "--format", "json"]))
        verify_config(old_config, config, record["image"], build)
        hold.write_text("Pinned " + build + "; commit " + commit + "\n")

        deployment["tasksAtSwitch"] = common.stop_only_when_idle(PROJECT)
        stopped = True
        for path in APP_FILES:
            assert common.digest((base / path).read_bytes()) == record["files"][path]
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base / path, target)
        common.run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])

        deployment["health"] = common.health(PORT, SITE, build)
        current = common.inspect(PROJECT)
        assert current["Image"] == record["imageId"]
        assert common.environment(current) == dict(old_env, JBB_BUILD_VERSION=build)
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        common.verify_files(PROJECT, PORT, record["files"])
        assert common.fingerprint(PROTECTED_PROJECT) == record["productionFingerprintBefore"], (
            "Production runtime changed"
        )

        deployment.update(
            verified=True,
            configurationPreserved=True,
            mountsPreserved=True,
            portsPreserved=True,
            fingerprint=common.fingerprint(PROJECT),
            productionFingerprintAfter=common.fingerprint(PROTECTED_PROJECT),
            tasksAfter=common.counts(PROJECT),
        )
        (base / "staging-deployment.json").write_text(json.dumps(deployment, indent=2))
        print(
            "DEPLOYMENT_VERIFIED", SITE, commit,
            deployment["tasksAtSwitch"], deployment["tasksAfter"], flush=True,
        )
    except BaseException:
        if stopped and common.inspect(PROJECT)["State"]["Running"]:
            try:
                common.stop_only_when_idle(PROJECT)
            except RuntimeError:
                deployment["rollbackDeferredActiveTasks"] = True
                deployment["failure"] = (
                    "New tasks became active; automatic rollback deferred to avoid interruption"
                )
                (base / "staging-deployment.json").write_text(
                    json.dumps(deployment, indent=2)
                )
                raise
        _restore(paths if stopped else [overlay, hold], existed, backup, root)
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(PORT, SITE, BASE_BUILD)
        raise


def main(argv=None):
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
            deploy(commit, build, base)


if __name__ == "__main__":
    main()
