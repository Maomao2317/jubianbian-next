"""Remove screenplay language delivery from production without touching staging.

Run on the application host:
    python3 release_screenplay_language_restore.py prepare SHA
    python3 release_screenplay_language_restore.py deploy SHA
"""
import copy
import datetime
import json
from pathlib import Path
import re
import shutil
import sys

import release_quality_ledger as common

BASELINE = "5856c8e4f8d73713dddee4bb6796373691485e95"
BASE_BUILD = "carousel-removal-20261113-5856c8e"
REF = "codex/production-language-restore-20261113"
PROJECT = "jubianbian"
PORT = 8000
STAGING_PROJECT = "jubianbian-staging"
STAGING_PORT = 18000
STAGING_BUILD = BASE_BUILD
APP_FILES = [
    "backend/app/" + name + ".py"
    for name in ("auth_store", "evidence", "processing", "providers", "rechecks", "routes", "script", "task_store", "user_view")
]
APP_FILES += ["web-staging/css/app.css", "web-staging/js/api.js", "web-staging/js/app.js"]
REMOVE_FILES = ["backend/app/translation.py"]
TEST_FILES = [
    "backend/test_large_batch.py",
    "backend/test_provider_delivery.py",
    "backend/test_rechecks.py",
    "backend/test_route_queue.py",
    "backend/test_upload_flow.py",
]
SUPPORT_FILES = ["ops/release_screenplay_language_restore.py", "ops/test_release_screenplay_language_restore.py"]
ALLOWED = set(APP_FILES + REMOVE_FILES + TEST_FILES + [
    "backend/test_translation.py", "web-staging/test-batch-upload.cjs", *SUPPORT_FILES,
])


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
    assert adjusted == old, "Unrelated production Compose configuration changed"


def assert_known_runtime_digest(path, actual, known):
    assert actual in known, "Runtime differs from known revisions: " + path


def verify_release_content(base):
    app = (base / "web-staging/js/app.js").read_text(encoding="utf-8")
    api = (base / "web-staging/js/api.js").read_text(encoding="utf-8")
    routes = (base / "backend/app/routes.py").read_text(encoding="utf-8")
    processing = (base / "backend/app/processing.py").read_text(encoding="utf-8")
    assert "sourceLanguage" not in app and "outputLanguage" not in app
    assert "sourceLanguage" not in api and "outputLanguage" not in api
    assert "20261111double11" not in app and "double11Offer" not in app
    assert "campaignSlidesMarkup" not in app
    assert "translate_delivery" not in processing and "/translation" not in routes
    assert not (base / "backend/app/translation.py").exists()


def parse_args(argv):
    if len(argv) != 3 or argv[1] not in {"prepare", "deploy"}:
        raise ValueError("usage: release_screenplay_language_restore.py prepare|deploy SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", argv[2]):
        raise ValueError("SHA must be a full lowercase Git commit id")
    return argv[1], argv[2]


def fetch_revision(commit):
    assert common.git("remote", "get-url", "origin").decode().strip() == (
        "https://github.com/Maomao2317/jubianbian-next.git"
    )
    common.git("fetch", "--no-tags", "origin", REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit, "Revision is not the pushed branch head"


def prepare(commit, build, base):
    fetch_revision(commit)
    changed = set(common.git("diff", "--name-only", BASELINE, commit).decode().splitlines())
    assert changed == ALLOWED, "Unexpected release files: " + str(changed.symmetric_difference(ALLOWED))

    production_before = common.fingerprint(PROJECT)
    staging_before = common.fingerprint(STAGING_PROJECT)
    old = common.inspect(PROJECT)
    old_env = common.environment(old)
    assert old_env["JBB_ENVIRONMENT"] == "production"
    assert old_env["JBB_BUILD_VERSION"] == BASE_BUILD, "Production runtime baseline changed"
    assert common.environment(common.inspect(STAGING_PROJECT))["JBB_BUILD_VERSION"] == STAGING_BUILD
    common.health(PORT, "production", BASE_BUILD)
    common.health(STAGING_PORT, "staging", STAGING_BUILD)

    for path in APP_FILES:
        live = common.output(["docker", "exec", PROJECT + "-app-1", "cat", "/app/" + path])
        known = {
            common.digest(common.git("show", revision + ":" + path))
            for revision in (BASELINE, commit)
        }
        assert_known_runtime_digest(path, common.digest(live), known)
    translation = common.output([
        "docker", "exec", PROJECT + "-app-1", "sh", "-c",
        "if test -f /app/backend/app/translation.py; then cat /app/backend/app/translation.py; else printf __ABSENT__; fi",
    ])
    if translation != b"__ABSENT__":
        assert common.digest(translation) == common.digest(common.git("show", BASELINE + ":backend/app/translation.py"))

    digests = {}
    for path in APP_FILES + REMOVE_FILES + TEST_FILES:
        try:
            data = common.normalized(common.git("show", commit + ":" + path))
        except Exception:
            if path in REMOVE_FILES:
                continue
            raise
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path in APP_FILES:
            digests[path] = common.digest(data)
    verify_release_content(base)

    image = "jubianbian-release:production-" + build
    before_image = image + "-before"
    common.run(["docker", "tag", old["Image"], before_image])
    dockerfile = base / "Dockerfile.production"
    dockerfile.write_text(
        "FROM " + before_image + "\n"
        + "".join("COPY " + path + " /app/" + path + "\n" for path in APP_FILES)
        + "RUN rm -f /app/backend/app/translation.py\n"
    )
    common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])

    validation = (
        "from pathlib import Path\n"
        "app = Path('/app/web-staging/js/app.js').read_text()\n"
        "api = Path('/app/web-staging/js/api.js').read_text()\n"
        "index = Path('/app/web-staging/index.html').read_text()\n"
        "routes = Path('/app/backend/app/routes.py').read_text()\n"
        "processing = Path('/app/backend/app/processing.py').read_text()\n"
        "assert 'sourceLanguage' not in app and 'outputLanguage' not in app\n"
        "assert 'sourceLanguage' not in api and 'outputLanguage' not in api\n"
        "assert '20261111double11' not in app + index and 'double11Offer' not in app\n"
        "assert 'campaignSlidesMarkup' not in app\n"
        "assert 'translate_delivery' not in processing and '/translation' not in routes\n"
        "assert not Path('/app/backend/app/translation.py').exists()\n"
    )
    common.run([
        "docker", "run", "--rm", "--network=none", "--memory=256m", "--cpus=1",
        "--entrypoint", "python", image, "-c", validation,
    ])

    checks = [path for path in common.git("ls-tree", "-rz", "--name-only", commit, "--", "backend").decode().split("\0")
              if re.fullmatch(r"backend/test_[^/]+\.py", path)]
    for path in checks:
        if path not in TEST_FILES:
            continue
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(common.normalized(common.git("show", commit + ":" + path)))
    modules = [Path(path).stem for path in TEST_FILES]
    command = ["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1", "-e", "JBB_DATA_DIR=/tmp/jbb-release-tests"]
    for path in TEST_FILES:
        command += ["-v", str(base / path) + ":/app/" + path + ":ro"]
    common.run(command + ["-w", "/app/backend", "--entrypoint", "python", image, "-m", "unittest", *modules])

    assert common.fingerprint(PROJECT) == production_before, "Production runtime changed during prepare"
    assert common.fingerprint(STAGING_PROJECT) == staging_before, "Staging runtime changed during prepare"
    record = {
        "commit": commit,
        "build": build,
        "files": digests,
        "removedFiles": REMOVE_FILES,
        "image": image,
        "imageId": image_id(image),
        "productionFingerprintBefore": production_before,
        "stagingFingerprint": staging_before,
        "isolatedRegressionPassed": True,
    }
    (base / "production-prepared.json").write_text(json.dumps(record, indent=2))
    print("PREPARED production", commit, "isolated regression passed; both runtimes unchanged", flush=True)


def deploy(commit, build, base):
    fetch_revision(commit)
    record = json.loads((base / "production-prepared.json").read_text())
    assert record["commit"] == commit and record["build"] == build and record["isolatedRegressionPassed"]
    assert record["productionFingerprintBefore"] == common.fingerprint(PROJECT), "Production changed after prepare"
    assert record["stagingFingerprint"] == common.fingerprint(STAGING_PROJECT), "Staging changed after prepare"
    assert record["imageId"] == image_id(record["image"]), "Prepared image changed"

    root = Path("/opt") / PROJECT
    before = common.inspect(PROJECT)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == "production" and old_env["JBB_BUILD_VERSION"] == BASE_BUILD
    files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(path).parent == root for path in files), "Unexpected production Compose path"
    compose = common.compose_args(PROJECT, root, files)
    overlay = root / "release-screenplay-language-restore.yml"
    assert str(overlay) not in files, "A production rollback overlay is already active"
    new_compose = common.compose_args(PROJECT, root, files + [str(overlay)])
    old_config = json.loads(common.output(compose + ["config", "--format", "json"]))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = base / ("backup-production-" + stamp)
    backup.mkdir(mode=0o700)
    paths = [overlay, *(root / path for path in APP_FILES + REMOVE_FILES)]
    existed = {path for path in paths if path.exists()}
    for path in existed:
        target = backup / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    record.update(backup=str(backup), previousBuild=old_env["JBB_BUILD_VERSION"])
    stopped = False
    try:
        overlay.write_text(json.dumps({"services": {"app": {
            "image": record["image"], "environment": {"JBB_BUILD_VERSION": build},
        }}}, indent=2))
        config = json.loads(common.output(new_compose + ["config", "--format", "json"]))
        verify_config(old_config, config, record["image"], build)

        record["tasksAtSwitch"] = common.stop_only_when_idle(PROJECT)
        stopped = True
        for path in APP_FILES:
            assert common.digest((base / path).read_bytes()) == record["files"][path]
            shutil.copy2(base / path, root / path)
        for path in REMOVE_FILES:
            (root / path).unlink(missing_ok=True)
        common.run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])

        record["health"] = common.health(PORT, "production", build)
        current = common.inspect(PROJECT)
        assert current["Image"] == record["imageId"]
        assert common.environment(current) == dict(old_env, JBB_BUILD_VERSION=build)
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        common.verify_files(PROJECT, PORT, record["files"])
        assert common.output(["docker", "exec", PROJECT + "-app-1", "sh", "-c", "test ! -f /app/backend/app/translation.py && echo absent"]).decode().strip() == "absent"
        assert common.fingerprint(STAGING_PROJECT) == record["stagingFingerprint"], "Staging runtime changed during production deployment"
        assert common.health(STAGING_PORT, "staging", STAGING_BUILD)["build"] == STAGING_BUILD

        record.update(
            verified=True,
            configurationPreserved=True,
            mountsPreserved=True,
            productionFingerprintAfter=common.fingerprint(PROJECT),
            stagingFingerprintAfter=common.fingerprint(STAGING_PROJECT),
            tasksAfter=common.counts(PROJECT),
        )
        (base / "production-deployment.json").write_text(json.dumps(record, indent=2))
        print("PRODUCTION_DEPLOYMENT_VERIFIED", commit, record["tasksAtSwitch"], record["tasksAfter"], flush=True)
    except BaseException:
        if stopped and common.inspect(PROJECT)["State"]["Running"]:
            try:
                common.stop_only_when_idle(PROJECT)
            except RuntimeError:
                record["rollbackDeferredActiveTasks"] = True
                record["failure"] = "New production tasks became active; automatic rollback deferred to avoid interruption"
                (base / "production-deployment.json").write_text(json.dumps(record, indent=2))
                raise
        for path in paths if stopped else [overlay]:
            if path in existed:
                shutil.copy2(backup / path.relative_to(root), path)
            elif path.exists():
                path.unlink()
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(PORT, "production", BASE_BUILD)
        raise


def main(argv=None):
    import fcntl

    mode, commit = parse_args(sys.argv if argv is None else argv)
    build = "screenplay-language-rollback-20261113-" + commit[:7]
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
