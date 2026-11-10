"""Release only the pushed GPU/transfer patch; never interrupt recognition.

Usage: python3 release_gpu_transport.py prepare|deploy staging|production SHA
GPU worker verification is recorded separately before enabling page transport.
"""
from pathlib import Path
import datetime
import fcntl
import json
import re
import shutil
import sqlite3
import sys

import release_quality_ledger as common

BASELINE = "4095edc166238f7ab00f94d6b1a51807bde42f48"
APP_FILES = ["backend/app/" + n + ".py" for n in (
    "config", "evidence", "remote_asr", "remote_ocr", "remote_circuit", "storage", "gpu_transport", "ocr_pages")]
NEW_FILES = {"backend/app/gpu_transport.py", "backend/app/ocr_pages.py"}
TESTS = ["test_" + n for n in (
    "gpu_transport", "ocr_pages", "remote_asr", "remote_ocr", "remote_circuit", "storage_transfer",
    "evidence_frames", "ocr_batch", "ocr_dialogue_integrity", "spoken_subtitles", "recognition_cache",
    "quality_capacity", "user_review", "dialogue_export", "admin_ledger", "route_queue")]
CHECK_FILES = ["backend/" + n + ".py" for n in TESTS]
ALLOWED = set(APP_FILES + CHECK_FILES + ["ops/" + p for p in (
    "gpu_ocr_server.py", "manage_gpu_ocr.py", "test_gpu_ocr_pages.py", "release_gpu_transport.py",
    "gpu-transport-release-2026-11-10.md", "test_release_gpu_transport.py")])
ENV = {"JBB_OCR_REMOTE_PAGE_TRANSPORT": "1", "JBB_ASR_REMOTE_TIMEOUT_SECONDS": "180",
       "JBB_OCR_REMOTE_TIMEOUT_SECONDS": "240", "JBB_MEDIA_DOWNLOAD_IDLE_TIMEOUT_SECONDS": "30"}


def stop_when_compute_idle(project):
    """Keep direct object uploads intact, but never interrupt a claimed job.

    S3 PUTs bypass the application. Their durable reservations survive a restart
    and the existing upload reconciler accepts objects completed during it.
    Local uploads, ready jobs and every running job still block the switch.
    The writer lock prevents claims/completion transitions during the stop.
    """
    connection = sqlite3.connect(str(common.database(project)), timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = dict(connection.execute("SELECT status,count(*) FROM tasks GROUP BY status"))
        active = connection.execute(
            """SELECT count(*) FROM tasks
               WHERE status IN ('queued', 'running', 'uploading')
               AND NOT (status = 'queued' AND COALESCE(stage, '') = 'uploading'
                        AND COALESCE(stored_path, '') GLOB 's3://?*')"""
        ).fetchone()[0]
        if active:
            raise RuntimeError("Release deferred: active compute/local uploads " + str(active))
        common.run(["docker", "stop", "--time", "5", project + "-app-1"], timeout=15)
        return state
    finally:
        connection.rollback()
        connection.close()


def main():
    mode, site, commit = sys.argv[1:]
    assert mode in {"prepare", "deploy"} and site in {"staging", "production"}
    assert re.fullmatch("[0-9a-f]{40}", commit)
    build = "gpu-transport-20261110-" + commit[:7]
    base = Path("/opt/jubianbian-" + build)
    project, port = ("jubianbian-staging", 18000) if site == "staging" else ("jubianbian", 8000)
    root = Path("/opt") / project
    lock = Path("/run/lock/jubianbian-quality-release.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    base.mkdir(mode=0o700, exist_ok=True)
    prepared = base / (site + "-prepared.json")
    if mode == "prepare":
        assert common.git("remote", "get-url", "origin").decode().strip() == "https://github.com/Maomao2317/jubianbian-next.git"
        common.git("fetch", "--no-tags", "origin", common.REF)
        assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit
        changed = set(common.git("diff", "--name-only", BASELINE, commit).decode().splitlines())
        assert changed <= ALLOWED and set(APP_FILES) <= changed, "Unexpected release files"
        before = common.inspect(project)
        assert common.environment(before)["JBB_BUILD_VERSION"] == "quality-ledger-20261110-4095edc"
        hashes = {}
        for p in APP_FILES + CHECK_FILES:
            data = common.normalized(common.git("show", commit + ":" + p))
            target = base / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if p not in APP_FILES:
                continue
            if p not in NEW_FILES:
                assert common.normalized(common.output(["docker", "exec", project + "-app-1", "cat", "/app/" + p])) == common.normalized(common.git("show", BASELINE + ":" + p)), "Runtime baseline differs: " + p
            hashes[p] = common.digest(data)
        image = "jubianbian-release:" + site + "-" + build
        previous_image = image + "-before"
        common.run(["docker", "tag", before["Image"], previous_image])
        dockerfile = base / ("Dockerfile." + site)
        dockerfile.write_text("FROM " + previous_image + "\n" + "".join("COPY " + p + " /app/" + p + "\n" for p in APP_FILES))
        common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])
        command = ["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1", "-e", "JBB_DATA_DIR=/tmp/jbb-release-tests"]
        for p in CHECK_FILES:
            command += ["-v", str(base / p) + ":/app/" + p + ":ro"]
        common.run(command + ["-w", "/app/backend", "--entrypoint", "python", image, "-m", "unittest", *TESTS])
        assert common.fingerprint(project) == [before["Id"], before["Image"], before["State"]["StartedAt"]]
        prepared.write_text(json.dumps(dict(commit=commit, build=build, files=hashes, image=image,
            imageId=json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"],
            previousFingerprint=common.fingerprint(project), isolatedRegressionPassed=True), indent=2))
        print("PREPARED", site, commit, flush=True)
        return

    record = json.loads(prepared.read_text())
    assert record["commit"] == commit and record["previousFingerprint"] == common.fingerprint(project)
    assert json.loads(common.output(["docker", "image", "inspect", record["image"]]))[0]["Id"] == record["imageId"]
    gpu = json.loads((base / "gpu-verification.json").read_text())
    assert gpu["commit"] == commit and gpu["verified"] and gpu["protocol"] == "jbb-ocr-pages-1"
    # Route both sites to the verified worker. Keep their existing gateway host.
    from urllib.parse import urlsplit, urlunsplit
    before = common.inspect(project)
    old_env = common.environment(before)
    update = dict(ENV, JBB_BUILD_VERSION=build)
    for kind in ("ASR", "OCR"):
        key = "JBB_" + kind + "_REMOTE_URL"
        url = urlsplit(old_env[key])
        update[key] = urlunsplit((url.scheme, url.hostname + ":" + str(gpu["gatewayPort"]), url.path, "", ""))
    if site == "production":
        staged = json.loads((base / "staging-deployment.json").read_text())
        assert staged["commit"] == commit and staged["verified"]
        common.health(18000, "staging", build)
    files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(p).parent == root for p in files)
    compose = common.compose_args(project, root, files)
    old_config = json.loads(common.output(compose + ["config", "--format", "json"]))
    overlay = root / "release-gpu-transport.yml"
    assert str(overlay) not in files
    new_compose = common.compose_args(project, root, files + [str(overlay)])
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
    production_before = common.fingerprint("jubianbian")
    stopped = False
    try:
        for p in APP_FILES:
            assert common.digest((base / p).read_bytes()) == record["files"][p]
            shutil.copy2(base / p, root / p)
        overlay.write_text(json.dumps({"services": {"app": {"image": record["image"], "environment": update}}}, indent=2))
        if site == "staging":
            (root / ".staging-deploy-hold").write_text("Pinned " + build + "; commit " + commit + "\n")
        config = json.loads(common.output(new_compose + ["config", "--format", "json"]))
        adjusted = json.loads(json.dumps(config))
        adjusted["services"]["app"]["image"] = old_config["services"]["app"]["image"]
        for k in update:
            target = adjusted["services"]["app"]["environment"]
            original = old_config["services"]["app"]["environment"]
            if k in original:
                target[k] = original[k]
            else:
                target.pop(k, None)
        assert adjusted == old_config, "Unrelated compose changes"
        record["tasksAtSwitch"] = stop_when_compute_idle(project)
        stopped = True
        common.run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])
        record["health"] = common.health(port, site, build)
        current = common.inspect(project)
        assert current["Image"] == record["imageId"]
        current_env = common.environment(current)
        assert {k:v for k,v in current_env.items() if k not in update} == {k:v for k,v in old_env.items() if k not in update}
        assert all(current_env[k] == v for k,v in update.items())
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        common.verify_files(project, port, record["files"])
        if site == "staging":
            assert common.fingerprint("jubianbian") == production_before
        record.update(verified=True, configurationPreserved=True, mountsPreserved=True,
            fingerprint=common.fingerprint(project), tasksAfter=common.counts(project))
        (base / (site + "-deployment.json")).write_text(json.dumps(record, indent=2))
        print("DEPLOYMENT_VERIFIED", site, commit, "tasks", record["tasksAtSwitch"], record["tasksAfter"], flush=True)
    except BaseException:
        if stopped and common.inspect(project)["State"]["Running"]:
            stop_when_compute_idle(project)
        for p in paths:
            if existed[p]:
                shutil.copy2(backup / p, root / p)
            elif (root / p).exists():
                (root / p).unlink()
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(port, site, old_env["JBB_BUILD_VERSION"])
        print("RELEASE_FAILED", site, "backup", backup, flush=True)
        raise


if __name__ == "__main__":
    main()
