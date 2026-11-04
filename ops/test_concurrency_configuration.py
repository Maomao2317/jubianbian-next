"""Verify the deployed concurrency profile cannot regress through stale env files."""
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parent.parent
PROTECTED_PROFILE = {
    "JBB_WORKER_CONCURRENCY": "24",
    "JBB_ARK_MAX_IN_FLIGHT": "24",
    "JBB_SHARED_MODEL_CONCURRENCY": "24",
    "JBB_EVIDENCE_CONCURRENCY": "4",
    "JBB_ARK_UPLOAD_MAX_IN_FLIGHT": "4",
    "JBB_ARK_PROXY_MAX_IN_FLIGHT": "2",
    "JBB_ARK_QUOTA_SCOPE": "per-key",
}
RESOURCE_LIMITS = {
    "JBB_ARK_ACCOUNT_MAX_IN_FLIGHT": "8",
    "JBB_DOWNLOAD_CONCURRENCY": "2",
    "JBB_GPU_ASR_MAX_IN_FLIGHT": "2",
    "JBB_GPU_OCR_MAX_IN_FLIGHT": "2",
    "JBB_LOCAL_ASR_CONCURRENCY": "1",
    "JBB_OCR_PROCESS_CONCURRENCY": "4",
    "JBB_GLM_MAX_IN_FLIGHT": "1",
}
class ConcurrencyConfigurationTests(unittest.TestCase):
    def test_protected_profile_is_literal_and_resource_limits_keep_defaults(self):
        for name in ("docker-compose.yml", "docker-compose.staging.yml"):
            with self.subTest(compose=name):
                compose = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
                actual = compose["services"]["app"]["environment"]
                for key, value in PROTECTED_PROFILE.items():
                    self.assertEqual(str(actual[key]), value, key)
                    self.assertNotIn("${", str(actual[key]), key)
                for key, value in RESOURCE_LIMITS.items():
                    self.assertEqual(str(actual[key]), f"${{{key}:-{value}}}", key)


if __name__ == "__main__":
    unittest.main()
