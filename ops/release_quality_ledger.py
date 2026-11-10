"""Deploy a verified, pushed revision as a bounded overlay on each live image.

Usage: python3 release_quality_ledger.py prepare|deploy staging|production SHA
No task, balance, credential, or GPU-service changes are made by this helper.
"""
from pathlib import Path
import datetime
import fcntl
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen

BASELINE = "5ed007adda160bec058a0f1dca5a26fe80f66e2f"
REF = "codex/repair-batch-content"
REPO = Path("/opt/jubianbian-github-check")
APP_FILES = ["backend/app/" + name + ".py" for name in
             ("config", "evidence", "processing", "providers", "remote_circuit", "routes", "script", "ledger_summary")]
APP_FILES += ["web-staging/index.html", "web-staging/js/app.js", "web-staging/css/app.css"]
TESTS = ["test_" + name for name in (
    "admin_ledger", "quality_capacity", "script_quality", "remote_circuit", "ark_cost_control",
    "content_repair", "recognition_regressions", "generation_completeness", "remote_asr", "remote_ocr",
    "recognition_cache", "batch_concurrency", "dialogue_export", "user_review", "admin_console",
    "route_queue", "provider_delivery", "resource_limits", "storage_transfer")]
CHECK_FILES = ["backend/" + name + ".py" for name in TESTS]
ALLOWED = set(APP_FILES + CHECK_FILES + ["web-staging/test-admin-ledger.cjs", "web-staging/test-user-review.cjs",
    "ops/release_quality_ledger.py", "ops/quality-ledger-release-2026-11-10.md"])
BUDGET_ENV = {"JBB_ARK_MIN_GROSS_MARGIN_RATIO": "0.65", "JBB_ARK_REVENUE_REALIZATION_RATIO": "0.80",
              "JBB_ARK_INFRA_RESERVE_RATIO": "0.05", "JBB_ARK_QUALITY_BUDGET_OVERRUN_RATIO": "0.03"}


def output(args, **kwargs):
    return subprocess.check_output([str(v) for v in args], **kwargs)


def run(args, **kwargs):
    return subprocess.run([str(v) for v in args], check=True, **kwargs)


def git(*args):
    return output(["git", "-c", "safe.directory=" + str(REPO), "-C", REPO, *args])


def inspect(project):
    return json.loads(output(["docker", "inspect", project + "-app-1"]))[0]


def fingerprint(project):
    d = inspect(project)
    return [d["Id"], d["Image"], d["State"]["StartedAt"]]


def environment(d):
    return dict(v.split("=", 1) for v in d["Config"]["Env"])


def mounts(d):
    return sorted((v["Source"], v["Destination"], v["RW"]) for v in d["Mounts"])


def database(project):
    return Path(next(m["Source"] for m in inspect(project)["Mounts"] if m["Destination"] == "/app/data")) / "jubianbian.sqlite3"


def counts(project):
    with sqlite3.connect("file:" + str(database(project)) + "?mode=ro", uri=True) as c:
        return dict(c.execute("SELECT status,count(*) FROM tasks GROUP BY status"))


def stop_only_when_idle(project):
    # The writer lock closes the race between the idle check and worker claims
    # or new submissions. Release it after the old worker has stopped, before
    # starting its replacement. Existing recognition jobs are never interrupted.
    c = sqlite3.connect(str(database(project)), timeout=5)
    try:
        c.execute("BEGIN IMMEDIATE")
        state = dict(c.execute("SELECT status,count(*) FROM tasks GROUP BY status"))
        if any(state.get(s, 0) for s in ("queued", "running", "uploading")):
            raise RuntimeError("Release deferred: active tasks " + str(state))
        run(["docker", "stop", "--time", "5", project + "-app-1"], timeout=15)
        return state
    finally:
        c.rollback()
        c.close()


def normalized(data):
    return data.replace(b"\r\n", b"\n")


def digest(data):
    return hashlib.sha256(normalized(data)).hexdigest()


def health(port, site, build):
    for _ in range(40):
        try:
            with urlopen("http://127.0.0.1:" + str(port) + "/api/health", timeout=3) as response:
                value = json.load(response)
            if value.get("status") == "ok" and value.get("environment") == site and value.get("build") == build:
                return value
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("Health/build verification failed: " + site)


def verify_files(project, port, digests):
    for p, expected in digests.items():
        assert digest(output(["docker", "exec", project + "-app-1", "cat", "/app/" + p])) == expected, "Runtime differs: " + p
        if p.startswith("web-staging/"):
            with urlopen("http://127.0.0.1:" + str(port) + "/" + p.removeprefix("web-staging/"), timeout=5) as response:
                assert digest(response.read()) == expected, "HTTP differs: " + p


def compose_args(project, root, files):
    cmd = ["docker", "compose", "--env-file", root / "fangzhou.env", "-p", project]
    for p in files:
        cmd += ["-f", p]
    return cmd


def main():
    mode, site, commit = sys.argv[1:]
    assert mode in ("prepare", "deploy") and site in ("staging", "production")
    assert re.fullmatch("[0-9a-f]{40}", commit)
    build = "quality-ledger-20261110-" + commit[:7]
    base = Path("/opt/jubianbian-" + build)
    project, port = ("jubianbian-staging", 18000) if site == "staging" else ("jubianbian", 8000)
    root = Path("/opt") / project
    lock = Path("/run/lock/jubianbian-quality-release.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    base.mkdir(mode=0o700, exist_ok=True)
    recordfile = base / (site + "-prepared.json")
    if mode == "prepare":
        assert git("remote", "get-url", "origin").decode().strip() == "https://github.com/Maomao2317/jubianbian-next.git"
        git("fetch", "--no-tags", "origin", REF)
        assert git("rev-parse", "FETCH_HEAD").decode().strip() == commit, "Revision is not the pushed branch head"
        changed = set(git("diff", "--name-only", BASELINE, commit).decode().splitlines())
        assert changed <= ALLOWED and set(APP_FILES) <= changed, "Unexpected release files: " + str(changed - ALLOWED)
        before = inspect(project)
        assert environment(before)["JBB_BUILD_VERSION"] == "dialogue-pricing-20261109-5ed007a", "Runtime baseline changed"
        digests = {}
        for p in APP_FILES + CHECK_FILES:
            data = normalized(git("show", commit + ":" + p))
            target = base / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if p not in APP_FILES:
                continue
            if p != "backend/app/ledger_summary.py":
                expected = normalized(git("show", BASELINE + ":" + p))
                live = normalized(output(["docker", "exec", project + "-app-1", "cat", "/app/" + p]))
                assert live == expected, "Runtime baseline differs: " + p
            digests[p] = digest(data)
        image = "jubianbian-release:" + site + "-" + build
        base_image = "jubianbian-release:" + site + "-before-" + build
        run(["docker", "tag", before["Image"], base_image])
        dockerfile = base / ("Dockerfile." + site)
        dockerfile.write_text("FROM " + base_image + "\n" + "".join("COPY " + p + " /app/" + p + "\n" for p in APP_FILES))
        run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])
        cmd = ["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1", "-e", "JBB_DATA_DIR=/tmp/jbb-release-tests"]
        for p in CHECK_FILES:
            cmd += ["-v", str(base / p) + ":/app/" + p + ":ro"]
        run(cmd + ["-w", "/app/backend", "--entrypoint", "python", image, "-m", "unittest", *TESTS])
        assert fingerprint(project) == [before["Id"], before["Image"], before["State"]["StartedAt"]], "Runtime changed during prepare"
        record = dict(commit=commit, build=build, files=digests, image=image,
            imageId=json.loads(output(["docker", "image", "inspect", image]))[0]["Id"],
            previousFingerprint=fingerprint(project), isolatedRegressionPassed=True)
        recordfile.write_text(json.dumps(record, indent=2))
        print("PREPARED", site, commit, "isolated regression passed; runtime unchanged", flush=True)
        return

    record = json.loads(recordfile.read_text())
    assert record["commit"] == commit and record["previousFingerprint"] == fingerprint(project)
    assert json.loads(output(["docker", "image", "inspect", record["image"]]))[0]["Id"] == record["imageId"]
    if site == "production":
        staged = json.loads((base / "staging-deployment.json").read_text())
        assert staged["commit"] == commit and staged["verified"]
        health(18000, "staging", build)
    previous = inspect(project)
    old_env = environment(previous)
    assert old_env["JBB_ENVIRONMENT"] == site
    files = previous["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(p).parent == root for p in files)
    compose = compose_args(project, root, files)
    old_config = json.loads(output(compose + ["config", "--format", "json"]))
    overlay = root / "release-quality-ledger.yml"
    assert str(overlay) not in files, "Release already applied"
    new_compose = compose_args(project, root, files + [str(overlay)])
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = base / ("backup-" + site + "-" + stamp)
    backup.mkdir(mode=0o700)
    paths = APP_FILES + [overlay.name, ".staging-deploy-hold"]
    existed = {}
    for p in paths:
        existed[p] = (root / p).exists()
        if existed[p]:
            (backup / p).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / p, backup / p)
    record.update(backup=str(backup), previousBuild=old_env["JBB_BUILD_VERSION"])
    production_before = fingerprint("jubianbian")
    stopped = False
    try:
        for p in APP_FILES:
            assert digest((base / p).read_bytes()) == record["files"][p]
            shutil.copy2(base / p, root / p)
        overlay.write_text(json.dumps({"services": {"app": {"image": record["image"],
            "environment": {**BUDGET_ENV, "JBB_BUILD_VERSION": build}}}}, indent=2))
        if site == "staging":
            (root / ".staging-deploy-hold").write_text("Pinned " + build + "; commit " + commit + "\n")
        new_config = json.loads(output(new_compose + ["config", "--format", "json"]))
        adjusted = json.loads(json.dumps(new_config))
        adjusted["services"]["app"]["image"] = old_config["services"]["app"]["image"]
        old_ce = old_config["services"]["app"]["environment"]
        for key in [*BUDGET_ENV, "JBB_BUILD_VERSION"]:
            if key in old_ce:
                adjusted["services"]["app"]["environment"][key] = old_ce[key]
            else:
                adjusted["services"]["app"]["environment"].pop(key, None)
        assert adjusted == old_config, "Compose changed beyond image/build/budget"
        record["tasksAtSwitch"] = stop_only_when_idle(project)
        stopped = True
        run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])
        record["health"] = health(port, site, build)
        current = inspect(project)
        assert current["Image"] == record["imageId"]
        allowed_env = set(BUDGET_ENV) | {"JBB_BUILD_VERSION"}
        current_env = environment(current)
        assert {k:v for k,v in current_env.items() if k not in allowed_env} == {k:v for k,v in old_env.items() if k not in allowed_env}, "Other environment changed"
        assert all(current_env[k] == v for k,v in BUDGET_ENV.items())
        assert mounts(current) == mounts(previous), "Data mounts changed"
        assert current["HostConfig"]["PortBindings"] == previous["HostConfig"]["PortBindings"], "Ports changed"
        verify_files(project, port, record["files"])
        if site == "staging":
            assert production_before == fingerprint("jubianbian"), "Production changed during staging"
        record.update(verified=True, configurationPreserved=True, mountsPreserved=True,
            fingerprint=fingerprint(project), tasksAfter=counts(project))
        (base / (site + "-deployment.json")).write_text(json.dumps(record, indent=2))
        print("DEPLOYMENT_VERIFIED", site, commit, "tasks", record["tasksAtSwitch"], record["tasksAfter"], flush=True)
    except BaseException:
        # If the new worker has started a job, preserve it for recovery instead
        # of interrupting it in an automatic rollback.
        if stopped and inspect(project)["State"]["Running"]:
            stop_only_when_idle(project)
        for p in paths:
            if existed[p]:
                shutil.copy2(backup / p, root / p)
            elif (root / p).exists():
                (root / p).unlink()
        if stopped:
            run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            health(port, site, old_env["JBB_BUILD_VERSION"])
        print("RELEASE_FAILED", site, "backup", backup, flush=True)
        raise


if __name__ == "__main__":
    main()
