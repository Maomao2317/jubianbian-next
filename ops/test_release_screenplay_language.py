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
        self.assertEqual(self.release.BASELINE, "d1a70c57014245db49273043b7b15ae201025443")
        self.assertEqual(self.release.BASE_BUILD, "screenplay-language-20261113-d1a70c5")
        self.assertEqual(self.release.PROJECT, "jubianbian-staging")
        self.assertEqual(self.release.STAGING_ENVIRONMENT_OVERRIDES, {"JBB_VIDEO_PROVIDER": "ark"})
        self.assertEqual(self.release.APP_FILES, ["backend/app/providers.py"])
        self.assertEqual(self.release.CHANGED_TEST_FILES, ["backend/test_provider_delivery.py"])
        self.assertEqual(self.release.FRONTEND_TEST_FILES, [])
        self.assertEqual(self.release.NEW_FILES, set())
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
                "JBB_VIDEO_PROVIDER": "glm",
                "GLM_API_KEY": "fixture-secret",
            },
        }}}
        new = deepcopy(old)
        new["services"]["app"]["image"] = "verified-image"
        new["services"]["app"]["environment"]["JBB_BUILD_VERSION"] = "new-build"
        new["services"]["app"]["environment"]["JBB_VIDEO_PROVIDER"] = "ark"
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

    def test_video_provider_must_be_ark_for_staging(self):
        old, new = self.config()
        new["services"]["app"]["environment"]["JBB_VIDEO_PROVIDER"] = "glm"
        with self.assertRaises(AssertionError):
            self.release.verify_config(old, new, "verified-image", "new-build")

    def test_existing_screenplay_overlay_is_reused_once(self):
        root = Path("/opt/jubianbian-staging")
        overlay = str(root / "release-screenplay-language.yml")
        base = [str(root / "docker-compose.yml"), str(root / "release-carousel-removal.yml")]
        self.assertEqual(self.release.staging_compose_files(root, base), base + [overlay])
        active = [base[0], overlay, base[1]]
        self.assertEqual(self.release.staging_compose_files(root, active), active)


if __name__ == "__main__":
    unittest.main()
