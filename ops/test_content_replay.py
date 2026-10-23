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

    def test_optional_appearance_verb_is_removed_with_its_exact_fact(self):
        original = self.fixture()
        original['characterProfiles'][0]['clothing'] = '蓝色外套'
        original['scenes'][0]['blocks'][0]['text'] = '蓝色外套的周青推门。'
        for verb in ('穿', '穿着', '身穿'):
            changed = deepcopy(original)
            changed['scenes'][0]['blocks'][0]['text'] = verb + '蓝色外套的周青推门。'
            replay.compare_scripts(original, changed, Counter())
            changed['scenes'][0]['blocks'][0]['text'] = verb + '蓝色外套的周青关门。'
            with self.assertRaises(AssertionError):
                replay.compare_scripts(original, changed, Counter())

    def test_visible_speaker_without_profile_delimits_stability_check(self):
        stable = self.fixture()
        stable['scenes'][0]['blocks'][0]['text'] = '周青爸爸递给穿蓝色外套的周青一只玩偶。'
        stable['scenes'][0]['blocks'].append({
            'type': 'vo', 'speaker': '旁白', 'visibleSpeaker': '周青爸爸', 'text': '他回来了。'})
        replay.verify_description_stability(stable)

    def test_nested_exact_profile_facts_do_not_change_the_action_core(self):
        original = self.fixture()
        original['characterProfiles'][0]['clothing'] = '蓝色带条纹的外套'
        original['scenes'][0]['blocks'][0]['text'] = '蓝色带条纹的外套的周青推门。'
        nested = deepcopy(original)
        nested['scenes'][0]['blocks'][0]['text'] = '蓝色带条纹的蓝色带条纹的外套的外套的周青推门。'
        replay.compare_scripts(nested, original, Counter())
        nested['scenes'][0]['blocks'][0]['text'] = nested['scenes'][0]['blocks'][0]['text'].replace('推门', '关门')
        with self.assertRaises(AssertionError):
            replay.compare_scripts(nested, original, Counter())

    def test_punctuation_inside_nested_facts_does_not_hide_the_outer_fact(self):
        original = self.fixture()
        original['characterProfiles'] = [{
            'name': '门卫', 'appearance': '男性，佩戴绿色制式门卫帽',
            'clothing': '绿色门卫制服，制服胸口印有白色标识',
        }]
        original['scenes'][0]['blocks'][0]['text'] = (
            '两名穿绿色制服的男性，佩戴绿色制式门卫帽、绿色门卫制服、'
            '制服胸口印有白色标识的门卫上前推门。')
        nested = deepcopy(original)
        nested['scenes'][0]['blocks'][0]['text'] = (
            '两名穿绿色制服的男性，佩戴绿色制式男性，佩戴绿色制式门卫帽、'
            '绿色门卫制服、制服胸口印有白色标识的门卫帽、绿色门卫制服，'
            '制服胸口印有白色标识的门卫上前推门。')
        replay.compare_scripts(nested, original, Counter())
        nested['scenes'][0]['blocks'][0]['text'] = nested['scenes'][0]['blocks'][0]['text'].replace('推门', '关门')
        with self.assertRaises(AssertionError):
            replay.compare_scripts(nested, original, Counter())


if __name__ == '__main__':
    unittest.main()
