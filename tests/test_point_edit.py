import copy
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from infofilm.core import digest
from infofilm.media import audio_info
from infofilm.planning import script_text
from infofilm.point_edit import plan_point_removal
from infofilm.silence import cut_pcm, retime, verify_pcm_edit


class PointEditTests(unittest.TestCase):
    def fixture(self, directory):
        audio = Path(directory) / 'source.wav'
        with wave.open(str(audio), 'wb') as f:
            f.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            f.writeframes(np.arange(32000, dtype=np.int16).tobytes())
        script = {'points': [{'id': f'P{i:02}', 'narration': word} for i, word in enumerate(['one', 'two', 'three', 'four'], 1)]}
        words = [{'id': f'w{i:04}', 'point_id': f'P{i:02}', 'word': p['narration'], 'start': i-1+.2, 'end': i-1+.7}
                 for i,p in enumerate(script['points'], 1)]
        info = audio_info(audio)
        timing = {'words': words, 'points': [{'point_id': w['point_id'], 'start': w['start'], 'end': w['end'],
                    'word_ids': [w['id']]} for w in words], 'audio': str(audio), 'audio_info': info,
                  'audio_sha256': info['audio_sha256'], 'text_sha256': digest(script_text(script))}
        return script, timing, audio

    def test_cut_preserves_kept_words_samples_ids_and_text_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            script, timing, source = self.fixture(directory)
            original = copy.deepcopy(timing)
            new_script, kept, report = plan_point_removal(script, timing, ['P02', 'P03'])
            edited = Path(directory) / 'edited.wav'
            cut_pcm(source, edited, report)
            self.assertTrue(verify_pcm_edit(source, edited, report)['passed'])
            updated = retime(kept, report, edited)
            self.assertEqual(timing, original)
            self.assertEqual([w['id'] for w in updated['words']], ['w0001', 'w0004'])
            self.assertEqual(updated['words'][0]['start'], .2)
            self.assertAlmostEqual(updated['words'][1]['start'], 1.2)
            self.assertEqual(updated['text_sha256'], digest(script_text(new_script)))
            self.assertEqual(report['removed_point_ids'], ['P02', 'P03'])
            self.assertEqual(audio_info(edited)['sample_count'], 16000)

    def test_boundary_and_disjoint_cuts_are_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            script, timing, source = self.fixture(directory)
            for removed in [['P01'], ['P04'], ['P01', 'P03']]:
                _, kept, report = plan_point_removal(script, timing, removed)
                edited = Path(directory) / 'edited.wav'; cut_pcm(source, edited, report)
                retime(kept, report, edited)
                self.assertTrue(verify_pcm_edit(source, edited, report)['passed'])

    def test_missing_ids_all_points_and_stale_or_overlapping_mfa_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            script, timing, _ = self.fixture(directory)
            for removed in [[], ['unknown'], ['P01', 'P02', 'P03', 'P04']]:
                with self.assertRaises(ValueError): plan_point_removal(script, timing, removed)
            timing['text_sha256'] = 'stale'
            with self.assertRaisesRegex(ValueError, 'no longer matches'):plan_point_removal(script, timing, ['P02'])
            timing['text_sha256'] = digest(script_text(script))
            timing['words'][0]['end'] = 1.3
            with self.assertRaisesRegex(ValueError, 'boundaries overlap'):plan_point_removal(script, timing, ['P02'])
