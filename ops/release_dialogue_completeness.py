"""Release a pushed dialogue-quality patch without interrupting recognition.

Run on the application host:
python3 release_dialogue_completeness.py prepare|deploy staging|production SHA
Preparation is isolated and does not restart either live service.
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

BASELINE = "b8e87e2180ce3d942f5ea98dda5fc90914d33349"
BASE_BUILD = "dialogue-completeness-20261112-b8e87e2"
REF = "codex/script-translation-language"
APP_FILES = ["backend/app/" + name + ".py" for name in ("evidence", "processing", "providers", "dialogue_recovery")]
NEW_FILES = {"backend/app/dialogue_recovery.py"}
PRESERVED_FILES = ["backend/app/" + name + ".py" for name in ("config", "routes", "script")]
PRESERVED_FILES += ["web-staging/index.html", "web-staging/css/app.css", "web-staging/js/app.js"]
TESTS = ["test_" + name for name in (
    "dialogue_recovery", "spoken_subtitles", "script_quality", "subtitle_priority", "provider_delivery",
    "generation_completeness", "recognition_regressions", "content_repair", "quality_capacity",
    "user_review", "dialogue_export", "ark_response_recovery", "ark_cost_control",
    "remote_asr", "remote_ocr", "route_queue", "recognition_cache", "batch_concurrency",
    "account_balance", "rechecks", "admin_ledger", "ocr_dialogue_integrity",
)]
CHECK_FILES = ["backend/" + name + ".py" for name in TESTS]
ALLOWED = set(APP_FILES + ["backend/test_dialogue_recovery.py"])
ALLOWED.update({"ops/release_dialogue_completeness.py", "ops/test_release_dialogue_completeness.py",
                "ops/dialogue-recovery-release-2026-11-12.md"})
BUDGET_ENV = {
    "JBB_ARK_QUALITY_MIN_GROSS_MARGIN_RATIO": "0",
    "JBB_ARK_REVENUE_REALIZATION_RATIO": "0.80",
    "JBB_ARK_INFRA_RESERVE_RATIO": "0.05",
    "JBB_ARK_TURBO_COST_SAFETY_FACTOR": "1.25",
    "JBB_ARK_QUALITY_BUDGET_OVERRUN_RATIO": "0",
}


def image_id(image):
    return json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"]


def verify_config(old, new, image, update):
    """Only the release image and explicitly authorized budget keys may differ."""
    adjusted = json.loads(json.dumps(new))
    app, old_app = adjusted["services"]["app"], old["services"]["app"]
    assert app["image"] == image
    assert all(app["environment"][key] == value for key, value in update.items())
    app["image"] = old_app["image"]
    for key in update:
        if key in old_app["environment"]:
            app["environment"][key] = old_app["environment"][key]
        else:
            app["environment"].pop(key, None)
    assert adjusted == old, "Unrelated compose changes"


def prepare(project, site, port, commit, build, base):
    assert common.git("remote", "get-url", "origin").decode().strip() == "https://github.com/Maomao2317/jubianbian-next.git"
    common.git("fetch", "--no-tags", "origin", REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit, "Not the pushed head"
    changed = set(common.git("diff", "--name-only", BASELINE, commit).decode().splitlines())
    assert changed <= ALLOWED and set(APP_FILES) <= changed, "Unexpected release files"
    before = common.fingerprint(project)
    assert common.environment(common.inspect(project))["JBB_BUILD_VERSION"] == BASE_BUILD
    assert all(common.environment(common.inspect(project)).get(key) == value for key, value in BUDGET_ENV.items())
    common.verify_files(project, port, {
        p: common.digest(common.git("show", BASELINE + ":" + p)) for p in APP_FILES + PRESERVED_FILES if p not in NEW_FILES
    })
    hashes = {}
    for p in APP_FILES + PRESERVED_FILES + CHECK_FILES:
        target = base / p
        target.parent.mkdir(parents=True, exist_ok=True)
        data = common.normalized(common.git("show", commit + ":" + p))
        target.write_bytes(data)
        if p in APP_FILES + PRESERVED_FILES:
            hashes[p] = common.digest(data)
    image = "jubianbian-release:" + site + "-" + build
    previous = image + "-before"
    common.run(["docker", "tag", before[1], previous])
    dockerfile = base / ("Dockerfile." + site)
    dockerfile.write_text("FROM " + previous + "\n" + "".join("COPY " + p + " /app/" + p + "\n" for p in APP_FILES))
    common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])
    cmd = ["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1",
           "-e", "JBB_DATA_DIR=/tmp/jbb-release-tests"]
    for p in CHECK_FILES:
        cmd += ["-v", str(base / p) + ":/app/" + p + ":ro"]
    common.run(cmd + ["-w", "/app/backend", "--entrypoint", "python", image, "-m", "unittest", *TESTS])
    assert common.fingerprint(project) == before
    record = dict(commit=commit, build=build, files=hashes, image=image, imageId=image_id(image),
                  previousFingerprint=before, isolatedRegressionPassed=True, budgetEnvironment=BUDGET_ENV)
    (base / (site + "-prepared.json")).write_text(json.dumps(record, indent=2))
    print("PREPARED", site, commit, flush=True)


def verify_staging(base, commit, build, hashes):
    staged = json.loads((base / "staging-deployment.json").read_text())
    assert staged["commit"] == commit and staged["verified"]
    assert staged["files"] == hashes
    assert staged["fingerprint"] == common.fingerprint("jubianbian-staging")
    common.health(18000, "staging", build)
    common.verify_files("jubianbian-staging", 18000, hashes)


def deploy(project, site, port, commit, build, base):
    record = json.loads((base / (site + "-prepared.json")).read_text())
    assert record["commit"] == commit and record["build"] == build and record["isolatedRegressionPassed"]
    assert record["previousFingerprint"] == common.fingerprint(project)
    assert record["imageId"] == image_id(record["image"])
    assert record["budgetEnvironment"] == BUDGET_ENV
    if site == "production":
        verify_staging(base, commit, build, record["files"])
    root = Path("/opt") / project
    before = common.inspect(project)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == site and old_env["JBB_BUILD_VERSION"] == BASE_BUILD
    files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(p).parent == root for p in files)
    compose = common.compose_args(project, root, files)
    overlay = root / "release-dialogue-recovery.yml"
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
    update = dict(BUDGET_ENV, JBB_BUILD_VERSION=build)
    stopped = False
    try:
        overlay.write_text(json.dumps({"services": {"app": {"image": record["image"], "environment": update}}}, indent=2))
        config = json.loads(common.output(new_compose + ["config", "--format", "json"]))
        verify_config(old_config, config, record["image"], update)
        if site == "staging":
            hold.write_text("Pinned " + build + "; commit " + commit + "\n")
        record["tasksAtSwitch"] = stop_when_compute_idle(project)
        stopped = True
        for p in APP_FILES:
            assert common.digest((base / p).read_bytes()) == record["files"][p]
            (root / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base / p, root / p)
        common.run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])
        record["health"] = common.health(port, site, build)
        current = common.inspect(project)
        assert current["Image"] == record["imageId"]
        assert common.environment(current) == dict(old_env, **update)
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        common.verify_files(project, port, record["files"])
        if site == "staging":
            assert common.fingerprint("jubianbian") == production_before
        record.update(verified=True, configurationPreserved=True, mountsPreserved=True,
                      fingerprint=common.fingerprint(project), tasksAfter=common.counts(project))
        (base / (site + "-deployment.json")).write_text(json.dumps(record, indent=2))
        print("DEPLOYMENT_VERIFIED", site, commit, flush=True)
    except BaseException:
        if stopped and common.output(["docker", "ps", "-q", "--filter", "name=^/" + project + "-app-1$"]).strip():
            # Refuse rollback if the replacement has already claimed jobs.
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
    build = "dialogue-recovery-20261112-" + commit[:7]
    base = Path("/opt/jubianbian-" + build)
    project, port = ("jubianbian-staging", 18000) if site == "staging" else ("jubianbian", 8000)
    with Path("/run/lock/jubianbian-quality-release.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base.mkdir(mode=0o700, exist_ok=True)
        if mode == "prepare":
            prepare(project, site, port, commit, build, base)
        else:
            deploy(project, site, port, commit, build, base)


if __name__ == "__main__":
    main()
