"""Promote the pushed Double 11 carousel through staging to production.

Run on the application host:
    python3 release_double11_carousel.py prepare COMMIT
    python3 release_double11_carousel.py deploy staging|production COMMIT
"""

import datetime
import fcntl
import json
from pathlib import Path
import re
import shutil
import sys

import release_quality_ledger as common


SOURCE_BASELINE = "8d6e90bf6b4b425dc6056b3f8f72b2491f49972a"
STAGING_BASE_BUILD = "double11-carousel-20261111-8d6e90b"
PRODUCTION_BASE_BUILD = "ark-response-recovery-60cdcb6"
PRODUCTION_SOURCE = "60cdcb651bd814e0eb8c036db1f99a84b4c995f1"
REF = "codex/repair-batch-content"
APP_FILES = [
    "web-staging/index.html",
    "web-staging/js/app.js",
    "web-staging/css/app.css",
]
EXPECTED_CHANGES = {"ops/release_double11_carousel.py"}
ENVIRONMENTS = {
    "staging": ("jubianbian-staging", 18000, STAGING_BASE_BUILD),
    "production": ("jubianbian", 8000, PRODUCTION_BASE_BUILD),
}


def image_id(image):
    return json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"]


def source_digests(commit):
    return {path: common.digest(common.git("show", commit + ":" + path)) for path in APP_FILES}


def verify_source_files(project, port, digests):
    common.verify_files(project, port, digests)


def prepare(commit, build, base):
    assert common.git("remote", "get-url", "origin").decode().strip() == "https://github.com/Maomao2317/jubianbian-next.git"
    common.git("fetch", "--no-tags", "origin", REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit, "Revision is not the pushed branch head"
    changed = set(common.git("diff", "--name-only", SOURCE_BASELINE, commit).decode().splitlines())
    assert changed == EXPECTED_CHANGES, "Unexpected release files: " + str(changed ^ EXPECTED_CHANGES)

    staging_before = common.fingerprint("jubianbian-staging")
    production_before = common.fingerprint("jubianbian")
    assert common.environment(common.inspect("jubianbian-staging"))["JBB_BUILD_VERSION"] == STAGING_BASE_BUILD
    assert common.environment(common.inspect("jubianbian"))["JBB_BUILD_VERSION"] == PRODUCTION_BASE_BUILD

    digests = source_digests(commit)
    verify_source_files("jubianbian-staging", 18000, digests)
    verify_source_files("jubianbian", 8000, source_digests(PRODUCTION_SOURCE))

    for path in APP_FILES:
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(common.git("show", commit + ":" + path))

    production_image = common.inspect("jubianbian")["Image"]
    previous = "jubianbian-release:production-before-" + build
    image = "jubianbian-release:" + build
    common.run(["docker", "tag", production_image, previous])
    dockerfile = base / "Dockerfile.release"
    dockerfile.write_text("FROM " + previous + "\n" + "".join(
        "COPY " + path + " /app/" + path + "\n" for path in APP_FILES
    ))
    common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])

    validation = """from pathlib import Path
index = Path('/app/web-staging/index.html').read_text()
script = Path('/app/web-staging/js/app.js').read_text()
styles = Path('/app/web-staging/css/app.css').read_text()
assert '20261111double11-carousel-v2' in index
assert 'campaignSlidesMarkup' in script and 'double11Offer' in script
assert 'campaign-prev' not in script and 'campaign-next' not in script
assert '}, 5000);' in script
assert 'grid-template-rows: auto auto auto' in styles
assert 'text-decoration: line-through' in styles
"""
    common.run([
        "docker", "run", "--rm", "--network=none", "--memory=256m", "--cpus=1",
        "--entrypoint", "python", image, "-c", validation,
    ])
    assert common.fingerprint("jubianbian-staging") == staging_before, "Staging changed during prepare"
    assert common.fingerprint("jubianbian") == production_before, "Production changed during prepare"

    record = {
        "commit": commit,
        "build": build,
        "files": digests,
        "image": image,
        "imageId": image_id(image),
        "stagingFingerprint": staging_before,
        "productionFingerprint": production_before,
        "isolatedValidationPassed": True,
    }
    (base / "release-prepared.json").write_text(json.dumps(record, indent=2))
    print("RELEASE_PREPARED", commit, record["imageId"], "runtimes unchanged", flush=True)


def deploy(site, commit, build, base):
    project, port, expected_build = ENVIRONMENTS[site]
    record = json.loads((base / "release-prepared.json").read_text())
    assert record["commit"] == commit and record["build"] == build
    assert record["isolatedValidationPassed"]
    assert record["imageId"] == image_id(record["image"])
    expected_fingerprint = record[site + "Fingerprint"]
    assert expected_fingerprint == common.fingerprint(project), site.title() + " changed after prepare"

    if site == "production":
        staged = json.loads((base / "staging-deployment.json").read_text())
        assert staged["commit"] == commit and staged["verified"]
        assert staged["imageId"] == record["imageId"]
        assert staged["fingerprint"] == common.fingerprint("jubianbian-staging")
        common.health(18000, "staging", build)

    root = Path("/opt") / project
    before = common.inspect(project)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == site
    assert old_env["JBB_BUILD_VERSION"] == expected_build
    config_files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(path).parent == root for path in config_files)
    compose = common.compose_args(project, root, config_files)
    old_config = json.loads(common.output(compose + ["config", "--format", "json"]))

    overlay = root / "release-double11-carousel.yml"
    deployment_files = config_files if str(overlay) in config_files else config_files + [str(overlay)]
    new_compose = common.compose_args(project, root, deployment_files)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = base / ("backup-" + site + "-" + stamp)
    backup.mkdir(mode=0o700)
    controls = [overlay]
    if site == "staging":
        controls.append(root / ".staging-deploy-hold")
    paths = [*controls, *(root / path for path in APP_FILES)]
    existed = {path for path in paths if path.exists()}
    for path in existed:
        target = backup / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    peer_project = "jubianbian" if site == "staging" else "jubianbian-staging"
    peer_before = common.fingerprint(peer_project)
    record.update(backup=str(backup), previousBuild=old_env["JBB_BUILD_VERSION"], peerBefore=peer_before)
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

        if site == "staging":
            controls[1].write_text("Pinned " + build + "; commit " + commit + "\n")
        record["tasksAtSwitch"] = common.stop_only_when_idle(project)
        stopped = True
        for path in APP_FILES:
            assert common.digest((base / path).read_bytes()) == record["files"][path]
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base / path, target)

        common.run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])
        record["health"] = common.health(port, site, build)
        current = common.inspect(project)
        assert current["Image"] == record["imageId"]
        assert common.environment(current) == dict(old_env, JBB_BUILD_VERSION=build)
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        verify_source_files(project, port, record["files"])
        assert common.fingerprint(peer_project) == peer_before, "Peer environment changed during " + site + " deployment"

        record.update(
            verified=True,
            configurationPreserved=True,
            mountsPreserved=True,
            fingerprint=common.fingerprint(project),
            peerAfter=common.fingerprint(peer_project),
            tasksAfter=common.counts(project),
        )
        (base / (site + "-deployment.json")).write_text(json.dumps(record, indent=2))
        print(site.upper() + "_DEPLOYMENT_VERIFIED", commit, record["tasksAtSwitch"], record["tasksAfter"], flush=True)
    except BaseException:
        if stopped and common.output(["docker", "ps", "-q", "--filter", "name=^/" + project + "-app-1$"]).strip():
            common.stop_only_when_idle(project)
        for path in paths if stopped else controls:
            if path in existed:
                shutil.copy2(backup / path.relative_to(root), path)
            elif path.exists():
                path.unlink()
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(port, site, expected_build)
        raise


def main():
    mode = sys.argv[1]
    if mode == "prepare":
        site = None
        commit = sys.argv[2]
    else:
        assert mode == "deploy"
        site = sys.argv[2]
        commit = sys.argv[3]
        assert site in ENVIRONMENTS
    assert re.fullmatch("[0-9a-f]{40}", commit)
    build = "double11-carousel-20261111-" + commit[:7]
    base = Path("/opt/jubianbian-" + build)
    with Path("/run/lock/jubianbian-quality-release.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base.mkdir(mode=0o700, exist_ok=True)
        if mode == "prepare":
            prepare(commit, build, base)
        else:
            deploy(site, commit, build, base)


if __name__ == "__main__":
    main()
