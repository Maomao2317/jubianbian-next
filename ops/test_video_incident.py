from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location('video_incident', Path(__file__).with_name('apply_video_incident.py'))
incident = importlib.util.module_from_spec(spec)
spec.loader.exec_module(incident)


class VideoIncidentTests(unittest.TestCase):
    def test_reviewed_speech_keeps_words_times_occurrences_and_messages(self):
        script = {'scenes': [{'blocks': [
            {'type': 'screen_text', 'screenType': 'subtitle', 'text': '周青，还钱！', 'startSec': 1, 'endSec': 2},
            {'type': 'screen_text', 'screenType': 'subtitle', 'text': '还钱！', 'startSec': 2, 'endSec': 3},
            {'type': 'screen_text', 'screenType': 'subtitle', 'text': '还钱！', 'startSec': 3, 'endSec': 4},
            {'type': 'screen_text', 'screenType': 'system', 'text': '手机短信', 'startSec': 2, 'endSec': 4}]}]}
        before = deepcopy(script)
        repaired = incident.repair_captions(script, [[0, 0], [0, 1], [0, 2]])
        self.assertEqual(script, before)
        for a, b in zip(script['scenes'][0]['blocks'], repaired['scenes'][0]['blocks']):
            self.assertEqual((a['text'], a['startSec'], a['endSec']), (b['text'], b['startSec'], b['endSec']))
        self.assertEqual(repaired['scenes'][0]['blocks'][0]['speaker'], '未知说话人')
        self.assertEqual(repaired['scenes'][0]['blocks'][3], script['scenes'][0]['blocks'][3])
        with self.assertRaises(AssertionError):
            incident.repair_captions(script, [[0, 3]])
        with self.assertRaises(AssertionError):
            incident.repair_captions(script, [[0, 0], [0, 0]])


if __name__ == '__main__':
    unittest.main()
