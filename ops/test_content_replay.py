"""Release replay must reject story edits while permitting profile deduplication."""
from collections import Counter
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    'content_replay', Path(__file__).with_name('verify-stability-completeness.py'))
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class ContentReplayTests(unittest.TestCase):
    def fixture(self):
        return {'characterProfiles': [{'name': '周青', 'clothing': '穿蓝色外套'}],
                'scenes': [{'blocks': [{'type': 'action', 'text': '穿蓝色外套的周青推门。',
                                        'startSec': 1, 'endSec': 2},
                                       {'type': 'dialogue', 'text': '快走！', 'speaker': '周青'}]}]}

    def test_only_profile_fact_changes_are_permitted(self):
        original = self.fixture()
        duplicate = deepcopy(original)
        duplicate['scenes'][0]['blocks'][0]['text'] = '穿蓝色外套、蓝色外套的周青推门。'
        replay.compare_scripts(duplicate, original, Counter())
        for field, value in [('text', '穿蓝色外套的周青关门。'), ('startSec', 3)]:
            changed = deepcopy(original)
            changed['scenes'][0]['blocks'][0][field] = value
            with self.assertRaises(AssertionError):
                replay.compare_scripts(original, changed, Counter())
        changed = deepcopy(original)
        changed['scenes'][0]['blocks'][1]['text'] = '快来！'
        with self.assertRaises(AssertionError):
            replay.compare_scripts(original, changed, Counter())

    def test_enrichment_must_be_exactly_stable(self):
        stable = self.fixture()
        replay.verify_description_stability(stable)
        incomplete = deepcopy(stable)
        incomplete['scenes'][0]['blocks'][0]['text'] = '周青推门。'
        with self.assertRaises(AssertionError):
            replay.verify_description_stability(incomplete)


if __name__ == '__main__':
    unittest.main()
