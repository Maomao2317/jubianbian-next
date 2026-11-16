"""Promote the pushed review/download patch without language delivery.

Run on the application host:
    python3 release_screenplay_language_restore.py prepare SHA
    python3 release_screenplay_language_restore.py staging SHA
    python3 release_screenplay_language_restore.py production SHA

Preparation builds and tests isolated images for both environments. Production
deployment is rejected until the same pushed revision is verified on staging.
"""
import copy
import datetime
import json
from pathlib import Path
import re
import shutil
import sys

import release_quality_ledger as common


REF = "codex/production-language-restore-20261113"
BUILD_PREFIX = "review-download-no-language-20261116-"
SITES = {
    "staging": {
        "project": "jubianbian-staging",
        "port": 18000,
        "baseline": "ec0fe613e07e6692c11b7a589c7961cb83508ad1",
        "baselineBuild": "review-download-20261116-ec0fe61",
    },
    "production": {
        "project": "jubianbian",
        "port": 8000,
        "baseline": "2905441b2731c84e90ae9c799a48d33e5c5063d5",
        "baselineBuild": "screenplay-language-rollback-20261113-2905441",
    },
}
APP_FILES = [
    "backend/app/" + name + ".py"
    for name in (
        "auth_store", "evidence", "processing", "providers", "rechecks",
        "routes", "script", "task_store", "user_view",
    )
]
APP_FILES += [
    "web-staging/css/app.css",
    "web-staging/js/api.js",
    "web-staging/js/app.js",
    "web-staging/js/config.js",
]
REMOVE_FILES = ["backend/app/translation.py"]
CHANGED_FILES = {
    "backend/app/processing.py",
    "backend/app/providers.py",
    "backend/app/routes.py",
    "backend/app/script.py",
    "backend/app/user_view.py",
    "backend/test_provider_delivery.py",
    "backend/test_script_quality.py",
    "backend/test_user_review.py",
    "backend/test_visual_description.py",
    "web-staging/css/app.css",
    "web-staging/js/app.js",
    "web-staging/js/config.js",
    "web-staging/test-user-review.cjs",
    "ops/release_screenplay_language_restore.py",
    "ops/test_release_screenplay_language_restore.py",
}


def image_id(image):
    return json.loads(common.output(["docker", "image", "inspect", image]))[0]["Id"]


def verify_config(old, new, image, build, site):
    adjusted = copy.deepcopy(new)
    app = adjusted["services"]["app"]
    old_app = old["services"]["app"]
    assert app["image"] == image
    assert app["environment"]["JBB_BUILD_VERSION"] == build
    app["image"] = old_app["image"]
    app["environment"]["JBB_BUILD_VERSION"] = old_app["environment"]["JBB_BUILD_VERSION"]
    assert adjusted == old, "Unrelated " + site + " Compose configuration changed"


def assert_known_runtime_digest(path, actual, expected):
    assert actual == expected, "Runtime baseline differs: " + path


def verify_release_content(base):
    sources = {
        path: (base / path).read_text(encoding="utf-8")
        for path in APP_FILES
    }
    combined = "\n".join(sources.values())
    assert "sourceLanguage" not in combined and "outputLanguage" not in combined
    assert "translate_delivery" not in combined and "/translation" not in combined
    assert "20261111double11" not in combined and "double11Offer" not in combined
    assert "campaignSlidesMarkup" not in combined
    assert "确认复核并开放下载" not in sources["web-staging/js/app.js"]
    assert "renderReviewSummary" in sources["web-staging/js/app.js"]
    assert "compact_clothing_detail" in sources["backend/app/script.py"]
    assert "穿搭只保留一至两个" in sources["backend/app/providers.py"]
    assert "完整保留原话" in sources["backend/app/providers.py"]
    assert not (base / "backend/app/translation.py").exists()


def parse_args(argv):
    if len(argv) != 3 or argv[1] not in {"prepare", "staging", "production"}:
        raise ValueError(
            "usage: release_screenplay_language_restore.py prepare|staging|production SHA"
        )
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


def _runtime_content(project, path):
    return common.output(["docker", "exec", project + "-app-1", "cat", "/app/" + path])


def _assert_translation_state(site, project, baseline):
    command = (
        "if test -f /app/backend/app/translation.py; "
        "then cat /app/backend/app/translation.py; else printf __ABSENT__; fi"
    )
    actual = common.output(["docker", "exec", project + "-app-1", "sh", "-c", command])
    if site == "production":
        assert actual == b"__ABSENT__", "Production unexpectedly contains translation.py"
    else:
        expected = common.git("show", baseline + ":backend/app/translation.py")
        assert common.digest(actual) == common.digest(expected), "Staging translation baseline differs"


def _validate_image(image):
    validation = (
        "from pathlib import Path\n"
        "paths = [\n"
        + "".join("    '/app/" + path + "',\n" for path in APP_FILES)
        + "]\n"
        "combined = '\\n'.join(Path(path).read_text() for path in paths)\n"
        "assert 'sourceLanguage' not in combined and 'outputLanguage' not in combined\n"
        "assert 'translate_delivery' not in combined and '/translation' not in combined\n"
        "assert '确认复核并开放下载' not in combined\n"
        "assert 'renderReviewSummary' in combined\n"
        "assert 'compact_clothing_detail' in combined\n"
        "assert not Path('/app/backend/app/translation.py').exists()\n"
    )
    common.run([
        "docker", "run", "--rm", "--network=none", "--memory=256m", "--cpus=1",
        "--entrypoint", "python", image, "-c", validation,
    ])


def prepare(commit, build, base):
    fetch_revision(commit)
    changed = set(common.git(
        "diff", "--name-only", SITES["production"]["baseline"], commit
    ).decode().splitlines())
    assert changed == CHANGED_FILES, "Unexpected release files: " + str(
        changed.symmetric_difference(CHANGED_FILES)
    )

    fingerprints = {}
    live = {}
    for site, settings in SITES.items():
        project = settings["project"]
        fingerprints[site] = common.fingerprint(project)
        live[site] = common.inspect(project)
        env = common.environment(live[site])
        assert env["JBB_ENVIRONMENT"] == site
        assert env["JBB_BUILD_VERSION"] == settings["baselineBuild"], (
            site.capitalize() + " runtime baseline changed"
        )
        common.health(settings["port"], site, settings["baselineBuild"])
        for path in APP_FILES:
            expected = common.git("show", settings["baseline"] + ":" + path)
            assert_known_runtime_digest(
                path, common.digest(_runtime_content(project, path)), common.digest(expected)
            )
        _assert_translation_state(site, project, settings["baseline"])

    checks = test_files(commit)
    digests = {}
    for path in APP_FILES + checks:
        data = common.normalized(common.git("show", commit + ":" + path))
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path in APP_FILES:
            digests[path] = common.digest(data)
    verify_release_content(base)

    images = {}
    for site in SITES:
        image = "jubianbian-release:" + site + "-" + build
        before_image = image + "-before"
        common.run(["docker", "tag", live[site]["Image"], before_image])
        dockerfile = base / ("Dockerfile." + site)
        dockerfile.write_text(
            "FROM " + before_image + "\n"
            + "".join("COPY " + path + " /app/" + path + "\n" for path in APP_FILES)
            + "RUN rm -f /app/backend/app/translation.py\n"
        )
        common.run([
            "docker", "build", "--network=none", "--pull=false", "-q", "-t", image,
            "-f", dockerfile, base,
        ])
        _validate_image(image)

        command = [
            "docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1",
            "-e", "JBB_DATA_DIR=/tmp/jbb-release-tests",
        ]
        for path in checks:
            command += ["-v", str(base / path) + ":/app/" + path + ":ro"]
        modules = [Path(path).stem for path in checks]
        common.run(command + [
            "-w", "/app/backend", "--entrypoint", "python", image,
            "-m", "unittest", *modules,
        ])
        images[site] = {"name": image, "id": image_id(image), "regressionPassed": True}

    for site, settings in SITES.items():
        assert common.fingerprint(settings["project"]) == fingerprints[site], (
            site.capitalize() + " runtime changed during preparation"
        )
    record = {
        "commit": commit,
        "build": build,
        "files": digests,
        "removedFiles": REMOVE_FILES,
        "images": images,
        "fingerprintsBefore": fingerprints,
        "fullIsolatedRegressionPassed": True,
    }
    (base / "release-prepared.json").write_text(json.dumps(record, indent=2))
    print(
        "PREPARED staging+production", commit,
        "full isolated regression passed; both runtimes unchanged", flush=True,
    )


def _restore(paths, existed, backup, root):
    for path in paths:
        if path in existed:
            shutil.copy2(backup / path.relative_to(root), path)
        elif path.exists():
            path.unlink()


def deploy(site, commit, build, base):
    fetch_revision(commit)
    settings = SITES[site]
    project = settings["project"]
    port = settings["port"]
    record = json.loads((base / "release-prepared.json").read_text())
    assert record["commit"] == commit and record["build"] == build
    assert record["fullIsolatedRegressionPassed"]
    image_record = record["images"][site]
    assert image_record["regressionPassed"]
    assert image_record["id"] == image_id(image_record["name"]), "Prepared image changed"

    if site == "staging":
        assert record["fingerprintsBefore"][site] == common.fingerprint(project), (
            "Staging changed after preparation"
        )
        protected_fingerprint = common.fingerprint(SITES["production"]["project"])
    else:
        staged = json.loads((base / "staging-deployment.json").read_text())
        assert staged["commit"] == commit and staged["build"] == build and staged["verified"]
        assert staged["fingerprint"] == common.fingerprint(SITES["staging"]["project"]), (
            "Verified staging runtime changed"
        )
        common.health(SITES["staging"]["port"], "staging", build)
        assert record["fingerprintsBefore"][site] == common.fingerprint(project), (
            "Production changed after preparation"
        )
        protected_fingerprint = staged["fingerprint"]

    root = Path("/opt") / project
    before = common.inspect(project)
    old_env = common.environment(before)
    assert old_env["JBB_ENVIRONMENT"] == site
    assert old_env["JBB_BUILD_VERSION"] == settings["baselineBuild"]
    files = before["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
    assert all(Path(path).parent == root for path in files), "Unexpected Compose path"
    compose = common.compose_args(project, root, files)
    overlay = root / "release-review-download-no-language.yml"
    assert str(overlay) not in files, "Review/download release overlay is already active"
    new_compose = common.compose_args(project, root, files + [str(overlay)])
    old_config = json.loads(common.output(compose + ["config", "--format", "json"]))

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = base / ("backup-" + site + "-" + stamp)
    backup.mkdir(mode=0o700)
    paths = [overlay, *(root / path for path in APP_FILES + REMOVE_FILES)]
    hold = root / ".staging-deploy-hold"
    if site == "staging":
        paths.append(hold)
    existed = {path for path in paths if path.exists()}
    for path in existed:
        target = backup / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    deployment = copy.deepcopy(record)
    deployment.update(site=site, backup=str(backup), previousBuild=old_env["JBB_BUILD_VERSION"])
    stopped = False
    try:
        overlay.write_text(json.dumps({"services": {"app": {
            "image": image_record["name"],
            "environment": {"JBB_BUILD_VERSION": build},
        }}}, indent=2))
        config = json.loads(common.output(new_compose + ["config", "--format", "json"]))
        verify_config(old_config, config, image_record["name"], build, site)
        if site == "staging":
            hold.write_text("Pinned " + build + "; commit " + commit + "\n")

        deployment["tasksAtSwitch"] = common.stop_only_when_idle(project)
        stopped = True
        for path in APP_FILES:
            assert common.digest((base / path).read_bytes()) == record["files"][path]
            shutil.copy2(base / path, root / path)
        for path in REMOVE_FILES:
            (root / path).unlink(missing_ok=True)
        common.run(new_compose + ["up", "-d", "--no-build", "--no-deps", "app"])

        deployment["health"] = common.health(port, site, build)
        current = common.inspect(project)
        assert current["Image"] == image_record["id"]
        assert common.environment(current) == dict(old_env, JBB_BUILD_VERSION=build)
        assert common.mounts(current) == common.mounts(before)
        assert current["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"]
        common.verify_files(project, port, record["files"])
        assert common.output([
            "docker", "exec", project + "-app-1", "sh", "-c",
            "test ! -f /app/backend/app/translation.py && echo absent",
        ]).decode().strip() == "absent"
        _validate_image(image_record["name"])

        protected_project = (
            SITES["production"]["project"] if site == "staging"
            else SITES["staging"]["project"]
        )
        assert common.fingerprint(protected_project) == protected_fingerprint, (
            protected_project + " changed during " + site + " deployment"
        )
        deployment.update(
            verified=True,
            configurationPreserved=True,
            mountsPreserved=True,
            portsPreserved=True,
            noLanguageDeliveryVerified=True,
            fingerprint=common.fingerprint(project),
            protectedFingerprint=common.fingerprint(protected_project),
            tasksAfter=common.counts(project),
        )
        (base / (site + "-deployment.json")).write_text(json.dumps(deployment, indent=2))
        print(
            "DEPLOYMENT_VERIFIED", site, commit,
            deployment["tasksAtSwitch"], deployment["tasksAfter"], flush=True,
        )
    except BaseException:
        if stopped and common.inspect(project)["State"]["Running"]:
            try:
                common.stop_only_when_idle(project)
            except RuntimeError:
                deployment["rollbackDeferredActiveTasks"] = True
                deployment["failure"] = (
                    "New tasks became active; automatic rollback deferred to avoid interruption"
                )
                (base / (site + "-deployment.json")).write_text(
                    json.dumps(deployment, indent=2)
                )
                raise
        _restore(paths if stopped else [overlay, *([hold] if site == "staging" else [])],
                 existed, backup, root)
        if stopped:
            common.run(compose + ["up", "-d", "--no-build", "--no-deps", "app"])
            common.health(port, site, settings["baselineBuild"])
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
            deploy(mode, commit, build, base)


if __name__ == "__main__":
    main()
