import sys
import types
import unittest

try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

from release_creation_studio import ALLOWED, APP_FILES, BASE_BUILD, CHANGED_TEST_FILES, PROJECT, PROTECTED_PROJECT, PORT, parse_args, verify_config


class CreationStudioReleaseTests(unittest.TestCase):
    def test_release_is_staging_only_and_scope_is_exact(self):
        self.assertEqual(PROJECT, "jubianbian-staging")
        self.assertEqual(PROTECTED_PROJECT, "jubianbian")
        self.assertEqual(PORT, 18000)
        self.assertEqual(BASE_BUILD, "translation-name-protection-20261118-9370a0d")
        self.assertEqual(
            APP_FILES,
            [
                "backend/app/routes.py",
                "web-staging/css/app.css",
                "web-staging/index.html",
                "web-staging/js/app.js",
                "web-staging/js/config.js",
            ],
        )
        self.assertEqual(CHANGED_TEST_FILES, ["backend/test_upload_flow.py", "backend/test_user_review.py"])
        self.assertEqual(ALLOWED, set(APP_FILES + CHANGED_TEST_FILES + [
            "web-staging/test-campaign-carousel.cjs",
            "web-staging/test-user-review.cjs",
            "web-staging/test-workspace-ui.cjs",
            "ops/creation-studio-staging-release-2026-11-18.md",
            "ops/release_creation_studio.py",
            "ops/test_release_creation_studio.py",
        ]))
        self.assertTrue(all(path.startswith("web-staging/") or path.startswith("backend/app/") for path in APP_FILES))
        self.assertFalse(any("production" in path for path in ALLOWED))

    def test_parse_args_has_no_production_target(self):
        sha = "a" * 40
        self.assertEqual(parse_args(["script.py", "prepare", sha]), ("prepare", sha))
        self.assertEqual(parse_args(["script.py", "deploy", sha]), ("deploy", sha))
        for argv in (
            ["script.py", "production", sha],
            ["script.py", "deploy", "short"],
        ):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                parse_args(argv)

    def test_compose_guard_allows_only_image_and_build(self):
        old = {"services": {"app": {
            "image": "old-image",
            "environment": {"JBB_BUILD_VERSION": "old", "JBB_ENVIRONMENT": "staging", "KEEP": "same"},
            "ports": ["18000:8000"],
        }}}
        new = {"services": {"app": {
            "image": "new-image",
            "environment": {"JBB_BUILD_VERSION": "new", "JBB_ENVIRONMENT": "staging", "KEEP": "same"},
            "ports": ["18000:8000"],
        }}}
        verify_config(old, new, "new-image", "new")
        new["services"]["app"]["ports"] = ["19000:8000"]
        with self.assertRaises(AssertionError):
            verify_config(old, new, "new-image", "new")


if __name__ == "__main__":
    unittest.main()
