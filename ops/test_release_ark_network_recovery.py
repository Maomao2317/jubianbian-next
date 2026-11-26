import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

if sys.platform == "win32" and "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import release_ark_network_recovery as release


class ArkNetworkReleaseTests(unittest.TestCase):
    def test_runtime_scope_and_site_baselines(self):
        self.assertEqual(set(release.APP_FILES), {
            "backend/app/ark_transport.py", "backend/app/providers.py", "backend/requirements.txt",
        })
        self.assertEqual(release.BASE_BUILDS["production"], "visual-review-20261123-7d17eb8")
        self.assertEqual(release.BASE_BUILDS["staging"], "script-adaptation-20261125-8661355")

    def test_adaptation_guard_handles_existing_and_active_jobs(self):
        with sqlite3.connect(":memory:") as connection:
            release.verify_adaptation_idle(connection)
            connection.execute("CREATE TABLE adaptations (job_id TEXT, job_started REAL)")
            release.verify_adaptation_idle(connection)
            connection.execute("INSERT INTO adaptations VALUES ('active', ?)", (release.time.time(),))
            with self.assertRaisesRegex(RuntimeError, "active script adaptation"):
                release.verify_adaptation_idle(connection)

    def test_deployment_requires_successful_real_model_smoke(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            record = {"commit": "a" * 40, "realModelSmoke": {"verified": False}}
            (base / "staging-prepared.json").write_text(json.dumps(record))
            with patch.object(release.release, "deploy") as deploy, self.assertRaises(AssertionError):
                release.deploy("jubianbian-staging", "staging", 18000, "a" * 40, "build", base)
            deploy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
