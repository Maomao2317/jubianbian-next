"""Safety checks for the staging-only screenplay language release."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


class ScreenplayLanguageReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).parent))
        with patch.dict(sys.modules, {"fcntl": Mock()}):
            path = Path(__file__).with_name("release_screenplay_language.py")
            spec = importlib.util.spec_from_file_location("screenplay_language_release", path)
            cls.release = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.release)

    def test_release_is_pinned_to_the_observed_staging_baseline(self):
        self.assertEqual(self.release.BASELINE, "8fc2337b5b04b291f023dbe503d9f7b0901d1ae6")
        self.assertEqual(self.release.BASE_BUILD, "dialogue-recovery-20261112-8fc2337")
        self.assertEqual(self.release.PROJECT, "jubianbian-staging")
        self.assertEqual(set(self.release.APP_FILES) & {
            "web-staging/css/app.css",
            "web-staging/js/api.js",
            "web-staging/js/app.js",
        }, {
            "web-staging/css/app.css",
            "web-staging/js/api.js",
            "web-staging/js/app.js",
        })
        self.assertEqual(self.release.NEW_FILES, {"backend/app/translation.py"})
        self.assertNotIn("docker-compose.yml", self.release.ALLOWED)
        self.assertNotIn("fangzhou.env", self.release.ALLOWED)
        self.assertNotIn("backend/app/config.py", self.release.ALLOWED)

    def test_only_the_staging_command_shape_is_accepted(self):
        commit = "a" * 40
        self.assertEqual(self.release.parse_args(["release", "prepare", commit]), ("prepare", commit))
        for argv in (
            ["release", "deploy", "production", commit],
            ["release", "deploy", commit[:7]],
            ["release", "deploy", commit, "extra"],
        ):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                self.release.parse_args(argv)

    def config(self):
        old = {"services": {"app": {
            "image": "old-image",
            "ports": ["18000:8000"],
            "volumes": ["staging-data:/app/data"],
            "environment": {
                "JBB_ENVIRONMENT": "staging",
                "JBB_BUILD_VERSION": "old-build",
                "GLM_API_KEY": "fixture-secret",
            },
        }}}
        new = deepcopy(old)
        new["services"]["app"]["image"] = "verified-image"
        new["services"]["app"]["environment"]["JBB_BUILD_VERSION"] = "new-build"
        return old, new

    def test_only_image_and_build_label_may_change(self):
        old, new = self.config()
        self.release.verify_config(old, new, "verified-image", "new-build")

    def test_unrelated_configuration_changes_are_rejected(self):
        for key in ("secret", "port", "volume", "environment"):
            with self.subTest(key=key):
                old, new = self.config()
                app = new["services"]["app"]
                if key == "secret":
                    app["environment"]["GLM_API_KEY"] = "changed"
                elif key == "port":
                    app["ports"] = ["19000:8000"]
                elif key == "volume":
                    app["volumes"] = ["production-data:/app/data"]
                else:
                    app["environment"]["JBB_ENVIRONMENT"] = "production"
                with self.assertRaises(AssertionError):
                    self.release.verify_config(old, new, "verified-image", "new-build")


if __name__ == "__main__":
    unittest.main()
