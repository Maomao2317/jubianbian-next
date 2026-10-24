"""Reuse the reviewed idle-only release driver for the Ark cost release."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


def main():
    commit = sys.argv[sys.argv.index('--commit') + 1]
    root = Path(__file__).parent
    driver_path = root / 'deploy-stability-completeness.py'
    repo = '/opt/jubianbian-github-check'
    expected = subprocess.check_output(['git', '-c', 'safe.directory='+repo, '-C', repo,
                                        'show', commit+':ops/deploy-stability-completeness.py'])
    assert driver_path.read_bytes() == expected
    spec = importlib.util.spec_from_file_location('release', driver_path)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    driver.BASELINE = 'b2db6e211ac127ca694817f865f7834fe141828c'
    driver.BUILD = 'ark-cost-control-20261024-r1'
    # The shared driver verifies its own bytes; this wrapper is also checked
    # against the pushed revision before loading it on the host.
    wrapper = subprocess.check_output(['git', '-c', 'safe.directory='+repo, '-C', repo,
                                       'show', commit+':ops/deploy-ark-cost-control.py'])
    assert Path(__file__).read_bytes() == wrapper
    driver.APP_CHANGES = {'backend/app/config.py', 'backend/app/providers.py', 'backend/app/processing.py'}
    driver.DEFAULTS = {'ARK_VIDEO_FPS': '0.5', 'ARK_REVIEW_VIDEO_FPS': '2.0'}
    if sys.argv[1] == 'production':
        report = json.loads(Path('/opt/jbb-video-reliability-20261023-'+commit[:12],
                                 'live-verification.json').read_text())
        assert report['sourceCommit'] == commit and report['passed']
        assert report['businessDataUnchanged'] and report['localReview']['sourceTimelineValidated']
    driver.main()


if __name__ == '__main__':
    main()
