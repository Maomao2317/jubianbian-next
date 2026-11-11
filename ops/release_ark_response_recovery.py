"""Release the pushed Ark recovery patch over the verified GPU build.

Run on the application host: python3 release_ark_response_recovery.py
prepare|deploy staging|production SHA. Preparation never restarts a service.
"""
import datetime
import fcntl
import json
from pathlib import Path
import re
import shutil
import sys

import release_quality_ledger as common
from release_gpu_transport import stop_when_compute_idle

BASELINE = "43b0d7c9e51265e40d7b9eb1d1cefe347fb5e4ce"
BASE_BUILD = "gpu-transport-20261110-43b0d7c"
APP_FILES = ["backend/app/" + name + ".py" for name in ("errors", "providers", "processing")]
TESTS = ["test_" + name for name in (
    "ark_response_recovery", "script_quality", "provider_delivery", "resource_limits",
    "account_balance", "content_repair", "recognition_regressions", "generation_completeness",
    "quality_capacity", "recognition_cache", "batch_concurrency", "dialogue_export",
    "user_review", "route_queue", "gpu_transport", "remote_asr", "remote_ocr", "ark_cost_control",
)]
CHECK_FILES = ["backend/" + name + ".py" for name in TESTS]
ALLOWED = {*APP_FILES, "backend/test_ark_response_recovery.py", "ops/release_ark_response_recovery.py"}


def image_id(image):
    return json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"]


def prepare(project, site, commit, build, base):
    assert common.git("remote", "get-url", "origin").decode().strip() == "https://github.com/Maomao2317/jubianbian-next.git"
    common.git("fetch", "--no-tags", "origin", common.REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit
    changed = set(common.git("diff", "--name-only", BASELINE, commit).decode().splitlines())
    assert changed == ALLOWED, "Unexpected release files"
    before = common.fingerprint(project)
    assert common.environment(common.inspect(project))["JBB_BUILD_VERSION"] == BASE_BUILD
    common.verify_files(project, 0, {p: common.digest(common.git("show", BASELINE + ":" + p)) for p in APP_FILES})
    hashes = {}
    for p in APP_FILES + CHECK_FILES:
        target = base / p
        target.parent.mkdir(parents=True, exist_ok=True)
        data = common.normalized(common.git("show", commit + ":" + p))
        target.write_bytes(data)
        if p in APP_FILES:
            hashes[p] = common.digest(data)
    image = "jubianbian-release:" + site + "-" + build
    previous = image + "-before"
    common.run(["docker", "tag", before[1], previous])
    dockerfile = base / ("Dockerfile." + site)
    dockerfile.write_text("FROM " + previous + "\n" + "".join("COPY " + p + " /app/" + p + "\n" for p in APP_FILES))
    common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])
    cmd = ["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1", "-e", "JBB_DATA_DIR=/tmp/jbb-release-tests"]
    for p in CHECK_FILES:
        cmd += ["-v", str(base / p) + ":/app/" + p + ":ro"]
    common.run(cmd + ["-w", "/app/backend", "--entrypoint", "python", image, "-m", "unittest", *TESTS])
    assert common.fingerprint(project) == before
    record = dict(commit=commit, build=build, files=hashes, image=image, imageId=image_id(image),
                  previousFingerprint=before, isolatedRegressionPassed=True)
    (base / (site + "-prepared.json")).write_text(json.dumps(record, indent=2))
    print("PREPARED", site, commit, flush=True)


def deploy(project, site, port, commit, build, base):
    record = json.loads((base / (site + "-prepared.json")).read_text())
    assert record["commit"] == commit and record["isolatedRegressionPassed"]
    assert record["previousFingerprint"] == common.fingerprint(project)
    assert record["imageId"] == image_id(record["image"])
    if site == "production":
        staged = json.loads((base / "staging-deployment.json").read_text())
        assert staged["commit"] == commit and staged["verified"]
        assert staged["files"] == record["files"]
        assert staged["fingerprint"] == common.fingerprint("jubianbian-staging")
        common.health(18000, "staging", build)
    root = Path("/opt") / project
    before = common.inspect(project)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == site and old_env["JBB_BUILD_VERSION"] == BASE_BUILD
    files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(p).parent == root for p in files)
    compose = common.compose_args(project, root, files)
    overlay = root / "release-ark-response-recovery.yml"
    assert str(overlay) not in files
    new_compose = common.compose_args(project, root, files + [str(overlay)])
    old_config = json.loads(common.output(compose + ["config", "--format", "json"]))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = base / ("backup-" + site + "-" + stamp)
    backup.mkdir(mode=0o700)
    hold = root / ".staging-deploy-hold"
    controls = [overlay, *([hold] if site == "staging" else [])]
    paths = [*controls, *(root / p for p in APP_FILES)]
    existed = {p for p in paths if p.exists()}
    for p in existed:
        dest = backup / p.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
    record.update(backup=str(backup), previousBuild=BASE_BUILD)
    production_before = common.fingerprint("jubianbian") if site == "staging" else None
    stopped = False
    try:
        overlay.write_text(json.dumps({"services": {"app": {"image": record["image"], "environment": {
            "JBB_BUILD_VERSION": build,
        }}}}, indent=2))
        config = json.loads(common.output(new_compose + ["config", "--format", "json"]))
        app, old_app = config["services"]["app"], old_config["services"]["app"]
        assert app["image"] == record["image"] and app["environment"]["JBB_BUILD_VERSION"] == build
        app["image"] = old_app["image"]
        app["environment"]["JBB_BUILD_VERSION"] = old_app["environment"]["JBB_BUILD_VERSION"]
        assert config == old_config, "Unrelated compose changes"
        if site == "staging":
            hold.write_text("Pinned " + build + "; commit " + commit + "\n")
        record["tasksAtSwitch"] = stop_when_compute_idle(project)
        stopped = True
        for p in APP_FILES:
            assert common.digest((base / p).read_bytes()) == record["files"][p]
            shutil.copy2(base / p, root / p)
        common.run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])
        record["health"] = common.health(port, site, build)
        current = common.inspect(project)
        assert current["Image"] == record["imageId"]
        assert common.environment(current) == dict(old_env, JBB_BUILD_VERSION=build)
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        common.verify_files(project, port, record["files"])
        if site == "staging":
            assert common.fingerprint("jubianbian") == production_before
        record.update(verified=True, configurationPreserved=True, mountsPreserved=True,
                      fingerprint=common.fingerprint(project), tasksAfter=common.counts(project))
        (base / (site + "-deployment.json")).write_text(json.dumps(record, indent=2))
        print("DEPLOYMENT_VERIFIED", site, commit, record["tasksAtSwitch"], record["tasksAfter"], flush=True)
    except BaseException:
        if stopped and common.output(["docker", "ps", "-q", "--filter", "name=^/" + project + "-app-1$"]).strip():
            # If the replacement already claimed jobs, leave it running.
            stop_when_compute_idle(project)
        for p in (paths if stopped else controls):
            if p in existed:
                shutil.copy2(backup / p.relative_to(root), p)
            elif p.exists():
                p.unlink()
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(port, site, BASE_BUILD)
        raise


def main():
    mode, site, commit = sys.argv[1:]
    assert mode in {"prepare", "deploy"} and site in {"staging", "production"}
    assert re.fullmatch("[0-9a-f]{40}", commit)
    build = "ark-response-recovery-" + commit[:7]
    base = Path("/opt/jubianbian-" + build)
    project, port = ("jubianbian-staging", 18000) if site == "staging" else ("jubianbian", 8000)
    with Path("/run/lock/jubianbian-quality-release.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base.mkdir(mode=0o700, exist_ok=True)
        if mode == "prepare":
            prepare(project, site, commit, build, base)
        else:
            deploy(project, site, port, commit, build, base)


if __name__ == "__main__":
    main()
