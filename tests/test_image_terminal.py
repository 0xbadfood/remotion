import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from infofilm.core import Store, configuration, digest, read, write
from infofilm.image_terminal import PROTOCOL, REVIEW_TAG, recover, score_errors, score_image, selectable, terminal_settings


def image(path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new('RGB', (32, 24), color)
    img.putpixel((0, 0), (0, 0, 0))
    img.save(path)
    return path


class TerminalImageTests(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        self.config['image_terminal_recovery'] = {'enabled': True, 'provider': 'flux_dev_local', 'max_candidates': 1}
        self.message = 'Revenue collection gave the Company administrative authority.'
        self.case = {'asset_id': 'A001', 'visual_message': self.message,
                     'library': {'assets': [{'id': 'A001', 'acceptance': ['No generated titles'], 'fact_ids': []}]}}

    def score(self, total, passed=False):
        return {'visual_message': self.message, 'message_marks': total*.5, 'prompt_marks': total*.3,
                'usability_marks': total*.2, 'score': total, 'pass': passed,
                'reason_keys': [] if passed else ['unwanted_text'], 'issues': [] if passed else ['Generated lettering'],
                'limitations': [], 'correction': '' if passed else 'Plain parchment', 'score_protocol': PROTOCOL}

    def setup(self, directory):
        root = Path(directory)/'images/abc123'
        original = image(root/'takes/take-00/candidate-0.png', 'red')
        params = {'prompt': 'A clerk examines plain parchment.'}
        state = {'status': 'exhausted', 'visual_message': self.message, 'takes': [
            {'provider': 'qwen_local', 'candidate': str(original), 'sha256': digest(original.read_bytes()), 'settings': params}], 'repairs': []}
        write(root/'message-state.json', state)
        return root, original

    def render(self, config, store, root, params, name):
        return image(root/'candidate-0.png', 'green')

    def test_best_available_can_remain_a_failed_review_and_is_tagged(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder);root, original = self.setup(folder)
            with patch('infofilm.image_recovery.provider_session', return_value=contextlib.nullcontext()) as session, \
                 patch('infofilm.image_recovery.render_take', side_effect=self.render) as render, \
                 patch('infofilm.image_terminal.score_image', side_effect=[self.score(60), self.score(80)]) as score:
                result = recover(self.config, store, root, self.case, Mock(), terminal_settings(self.config))
                self.assertEqual(result['provider_id'], 'flux_dev_local')
                self.assertFalse(result['review']['pass'])
                self.assertTrue(selectable(result))
                self.assertEqual(result['review_tag'], REVIEW_TAG)
                self.assertEqual(score.call_args_list[0].args[3], score.call_args_list[1].args[3])
                self.assertEqual(render.call_count, 1)
            with patch('infofilm.image_recovery.provider_session', side_effect=AssertionError('No reload')), \
                 patch('infofilm.image_terminal.score_image', side_effect=AssertionError('No rescore')):
                self.assertEqual(recover(self.config, store, root, self.case, Mock(), terminal_settings(self.config)), result)
            self.assertEqual(read(root/'message-state.json')['status'], 'best_available')
            store.db.close()

    def test_original_wins_when_it_scores_higher_even_if_premium_passes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder);root, original = self.setup(folder)
            with patch('infofilm.image_recovery.render_take', side_effect=self.render), \
                 patch('infofilm.image_recovery.provider_session', return_value=contextlib.nullcontext()), \
                 patch('infofilm.image_terminal.score_image', side_effect=[self.score(85), self.score(70, True)]):
                result = recover(self.config, store, root, self.case, Mock(), terminal_settings(self.config))
            self.assertEqual(result['file'], str(original))
            self.assertTrue(result['needs_review'])
            self.assertFalse(result['review']['pass'])
            store.db.close()

    def test_score_schema_rejects_nan_boolean_and_out_of_range_marks(self):
        for bad in [float('nan'), True, 51, -1]:
            value = self.score(80);value['message_marks'] = bad
            self.assertTrue(score_errors(value, self.message))
        value = self.score(80);value['score'] = 90
        self.assertTrue(score_errors(value, self.message))
        self.assertFalse(selectable({'review': {'pass': False}, 'needs_review': True}))

    def test_scoring_is_blind_and_uses_code_total(self):
        models = Mock();value = self.score(80);value.pop('score');models.call.return_value = value
        result = score_image(models, self.case, Path('/tmp/actual.png'), 'Actual scene prompt', 'test')
        self.assertEqual(result['score'], 80)
        prompt = models.call.call_args.args[2]
        self.assertIn('Actual scene prompt', prompt)
        self.assertNotIn('qwen_local', prompt)
        self.assertNotIn('flux_dev_local', prompt)

    def test_invalid_policy_stops_before_generation(self):
        for value in [{'enabled': 'yes'}, {'enabled': True, 'provider': 'missing', 'max_candidates': 1},
                      {'enabled': True, 'provider': 'flux_dev_local', 'max_candidates': 50}]:
            with self.assertRaises(ValueError):
                terminal_settings({**self.config, 'image_terminal_recovery': value})
