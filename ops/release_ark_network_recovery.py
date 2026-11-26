"""Release the pushed Ark transport revision through staging and production.

Run prepare|smoke|deploy staging|production SHA on the application host.
"""
import fcntl
from functools import partial
import json
import os
from pathlib import Path
import re
import sys
import time

import release_ark_response_recovery as release
from release_gpu_transport import stop_when_compute_idle

common = release.common
BASELINE = "7d17eb818658008cce829381e16686855ff96953"
REF = "codex/ark-network-recovery-20261126"
BASE_BUILDS = {
    "staging": "script-adaptation-20261125-8661355",
    "production": "visual-review-20261123-7d17eb8",
}
APP_FILES = ["backend/app/ark_transport.py", "backend/app/providers.py", "backend/requirements.txt"]
UPDATED_TESTS = {"backend/test_" + name + ".py" for name in (
    "ark_response_recovery", "batch_concurrency", "resource_limits", "script_quality",
)}
ALLOWED = set(APP_FILES) | UPDATED_TESTS | {
    "backend/test_ark_transport.py", "ops/release_ark_network_recovery.py",
    "ops/test_release_ark_network_recovery.py", "ops/release_gpu_transport.py",
}
TESTS = ["test_ark_transport", *release.TESTS]


def fetch_revision(commit):
    assert common.git("remote", "get-url", "origin").decode().strip() == (
        "https://github.com/Maomao2317/jubianbian-next.git"
    )
    common.git("fetch", "--no-tags", "origin", REF)
    assert common.git("rev-parse", "FETCH_HEAD").decode().strip() == commit
    changed = set(common.git("diff", "--name-only", BASELINE, commit).decode().splitlines())
    assert changed == ALLOWED, "Unexpected release files: " + str(changed ^ ALLOWED)


def verify_adaptation_idle(connection):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='adaptations'").fetchone()
    if exists:
        active = connection.execute(
            "SELECT count(*) FROM adaptations WHERE job_id IS NOT NULL AND job_started>=?",
            (time.time() - 45 * 60,),
        ).fetchone()[0]
        if active:
            raise RuntimeError("Release deferred: active script adaptation jobs " + str(active))


def prepare(project, site, commit, build, base):
    before = common.inspect(project)
    fingerprint = common.fingerprint(project)
    assert common.environment(before)["JBB_BUILD_VERSION"] == BASE_BUILDS[site]
    common.verify_files(project, 0, {
        path: common.digest(common.git("show", BASELINE + ":" + path))
        for path in APP_FILES if path != "backend/app/ark_transport.py"
    })
    checks = ["backend/" + name + ".py" for name in TESTS]
    hashes = {}
    for path in APP_FILES + checks:
        target = base / path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = common.normalized(common.git("show", commit + ":" + path))
        target.write_bytes(data)
        if path in APP_FILES:
            hashes[path] = common.digest(data)
    image = "jubianbian-release:" + site + "-" + build
    previous = image + "-before"
    common.run(["docker", "tag", before["Image"], previous])
    dockerfile = base / ("Dockerfile." + site)
    dockerfile.write_text("FROM " + previous + "\n" + "".join(
        "COPY " + path + " /app/" + path + "\n" for path in APP_FILES
    ))
    common.run(["docker", "build", "--network=none", "--pull=false", "-q", "-t", image, "-f", dockerfile, base])
    command = ["docker", "run", "--rm", "--network=none", "--memory=1g", "--cpus=1",
               "-e", "JBB_DATA_DIR=/tmp/jbb-network-tests"]
    for path in checks:
        command += ["-v", str(base / path) + ":/app/" + path + ":ro"]
    common.run(command + ["-w", "/app/backend", "--entrypoint", "python", image, "-m", "unittest", *TESTS])
    assert common.fingerprint(project) == fingerprint
    record = dict(commit=commit, build=build, files=hashes, image=image, imageId=release.image_id(image),
                  previousFingerprint=fingerprint, isolatedRegressionPassed=True)
    (base / (site + "-prepared.json")).write_text(json.dumps(record, indent=2))
    print("PREPARED", site, commit, flush=True)


SMOKE = '''import json,time
from app import providers as p
from app.ark_transport import _pool
results=[]
for index,key in enumerate(p.ARK_API_KEYS,1):
    for attempt in range(3):
        started=time.monotonic()
        result=p._ark_stream_response({"model":p.ARK_MODEL,"input":"Reply with OK.","max_output_tokens":64,"thinking":{"type":"disabled"}},key)
        assert result.get("status")=="completed", "Model smoke did not complete"
        results.append({"account":index,"attempt":attempt+1,"seconds":round(time.monotonic()-started,3)})
assert results, "No model accounts configured"
pool=_pool.connection_from_url(p.ARK_BASE_URL)
report={"verified":True,"calls":results,"requests":pool.num_requests,"connections":pool.num_connections}
assert report["requests"]>=len(results) and report["connections"]<report["requests"], "TLS reuse not observed"
print(json.dumps(report))
'''


def smoke(project, site, commit, base):
    record = json.loads((base / (site + "-prepared.json")).read_text())
    assert record["commit"] == commit and record["isolatedRegressionPassed"]
    assert record["previousFingerprint"] == common.fingerprint(project)
    assert record["imageId"] == release.image_id(record["image"])
    settings = common.environment(common.inspect(project))
    names = [name for name in settings if name.startswith("ARK_")]
    env = dict(os.environ)
    env.update({name: settings[name] for name in names})
    command = ["docker", "run", "--rm", "--memory=1g", "--cpus=1",
               "-e", "JBB_DATA_DIR=/tmp/jbb-network-smoke"]
    for name in names:
        command += ["-e", name]
    report = common.output(command + ["-w", "/app/backend", "--entrypoint", "python", record["image"], "-c", SMOKE], env=env)
    record["realModelSmoke"] = json.loads(report)
    assert record["realModelSmoke"]["verified"]
    assert record["previousFingerprint"] == common.fingerprint(project)
    (base / (site + "-prepared.json")).write_text(json.dumps(record, indent=2))
    print("MODEL_SMOKE_VERIFIED", site, record["realModelSmoke"], flush=True)


def deploy(project, site, port, commit, build, base):
    record = json.loads((base / (site + "-prepared.json")).read_text())
    assert record["commit"] == commit and record["realModelSmoke"]["verified"]
    release.BASE_BUILD = BASE_BUILDS[site]
    release.APP_FILES = APP_FILES
    release.stop_when_compute_idle = partial(stop_when_compute_idle, verify_idle=verify_adaptation_idle)
    release.deploy(project, site, port, commit, build, base)


def main():
    mode, site, commit = sys.argv[1:]
    assert mode in {"prepare", "smoke", "deploy"} and site in BASE_BUILDS
    assert re.fullmatch("[0-9a-f]{40}", commit)
    build = "ark-network-recovery-20261126-" + commit[:7]
    base = Path("/opt/jubianbian-" + build)
    project, port = ("jubianbian-staging", 18000) if site == "staging" else ("jubianbian", 8000)
    with Path("/run/lock/jubianbian-quality-release.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fetch_revision(commit)
        base.mkdir(mode=0o700, exist_ok=True)
        if mode == "prepare":
            prepare(project, site, commit, build, base)
        elif mode == "smoke":
            smoke(project, site, commit, base)
        else:
            deploy(project, site, port, commit, build, base)


if __name__ == "__main__":
    main()
