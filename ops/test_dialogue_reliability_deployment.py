"""Tests for the guarded dialogue-reliability promotion helper."""
import importlib.util
from pathlib import Path
import sys
import types
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

    def test_driver_invocation_does_not_require_driver_sys_attribute(self):
        original_argv = sys.argv
        observed = []
        driver = types.SimpleNamespace(main=lambda: observed.append(tuple(sys.argv)))
        argv = [promotion.DRIVER, 'production', '--commit', promotion.RUNTIME_COMMIT]
        promotion.run_driver(driver, argv)
        self.assertEqual(observed, [tuple(argv)])
        self.assertIs(sys.argv, original_argv)

    def test_prior_release_upload_budget_check_is_replaced_precisely(self):
        calls = []
        routing = types.SimpleNamespace(
            output=lambda args, **kwargs: calls.append((args, kwargs)) or b'verified')
        promotion.replace_unrelated_upload_budget_check(routing)
        old_check = (
            "from app import config; assert config.MEDIA_PROXY_UPLOAD_TIMEOUT_SECONDS == 900; "
            "assert config.ARK_UPLOAD_TOTAL_TIMEOUT_SECONDS == 900; "
            "print('UPLOAD_BUDGETS_VERIFIED')"
        )
        command = ['docker', 'exec', '-e', 'PYTHONPATH=/app/backend', 'app',
                   'python', '-c', old_check]
        self.assertEqual(routing.output(command, timeout=5), b'verified')
        rewritten, kwargs = calls.pop()
        self.assertEqual(rewritten[:-1], command[:-1])
        self.assertIn('RUNTIME_CONFIGURATION_VERIFIED', rewritten[-1])
        self.assertNotIn('UPLOAD_BUDGETS_VERIFIED', rewritten[-1])
        self.assertEqual(kwargs, {'timeout': 5})

        normal = ['docker', 'exec', 'app', 'python', '-c', 'print(1)']
        routing.output(normal)
        self.assertEqual(calls.pop()[0], normal)


if __name__ == '__main__':
    unittest.main()
