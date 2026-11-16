import unittest
from pathlib import Path
import sys
import types
from unittest.mock import patch

try:
    import fcntl  # noqa: F401
except ModuleNotFoundError:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import release_screenplay_language_restore as release


class ProductionLanguageRestoreTests(unittest.TestCase):
    def _release_sources(self, site):
        sources = {path: "legacy code" for path in release.SITE_FILES[site]}
        sources["web-staging/js/app.js"] = "function renderReviewSummary() {}"
        sources["backend/app/script.py"] = (
            "def compact_clothing_detail(): pass\n"
            "def compact_action_character_introductions(): pass"
        )
        sources["backend/app/providers.py"] = (
            "只保留一个最能识别的核心穿搭；"
            "正文不要罗列发型、五官、年龄外观；完整保留原话"
        )
        if site == "staging":
            sources["backend/app/providers.py"] += (
                " sourceLanguage outputLanguage translate_delivery /translation"
            )
            sources[release.TRANSLATION_FILE] = "translation module"
        return sources

    def _verify_sources(self, site, sources, translation_exists=None):
        base = Path("/virtual-release")
        if translation_exists is None:
            translation_exists = site == "staging"

        def read_text(path, **_kwargs):
            relative = path.as_posix().removeprefix(base.as_posix() + "/")
            return sources[relative]

        def exists(path):
            return translation_exists and path.as_posix().endswith("backend/app/translation.py")

        with patch.object(Path, "read_text", read_text), patch.object(Path, "exists", exists):
            release.verify_release_content(base, site)

    def test_release_targets_staging_before_production(self):
        self.assertEqual(release.SITES["staging"]["project"], "jubianbian-staging")
        self.assertEqual(release.SITES["staging"]["port"], 18000)
        self.assertEqual(release.SITES["production"]["project"], "jubianbian")
        self.assertEqual(release.SITES["production"]["port"], 8000)
        self.assertEqual(
            release.SITES["staging"]["baseline"], release.SITES["production"]["baseline"]
        )
        self.assertEqual(
            release.SITES["staging"]["baselineBuild"],
            "review-download-no-language-20261116-b0041e4",
        )
        self.assertEqual(
            release.BUILD_PREFIX, "character-intro-split-20261116-"
        )
        self.assertEqual(
            release.PRODUCTION_SOURCE_COMMIT,
            "fb8e304decfa9b4d927975aa3e0e11bc85c57836",
        )
        self.assertEqual(
            release.STAGING_LANGUAGE_COMMIT,
            "ec0fe613e07e6692c11b7a589c7961cb83508ad1",
        )
        self.assertIn(release.TRANSLATION_FILE, release.STAGING_CHANGED_FILES)
        self.assertNotIn(release.TRANSLATION_FILE, release.PRODUCTION_CHANGED_FILES)
        self.assertEqual(release.REMOVE_FILES["staging"], [])
        self.assertEqual(release.REMOVE_FILES["production"], [release.TRANSLATION_FILE])

    def test_parse_args_requires_action_and_full_sha(self):
        sha = "a" * 40
        for action in ("prepare", "staging", "production"):
            self.assertEqual(release.parse_args(["script.py", action, sha]), (action, sha))
        for argv in (["script.py", "deploy", sha], ["script.py", "production", "abc"]):
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
        release.verify_config(old, new, "new-image", "new", "production")
        new["services"]["app"]["ports"] = ["9000:9000"]
        with self.assertRaises(AssertionError):
            release.verify_config(old, new, "new-image", "new", "production")

    def test_runtime_guard_requires_exact_site_baseline(self):
        release.assert_known_runtime_digest("backend/app/auth_store.py", "baseline", "baseline")
        with self.assertRaises(AssertionError):
            release.assert_known_runtime_digest("backend/app/auth_store.py", "unknown", "baseline")

    def test_translation_module_must_be_absent_from_both_live_sites(self):
        for site in ("staging", "production"):
            with self.subTest(site=site), patch.object(
                release.common, "output", return_value=b"__ABSENT__"
            ):
                release._assert_translation_state(site, "jubianbian-" + site)
            with self.subTest(site=site + "-present"), patch.object(
                release.common, "output", return_value=b"translation code"
            ), self.assertRaises(AssertionError):
                release._assert_translation_state(site, "jubianbian-" + site)

    def test_content_guard_accepts_environment_specific_language_delivery(self):
        for site in ("staging", "production"):
            with self.subTest(site=site):
                self._verify_sources(site, self._release_sources(site))

    def test_production_guard_rejects_language_and_review_button(self):
        sources = self._release_sources("production")
        sources["web-staging/js/app.js"] += " sourceLanguage"
        with self.assertRaises(AssertionError):
            self._verify_sources("production", sources)
        sources["web-staging/js/app.js"] = (
            "function renderReviewSummary() {} 确认复核并开放下载"
        )
        with self.assertRaises(AssertionError):
            self._verify_sources("production", sources)

    def test_production_guard_rejects_translation_module(self):
        with self.assertRaises(AssertionError):
            self._verify_sources(
                "production", self._release_sources("production"), translation_exists=True
            )

    def test_staging_guard_requires_language_module_and_fields(self):
        sources = self._release_sources("staging")
        sources["backend/app/providers.py"] = sources["backend/app/providers.py"].replace(
            "translate_delivery", "missing_translation"
        )
        with self.assertRaises(AssertionError):
            self._verify_sources("staging", sources)
        with self.assertRaises(AssertionError):
            self._verify_sources(
                "staging", self._release_sources("staging"), translation_exists=False
            )


if __name__ == "__main__":
    unittest.main()
