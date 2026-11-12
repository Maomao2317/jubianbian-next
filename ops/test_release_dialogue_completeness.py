"""Dialogue release rejects unrelated configuration and stale staging."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


class DialogueReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).parent))
        with patch.dict(sys.modules, {"fcntl": Mock()}):
            spec = importlib.util.spec_from_file_location("dialogue_release", Path(__file__).with_name("release_dialogue_completeness.py"))
            cls.release = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.release)

    def config(self):
        old = {"services": {"app": {"image": "old", "ports": ["8000:8000"],
               "environment": {"JBB_BUILD_VERSION": "old", "ARK_API_KEY": "fixture",
                               "JBB_ARK_INFRA_RESERVE_RATIO": "0.1"}}}}
        new = deepcopy(old)
        update = dict(self.release.BUDGET_ENV, JBB_BUILD_VERSION="new")
        new["services"]["app"]["image"] = "release"
        new["services"]["app"]["environment"].update(update)
        return old, new, update

    def test_overlay_is_limited_to_recovery_patch_and_preserves_frontend(self):
        self.assertEqual(self.release.NEW_FILES, {"backend/app/dialogue_recovery.py"})
        self.assertIn("backend/test_dialogue_recovery.py", self.release.CHECK_FILES)
        self.assertNotIn("web-staging/js/app.js", self.release.APP_FILES)
        self.assertIn("web-staging/js/app.js", self.release.PRESERVED_FILES)
        self.assertNotIn("backend/.env", self.release.ALLOWED)

    def test_authorized_budget_and_image_changes_only(self):
        old, new, update = self.config()
        unchanged = deepcopy(new)
        self.release.verify_config(old, new, "release", update)
        self.assertEqual(new, unchanged)

    def test_unrelated_env_ports_or_image_changes_are_rejected(self):
        for field in ("secret", "ports", "image", "budget"):
            with self.subTest(field=field):
                old, new, update = self.config()
                app = new["services"]["app"]
                if field == "secret":
                    app["environment"]["ARK_API_KEY"] = "changed"
                elif field == "ports":
                    app["ports"] = ["9000:8000"]
                elif field == "image":
                    app["image"] = "unverified"
                else:
                    app["environment"]["JBB_ARK_QUALITY_MIN_GROSS_MARGIN_RATIO"] = "-1"
                with self.assertRaises(AssertionError):
                    self.release.verify_config(old, new, "release", update)

    def test_production_requires_same_verified_staging_revision_and_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            record = {"commit": "sha", "verified": True, "files": {"app": "hash"}, "fingerprint": ["id"]}
            with patch.object(self.release.common, "fingerprint", return_value=["id"]), \
                 patch.object(self.release.common, "health") as health, \
                 patch.object(self.release.common, "verify_files") as files:
                (base / "staging-deployment.json").write_text(json.dumps(record))
                self.release.verify_staging(base, "sha", "build", {"app": "hash"})
                health.assert_called_once_with(18000, "staging", "build")
                files.assert_called_once()
                for key, value in (("commit", "other"), ("verified", False),
                                   ("files", {"app": "other"}), ("fingerprint", ["restarted"])):
                    with self.subTest(key=key):
                        (base / "staging-deployment.json").write_text(json.dumps({**record, key: value}))
                        with self.assertRaises(AssertionError):
                            self.release.verify_staging(base, "sha", "build", {"app": "hash"})


if __name__ == "__main__":
    unittest.main()
