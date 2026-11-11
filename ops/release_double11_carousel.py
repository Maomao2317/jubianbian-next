"""Deploy the pushed Double 11 carousel to staging without touching production.

Run on the application host:
    python3 release_double11_carousel.py prepare|deploy COMMIT
"""

import datetime
import fcntl
import json
from pathlib import Path
import re
import shutil
import sys

import release_quality_ledger as common


BASELINE = "60cdcb651bd814e0eb8c036db1f99a84b4c995f1"
BASE_BUILD = "ark-response-recovery-60cdcb6"
REF = "codex/repair-batch-content"
PROJECT = "jubianbian-staging"
PORT = 18000
APP_FILES = [
    "web-staging/index.html",
    "web-staging/js/app.js",
    "web-staging/css/app.css",
]
ALLOWED = set(APP_FILES + [
    "web-staging/test-campaign-carousel.cjs",
    "ops/release_double11_carousel.py",
])


def image_id(image):
    return json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"]


def verify_source_files(project, port, digests):
    common.verify_files(project, port, digests)


def prepare(commit, build, base):
    assert common.git("remote", "get-url", "origin").decode().strip() == "https://github.com/Maomao2317/jubianbian-next.git"
    common.git("fetch", "--no-tags", "origin", REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit, "Revision is not the pushed branch head"
    changed = set(common.git("diff", "--name-only", BASELINE, commit).decode().splitlines())
    assert changed == ALLOWED, "Unexpected release files: " + str(changed ^ ALLOWED)

    before = common.fingerprint(PROJECT)
    assert common.environment(common.inspect(PROJECT))["JBB_BUILD_VERSION"] == BASE_BUILD
    baseline_digests = {path: common.digest(common.git("show", BASELINE + ":" + path)) for path in APP_FILES}
    verify_source_files(PROJECT, PORT, baseline_digests)

    digests = {}
    for path in APP_FILES:
        data = common.git("show", commit + ":" + path)
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digests[path] = common.digest(data)

    image = "jubianbian-release:staging-" + build
    previous = image + "-before"
    common.run(["docker", "tag", before[1], previous])
    dockerfile = base / "Dockerfile.staging"
    dockerfile.write_text("FROM " + previous + "\n" + "".join(
        "COPY " + path + " /app/" + path + "\n" for path in APP_FILES
    ))
    common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])

    validation = """from pathlib import Path
index = Path('/app/web-staging/index.html').read_text()
script = Path('/app/web-staging/js/app.js').read_text()
styles = Path('/app/web-staging/css/app.css').read_text()
assert '20261111double11-carousel' in index
assert 'campaignSlidesMarkup' in script and 'double11Offer' in script
assert 'campaign-carousel' in styles and '@media (max-width: 620px)' in styles
assert 'text-decoration: line-through' in styles
"""
    common.run([
        "docker", "run", "--rm", "--network=none", "--memory=256m", "--cpus=1",
        "--entrypoint", "python", image, "-c", validation,
    ])
    assert common.fingerprint(PROJECT) == before, "Runtime changed during prepare"

    record = {
        "commit": commit,
        "build": build,
        "files": digests,
        "image": image,
        "imageId": image_id(image),
        "previousFingerprint": before,
        "isolatedValidationPassed": True,
    }
    (base / "staging-prepared.json").write_text(json.dumps(record, indent=2))
    print("STAGING_PREPARED", commit, "runtime unchanged", flush=True)


def deploy(commit, build, base):
    record = json.loads((base / "staging-prepared.json").read_text())
    assert record["commit"] == commit and record["build"] == build
    assert record["isolatedValidationPassed"]
    assert record["previousFingerprint"] == common.fingerprint(PROJECT), "Staging changed after prepare"
    assert record["imageId"] == image_id(record["image"])

    root = Path("/opt") / PROJECT
    before = common.inspect(PROJECT)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == "staging"
    assert old_env["JBB_BUILD_VERSION"] == BASE_BUILD
    config_files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(path).parent == root for path in config_files)
    compose = common.compose_args(PROJECT, root, config_files)
    old_config = json.loads(common.output(compose + ["config", "--format", "json"]))

    overlay = root / "release-double11-carousel.yml"
    assert str(overlay) not in config_files
    new_compose = common.compose_args(PROJECT, root, config_files + [str(overlay)])
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

    production_before = common.fingerprint("jubianbian")
    record.update(backup=str(backup), previousBuild=old_env["JBB_BUILD_VERSION"], productionBefore=production_before)
    stopped = False
    try:
        overlay.write_text(json.dumps({"services": {"app": {
            "image": record["image"],
            "environment": {"JBB_BUILD_VERSION": build},
        }}}, indent=2))
        config = json.loads(common.output(new_compose + ["config", "--format", "json"]))
        app = config["services"]["app"]
        old_app = old_config["services"]["app"]
        assert app["image"] == record["image"]
        assert app["environment"]["JBB_BUILD_VERSION"] == build
        app["image"] = old_app["image"]
        app["environment"]["JBB_BUILD_VERSION"] = old_app["environment"]["JBB_BUILD_VERSION"]
        assert config == old_config, "Compose changed beyond image and build"

        hold.write_text("Pinned " + build + "; commit " + commit + "\n")
        record["tasksAtSwitch"] = common.stop_only_when_idle(PROJECT)
        stopped = True
        for path in APP_FILES:
            assert common.digest((base / path).read_bytes()) == record["files"][path]
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base / path, target)

        common.run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])
        record["health"] = common.health(PORT, "staging", build)
        current = common.inspect(PROJECT)
        assert current["Image"] == record["imageId"]
        assert common.environment(current) == dict(old_env, JBB_BUILD_VERSION=build)
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        verify_source_files(PROJECT, PORT, record["files"])
        assert common.fingerprint("jubianbian") == production_before, "Production changed during staging deployment"

        record.update(
            verified=True,
            configurationPreserved=True,
            mountsPreserved=True,
            fingerprint=common.fingerprint(PROJECT),
            productionAfter=common.fingerprint("jubianbian"),
            tasksAfter=common.counts(PROJECT),
        )
        (base / "staging-deployment.json").write_text(json.dumps(record, indent=2))
        print("STAGING_DEPLOYMENT_VERIFIED", commit, record["tasksAtSwitch"], record["tasksAfter"], flush=True)
    except BaseException:
        if stopped and common.output(["docker", "ps", "-q", "--filter", "name=^/" + PROJECT + "-app-1$"]).strip():
            common.stop_only_when_idle(PROJECT)
        for path in paths if stopped else [overlay, hold]:
            if path in existed:
                shutil.copy2(backup / path.relative_to(root), path)
            elif path.exists():
                path.unlink()
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(PORT, "staging", BASE_BUILD)
        raise


def main():
    mode, commit = sys.argv[1:]
    assert mode in {"prepare", "deploy"}
    assert re.fullmatch("[0-9a-f]{40}", commit)
    build = "double11-carousel-20261111-" + commit[:7]
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
