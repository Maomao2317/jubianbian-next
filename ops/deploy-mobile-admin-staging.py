"""Deploy the published mobile admin UI using the verified staging release workflow."""
import importlib.util
from pathlib import Path
import sys


def main(commit):
    path = Path(__file__).with_name('deploy-customer-copy-staging.py')
    spec = importlib.util.spec_from_file_location('staging_release', path)
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    release.BASE_COMMIT = '2894b1a'
    release.BUILD = 'mobile-admin-20261019-r1'
    release.IMAGE = 'jubianbian-release:' + release.BUILD
    release.FILES = {'web-staging/css/app.css', 'web-staging/index.html',
                     'web-staging/js/app.js', 'web-staging/test-user-review.cjs'}
    release.SELF = 'ops/deploy-mobile-admin-staging.py'
    release.SUPPORT_FILES = {'ops/deploy-customer-copy-staging.py'}
    release.health('customer-copy-20261019-r1')
    release.main(commit)


if __name__ == '__main__':
    main(sys.argv[1])
