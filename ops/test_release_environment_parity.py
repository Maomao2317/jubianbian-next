"""Release guards for promoting the same pushed image to both sites."""
import unittest
from unittest.mock import patch
import sys
import types

if sys.platform == "win32" and "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import release_environment_parity as release


class EnvironmentParityReleaseTests(unittest.TestCase):
    def test_site_baselines_and_release_scope(self):
        self.assertEqual(release.SITES["staging"], (
            "jubianbian-staging", 18000, "screenplay-fidelity-20261119-8d706c8"))
        self.assertEqual(release.SITES["production"], (
            "jubianbian", 8000, "character-intro-split-20261116-cfd1b08"))
        self.assertEqual(release.BASELINE, "8d706c8c9c80fec11579f46fe58f253519f3e114")
        self.assertEqual(len(release.RELEASE_FILES), 5)

    def test_manifest_contains_runtime_code_not_frontend_tests(self):
        paths = ("backend/app/config.py\0backend/app/translation.py\0"
                 "web-staging/index.html\0web-staging/css/app.css\0"
                 "web-staging/js/app.js\0web-staging/test-user-review.cjs\0")
        with patch.object(release.common, "git", return_value=paths.encode()):
            self.assertEqual(release.app_files("a" * 40), [
                "backend/app/config.py", "backend/app/translation.py",
                "web-staging/css/app.css", "web-staging/index.html",
                "web-staging/js/app.js",
            ])

    def test_compose_change_allows_image_and_build_only(self):
        old = {"services": {"app": {"image": "before", "environment": {
            "JBB_BUILD_VERSION": "old", "JBB_ENVIRONMENT": "production", "KEEP": "same"},
            "ports": ["8000:8000"]}}}
        new = {"services": {"app": {"image": "after", "environment": {
            "JBB_BUILD_VERSION": "new", "JBB_ENVIRONMENT": "production", "KEEP": "same"},
            "ports": ["8000:8000"]}}}
        release.verify_config(old, new, "after", "new")
        new["services"]["app"]["ports"] = ["18000:8000"]
        with self.assertRaises(AssertionError):
            release.verify_config(old, new, "after", "new")


if __name__ == "__main__":
    unittest.main()
