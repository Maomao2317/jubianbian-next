"""Tests for the guarded dialogue-reliability promotion helper."""
import importlib.util
from pathlib import Path
import sys
import unittest

spec = importlib.util.spec_from_file_location(
    'promote_dialogue_reliability', Path(__file__).with_name('promote-dialogue-reliability.py'))
promotion = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = promotion
spec.loader.exec_module(promotion)


class DialoguePromotionTests(unittest.TestCase):
    def test_staging_artifacts_must_match_the_exact_runtime_revision_and_image(self):
        prepared = {
            'sourceCommit': promotion.RUNTIME_COMMIT, 'build': promotion.BUILD,
            'offlineTestsPassed': True, 'image': 'sha256:candidate', 'files': {'backend/app/x.py': 'abc'},
        }
        staged = {
            'sourceCommit': promotion.RUNTIME_COMMIT, 'build': promotion.BUILD, 'site': 'staging',
            'verified': True, 'configurationPreserved': True,
            'dataMountsPreserved': True, 'image': 'sha256:candidate',
        }
        promotion.validate_artifacts(prepared, staged)
        staged['image'] = 'sha256:other'
        with self.assertRaises(AssertionError):
            promotion.validate_artifacts(prepared, staged)

    def test_source_scope_is_exact(self):
        promotion.validate_source_scope(promotion.APP_CHANGES | promotion.TEST_CHANGES)
        with self.assertRaises(AssertionError):
            promotion.validate_source_scope(
                promotion.APP_CHANGES | promotion.TEST_CHANGES | {'backend/app/config.py'})

    def test_smoke_explicitly_uses_ark_and_current_incomplete_repair_policy(self):
        source = Path(__file__).with_name('deploy-stability-completeness.py')
        smoke = promotion.load_smoke(source)
        self.assertIn('"provider": "ark"', smoke)
        self.assertIn('assert processing._is_non_retryable_provider_error(exc)', smoke)
        self.assertNotIn('assert not processing._is_non_retryable_provider_error(exc)', smoke)
        self.assertNotIn('processing.glm_recognize(', smoke)

    def test_smoke_transform_refuses_unexpected_source(self):
        with self.assertRaises(AssertionError):
            promotion.transform_smoke('print("unexpected")')


if __name__ == '__main__':
    unittest.main()
