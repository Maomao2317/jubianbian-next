import sys
import types
import unittest
from unittest.mock import patch

if sys.platform == "win32" and "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import release_adaptation_ui as release


class AdaptationUiReleaseTests(unittest.TestCase):
    def test_rejects_production_or_arbitrary_target_arguments(self):
        for args in (["release", "production", "a" * 40],
                     ["release", "deploy", "production", "a" * 40],
                     ["release", "deploy", "HEAD"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                release.main(args)

    def test_release_scope_rejects_backend_changes(self):
        with patch.object(release.common, "git", return_value=b"backend/app/processing.py\n"):
            with self.assertRaisesRegex(AssertionError, "Unexpected release files"):
                release.verify_scope("a" * 40)

    def test_valid_frontend_scope_still_requires_source_ancestry(self):
        def git(*args):
            if args[0] == "diff":
                return b"web-staging/js/adaptation.js\nweb-staging/css/app.css\n"
            raise RuntimeError("Not descended from reviewed source")
        with patch.object(release.common, "git", side_effect=git):
            with self.assertRaisesRegex(RuntimeError, "reviewed source"):
                release.verify_scope("a" * 40)
        self.assertTrue(all(path.startswith("web-staging/") for path in release.ASSETS))
        self.assertEqual(release.PROJECT, "jubianbian-staging")


if __name__ == "__main__":
    unittest.main()
