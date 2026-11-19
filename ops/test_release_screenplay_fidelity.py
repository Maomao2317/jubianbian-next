import sys
import types
import unittest

try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

from release_screenplay_fidelity import (
    ALLOWED, APP_FILES, BASELINE, BASE_BUILD, CHANGED_TEST_FILES,
    FRONTEND_TEST_FILES, PROJECT, PROTECTED_PROJECT, PORT, parse_args,
    verify_config,
)


class ScreenplayFidelityReleaseTests(unittest.TestCase):
    def test_staging_only_and_exact_files(self):
        self.assertEqual(PROJECT, "jubianbian-staging")
        self.assertEqual(PROTECTED_PROJECT, "jubianbian")
        self.assertEqual(PORT, 18000)
        self.assertEqual(BASELINE, "75f483fc4379eaa9f61c0f9e3cbb5ccebf4192d7")
        self.assertEqual(BASE_BUILD, "creation-studio-20261118-75f483f")
        self.assertEqual(APP_FILES, [
            "backend/app/processing.py", "backend/app/providers.py",
            "backend/app/script.py", "backend/app/translation.py",
            "backend/app/user_view.py", "web-staging/js/app.js",
        ])
        self.assertEqual(ALLOWED, set(APP_FILES + CHANGED_TEST_FILES + FRONTEND_TEST_FILES + [
            "ops/screenplay-fidelity-staging-release-2026-11-19.md",
            "ops/release_screenplay_fidelity.py", "ops/test_release_screenplay_fidelity.py",
        ]))

    def test_cli_has_no_production_target(self):
        sha = "a" * 40
        self.assertEqual(parse_args(["script.py", "prepare", sha]), ("prepare", sha))
        self.assertEqual(parse_args(["script.py", "deploy", sha]), ("deploy", sha))
        with self.assertRaises(ValueError):
            parse_args(["script.py", "production", sha])

    def test_compose_guard_allows_only_image_and_build(self):
        old = {"services": {"app": {"image": "old", "environment": {
            "JBB_BUILD_VERSION": "old", "JBB_ENVIRONMENT": "staging"}, "ports": ["18000:8000"]}}}
        new = {"services": {"app": {"image": "new", "environment": {
            "JBB_BUILD_VERSION": "new", "JBB_ENVIRONMENT": "staging"}, "ports": ["18000:8000"]}}}
        verify_config(old, new, "new", "new")
        new["services"]["app"]["ports"] = ["19000:8000"]
        with self.assertRaises(AssertionError):
            verify_config(old, new, "new", "new")


if __name__ == "__main__":
    unittest.main()
