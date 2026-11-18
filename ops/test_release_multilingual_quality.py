import sys
import types
import unittest
from unittest.mock import patch

try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import release_multilingual_quality as release


class MultilingualQualityReleaseTests(unittest.TestCase):
    def test_release_is_staging_only_and_scope_is_exact(self):
        self.assertEqual(release.PROJECT, "jubianbian-staging")
        self.assertEqual(release.PROTECTED_PROJECT, "jubianbian")
        self.assertEqual(release.PORT, 18000)
        self.assertEqual(
            release.BASE_BUILD,
            "translation-screenplay-focus-20261118-8ddf4b5",
        )
        self.assertEqual(
            release.APP_FILES,
            ["backend/app/providers.py", "backend/app/translation.py"],
        )
        self.assertEqual(release.CHANGED_TEST_FILES, ["backend/test_translation.py"])
        self.assertEqual(
            release.ALLOWED,
            set(release.APP_FILES + release.CHANGED_TEST_FILES + release.RELEASE_SUPPORT_FILES),
        )

    def test_parse_args_accepts_no_production_action(self):
        sha = "a" * 40
        for action in ("prepare", "deploy"):
            self.assertEqual(release.parse_args(["script.py", action, sha]), (action, sha))
        for argv in (
            ["script.py", "production", sha],
            ["script.py", "deploy", "short"],
        ):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                release.parse_args(argv)

    def test_fetch_revision_requires_pushed_branch_head(self):
        commit = "a" * 40
        responses = [
            b"https://github.com/Maomao2317/jubianbian-next.git\n",
            b"",
            (commit + "\n").encode(),
        ]
        with patch.object(release.common, "git", side_effect=responses) as git:
            release.fetch_revision(commit)
        self.assertEqual(git.call_args_list[1].args, ("fetch", "--no-tags", "origin", release.REF))

        responses[-1] = ("b" * 40 + "\n").encode()
        with patch.object(release.common, "git", side_effect=responses), self.assertRaises(AssertionError):
            release.fetch_revision(commit)

    def test_compose_guard_allows_only_image_and_build(self):
        old = {"services": {"app": {
            "image": "old-image",
            "environment": {"JBB_BUILD_VERSION": "old", "KEEP": "same"},
            "ports": ["18000:8000"],
        }}}
        new = {"services": {"app": {
            "image": "new-image",
            "environment": {"JBB_BUILD_VERSION": "new", "KEEP": "same"},
            "ports": ["18000:8000"],
        }}}
        release.verify_config(old, new, "new-image", "new")
        new["services"]["app"]["ports"] = ["19000:8000"]
        with self.assertRaises(AssertionError):
            release.verify_config(old, new, "new-image", "new")


if __name__ == "__main__":
    unittest.main()
