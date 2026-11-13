import tempfile
import unittest
from pathlib import Path
import sys
import types

try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import release_screenplay_language_restore as release


class ProductionLanguageRestoreTests(unittest.TestCase):
    def test_release_targets_production_and_names_staging_only_as_guard(self):
        self.assertEqual(release.PROJECT, "jubianbian")
        self.assertEqual(release.PORT, 8000)
        self.assertEqual(release.STAGING_PROJECT, "jubianbian-staging")
        self.assertEqual(release.STAGING_PORT, 18000)
        self.assertNotIn(release.STAGING_PROJECT, release.APP_FILES)

    def test_parse_args_requires_action_and_full_sha(self):
        sha = "a" * 40
        self.assertEqual(release.parse_args(["script.py", "prepare", sha]), ("prepare", sha))
        for argv in (["script.py", "staging", sha], ["script.py", "deploy", "abc"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                release.parse_args(argv)

    def test_compose_guard_allows_only_image_and_build(self):
        old = {"services": {"app": {
            "image": "old-image", "environment": {"JBB_BUILD_VERSION": "old", "KEEP": "same"},
            "ports": ["8000:8000"],
        }}}
        new = {"services": {"app": {
            "image": "new-image", "environment": {"JBB_BUILD_VERSION": "new", "KEEP": "same"},
            "ports": ["8000:8000"],
        }}}
        release.verify_config(old, new, "new-image", "new")
        new["services"]["app"]["ports"] = ["9000:9000"]
        with self.assertRaises(AssertionError):
            release.verify_config(old, new, "new-image", "new")

    def test_runtime_guard_accepts_only_baseline_or_pushed_file_content(self):
        release.assert_known_runtime_digest("backend/app/auth_store.py", "pushed", {"baseline", "pushed"})
        with self.assertRaises(AssertionError):
            release.assert_known_runtime_digest("backend/app/auth_store.py", "unknown", {"baseline", "pushed"})

    def test_content_guard_rejects_language_and_campaign_regressions(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for path in ("web-staging/js/app.js", "web-staging/js/api.js", "backend/app/routes.py", "backend/app/processing.py"):
                (base / path).parent.mkdir(parents=True, exist_ok=True)
                (base / path).write_text("legacy code")
            release.verify_release_content(base)

            app = base / "web-staging/js/app.js"
            app.write_text("sourceLanguage")
            with self.assertRaises(AssertionError):
                release.verify_release_content(base)

    def test_content_guard_rejects_translation_module_and_campaign_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for path in ("web-staging/js/api.js", "backend/app/routes.py", "backend/app/processing.py"):
                (base / path).parent.mkdir(parents=True, exist_ok=True)
                (base / path).write_text("legacy code")
            app = base / "web-staging/js/app.js"
            app.parent.mkdir(parents=True, exist_ok=True)
            app.write_text("campaignSlidesMarkup")
            with self.assertRaises(AssertionError):
                release.verify_release_content(base)


if __name__ == "__main__":
    unittest.main()
