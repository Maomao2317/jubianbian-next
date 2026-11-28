"""Release the four adaptation UI assets to staging; no production target."""
import datetime
import json
from pathlib import Path
import re
import shutil
import sys

import release_quality_ledger as common
from release_gpu_transport import stop_when_compute_idle
from release_script_adaptation import restore, verify_adaptation_idle, verify_config

REF = "codex/adaptation-ui-polish-20261128"
SOURCE_BASE = "b51f6936524a4dc6847cbed9ee6b7212b172e0fb"
RUNTIME_BASE = "3ef19be6b2ac7ab30b11a6322a1bc16ac0fb72d0"
BASE_BUILD = "script-adaptation-20261126-3ef19be"
PROJECT, PROTECTED, PORT = "jubianbian-staging", "jubianbian", 18000
ROOT = Path("/opt/jubianbian-staging")
ASSETS = ["web-staging/" + name for name in
          ("index.html", "css/app.css", "js/app.js", "js/adaptation.js")]
ALLOWED = set(ASSETS + ["web-staging/test-adaptation-project-list.cjs",
    "web-staging/test-admin-ledger.cjs",
    "ops/release_adaptation_ui.py", "ops/test_release_adaptation_ui.py",
    "ops/adaptation-ui-staging-verification-2026-11-28.md"])


def verify_scope(commit):
    changed = set(common.git("diff", "--name-only", SOURCE_BASE, commit).decode().splitlines())
    assert changed and changed <= ALLOWED, "Unexpected release files: " + str(changed - ALLOWED)
    common.git("merge-base", "--is-ancestor", SOURCE_BASE, commit)


def fetch(commit):
    assert common.git("remote", "get-url", "origin").decode().strip() == "https://github.com/Maomao2317/jubianbian-next.git"
    common.git("fetch", "--no-tags", "origin", REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit, "Not the pushed branch head"
    verify_scope(commit)


def prepare(commit, base, build):
    fetch(commit)
    stage, production = common.fingerprint(PROJECT), common.fingerprint(PROTECTED)
    before = common.inspect(PROJECT)
    env = common.environment(before)
    assert env["JBB_ENVIRONMENT"] == "staging" and env["JBB_BUILD_VERSION"] == BASE_BUILD
    common.health(PORT, "staging", BASE_BUILD)
    common.verify_files(PROJECT, PORT, {p: common.digest(common.git("show", RUNTIME_BASE + ":" + p)) for p in ASSETS})
    hashes = {}
    for path in ASSETS:
        data = common.normalized(common.git("show", commit + ":" + path))
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[path] = common.digest(data)
    # Test the candidate frontend against the unchanged adaptation backend.
    for path in ("backend/test_adaptation.py",):
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(common.git("show", RUNTIME_BASE + ":" + path))
    image = "jubianbian-release:" + build
    pinned = image + "-before"
    common.run(["docker", "tag", before["Image"], pinned])
    dockerfile = base / "Dockerfile.staging"
    dockerfile.write_text("FROM " + pinned + "\n" + "".join("COPY " + p + " /app/" + p + "\n" for p in ASSETS))
    common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])
    common.run(["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1",
        "-e", "JBB_ENVIRONMENT=staging", "-e", "JBB_DATA_DIR=/tmp/jbb-ui-tests",
        "-v", str(base / "backend/test_adaptation.py") + ":/app/backend/test_adaptation.py:ro",
        "-w", "/app/backend", "--entrypoint", "python", image, "-m", "unittest", "test_adaptation"])
    assert common.fingerprint(PROJECT) == stage
    assert common.fingerprint(PROTECTED) == production
    record = dict(commit=commit, build=build, files=hashes, image=image,
        imageId=json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"],
        stagingBefore=stage, productionBefore=production, checksPassed=True)
    (base / "prepared.json").write_text(json.dumps(record, indent=2))
    print("UI_PREPARED", commit, "four assets; isolated adaptation regression passed", flush=True)


def deploy(commit, base, build):
    fetch(commit)
    record = json.loads((base / "prepared.json").read_text())
    assert record["commit"] == commit and record["build"] == build and record["checksPassed"]
    assert common.fingerprint(PROJECT) == record["stagingBefore"]
    assert common.fingerprint(PROTECTED) == record["productionBefore"]
    assert json.loads(common.output(["docker", "image", "inspect", record["image"]]))[0]["Id"] == record["imageId"]
    before = common.inspect(PROJECT)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == "staging" and old_env["JBB_BUILD_VERSION"] == BASE_BUILD
    files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(p).parent == ROOT for p in files)
    overlay, hold = ROOT / ("release-" + build + ".yml"), ROOT / ".staging-deploy-hold"
    assert not overlay.exists()
    compose = common.compose_args(PROJECT, ROOT, files)
    updated = common.compose_args(PROJECT, ROOT, files + [str(overlay)])
    config = json.loads(common.output(compose + ["config", "--format", "json"]))
    backup = base / ("backup-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    backup.mkdir(mode=0o700)
    paths = [overlay, hold, *(ROOT / p for p in ASSETS)]
    existed = {p for p in paths if p.exists()}
    for path in existed:
        target = backup / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    record["backup"] = str(backup)
    stopped = False
    try:
        overlay.write_text(json.dumps({"services": {"app": {"image": record["image"],
            "environment": {"JBB_BUILD_VERSION": build}}}}, indent=2))
        verify_config(config, json.loads(common.output(updated + ["config", "--format", "json"])), record["image"], build)
        hold.write_text("Pinned " + build + "; commit " + commit + "\n")
        record["tasksAtSwitch"] = stop_when_compute_idle(PROJECT, verify_idle=verify_adaptation_idle)
        stopped = True
        for path in ASSETS:
            assert common.digest((base / path).read_bytes()) == record["files"][path]
            target = ROOT / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base / path, target)
        common.run(updated + ["up", "-d", "--no-build", "--no-deps", "app"])
        record["health"] = common.health(PORT, "staging", build)
        current = common.inspect(PROJECT)
        assert current["Image"] == record["imageId"]
        assert common.environment(current) == dict(old_env, JBB_BUILD_VERSION=build)
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        common.verify_files(PROJECT, PORT, record["files"])
        assert common.fingerprint(PROTECTED) == record["productionBefore"], "Production changed"
        record.update(verified=True, productionAfter=common.fingerprint(PROTECTED))
        (base / "deployment.json").write_text(json.dumps(record, indent=2))
        print("UI_STAGING_VERIFIED", commit, build, "production runtime unchanged", flush=True)
    except BaseException as exc:
        record["failure"] = type(exc).__name__
        (base / "deployment-failure.json").write_text(json.dumps(record, indent=2))
        if stopped and common.inspect(PROJECT)["State"]["Running"]:
            try:
                stop_when_compute_idle(PROJECT, verify_idle=verify_adaptation_idle)
            except RuntimeError:
                record["rollbackDeferredActiveTasks"] = True
                (base / "deployment-failure.json").write_text(json.dumps(record, indent=2))
                raise
        restore(paths if stopped else [overlay, hold], existed, backup, ROOT)
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(PORT, "staging", BASE_BUILD)
        raise


def main(argv=None):
    args = sys.argv if argv is None else argv
    if len(args) != 3 or args[1] not in {"prepare", "deploy"} or not re.fullmatch(r"[0-9a-f]{40}", args[2]):
        raise ValueError("usage: release_adaptation_ui.py prepare|deploy SHA (staging only)")
    mode, commit = args[1:]
    build = "adaptation-ui-20261128-" + commit[:7]
    base = Path("/opt/jubianbian-" + build)
    import fcntl
    with Path("/run/lock/jubianbian-quality-release.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base.mkdir(mode=0o700, exist_ok=True)
        (prepare if mode == "prepare" else deploy)(commit, base, build)


if __name__ == "__main__":
    main()
