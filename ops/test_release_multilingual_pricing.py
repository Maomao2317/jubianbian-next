import unittest
from unittest.mock import patch
import sys
import types

try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import release_multilingual_pricing as release


class PricingReleaseTests(unittest.TestCase):
    def test_scope_and_arguments(self):
        self.assertEqual(release.SITES, {"staging": ("jubianbian-staging", 18000), "production": ("jubianbian", 8000)})
        sha = "a" * 40
        for mode in ("prepare", "staging", "production"):
            self.assertEqual(release.parse_args(["script.py", mode, sha]), (mode, sha))
        with self.assertRaises(ValueError):
            release.parse_args(["script.py", "deploy", sha])

    def test_compose_guard_preserves_site_configuration(self):
        old = {"services": {"app": {"image": "old", "environment": {"JBB_BUILD_VERSION": "before", "JBB_RETENTION_DAYS": "30"}, "ports": ["18000:8000"]}}}
        new = {"services": {"app": {"image": "new", "environment": {"JBB_BUILD_VERSION": "after", "JBB_RETENTION_DAYS": "30"}, "ports": ["18000:8000"]}}}
        release.verify_config(old, new, "new", "after")
        old_without_image = {"services": {"app": {"build": {"context": "."}, "environment": {"JBB_BUILD_VERSION": "before", "JBB_RETENTION_DAYS": "30"}, "ports": ["18000:8000"]}}}
        new_with_overlay = {"services": {"app": {"build": {"context": "."}, "image": "new", "environment": {"JBB_BUILD_VERSION": "after", "JBB_RETENTION_DAYS": "30"}, "ports": ["18000:8000"]}}}
        release.verify_config(old_without_image, new_with_overlay, "new", "after")
        new["services"]["app"]["ports"] = ["19000:8000"]
        with self.assertRaises(AssertionError):
            release.verify_config(old, new, "new", "after")

    def test_fetch_requires_pushed_branch_head(self):
        sha = "a" * 40
        with patch.object(release.common, "git", side_effect=[
            b"https://github.com/Maomao2317/jubianbian-next.git\n", b"", (sha + "\n").encode()
        ]):
            release.fetch_revision(sha)

    def test_release_scope_allows_unchanged_check_fixtures(self):
        changed = set(release.APP_FILES + [
            "backend/test_language_pricing.py",
            "backend/test_large_batch.py",
            "backend/test_upload_flow.py",
            "ops/release_multilingual_pricing.py",
            "ops/test_release_multilingual_pricing.py",
            "web-staging/test-language-pricing.cjs",
            "web-staging/test-user-review.cjs",
            "web-staging/test-adaptation-project-list.cjs",
        ])
        with patch.object(release, "_changed_files", return_value=changed):
            release.verify_release_scope("a" * 40)

        with patch.object(release, "_changed_files", return_value=changed | {"unexpected.txt"}):
            with self.assertRaises(AssertionError):
                release.verify_release_scope("a" * 40)


if __name__ == "__main__":
    unittest.main()
