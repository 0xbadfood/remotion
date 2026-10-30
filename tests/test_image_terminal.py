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
                'limitations': [], 'correction': '' if passed else 'Plain parchment', 'score_protocol': PROTOCOL,
                'component_inventory': []}

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

    def test_unreviewed_candidate_is_available_for_premium_comparison(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder);root, original = self.setup(folder)
            state=read(root/'message-state.json')
            state['pending_review']=state['takes'].pop()
            write(root/'message-state.json',state)
            with patch('infofilm.image_recovery.render_take',side_effect=self.render), \
                 patch('infofilm.image_recovery.provider_session',return_value=contextlib.nullcontext()), \
                 patch('infofilm.image_terminal.score_image',side_effect=[self.score(85),self.score(70,True)]) as score:
                result=recover(self.config,store,root,self.case,Mock(),terminal_settings(self.config))
            self.assertEqual(result['file'],str(original))
            self.assertEqual(score.call_count,2)
            self.assertTrue(result['needs_review'])
            store.db.close()

    def test_zero_premium_candidates_keeps_the_best_local_try_without_the_cloud(self):
        config = {**self.config, 'image_terminal_recovery': {'enabled': True, 'provider': 'flux_dev_local', 'max_candidates': 0}}
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder);root, original = self.setup(folder)
            with patch('infofilm.image_recovery.provider_session', side_effect=AssertionError('No premium session')), \
                 patch('infofilm.image_recovery.render_take', side_effect=AssertionError('No premium render')), \
                 patch('infofilm.image_terminal.score_image', return_value=self.score(72)):
                result = recover(config, store, root, self.case, Mock(), terminal_settings(config))
            self.assertEqual(result['file'], str(original))
            self.assertEqual(result['review_tag'], REVIEW_TAG)
            states = [r[0] for r in store.db.execute("SELECT state FROM events")]
            self.assertIn('best_local_selection', states); self.assertNotIn('premium_rescue', states)
            store.db.close()

    def test_final_ranking_reuses_each_takes_score_and_code_picks_the_highest(self):
        config = {**self.config, 'image_terminal_recovery': {'enabled': True, 'provider': 'flux_dev_local', 'max_candidates': 0}}
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder); root, original = self.setup(folder)
            second = image(root/'takes/take-01/candidate-0.png', 'blue')
            state = read(root/'message-state.json')
            state['takes'][0]['review'] = {**self.score(62), 'repair_mode': 'targeted', 'mode_reason': 'Small fix.'}
            state['takes'].append({'provider': 'qwen_local', 'candidate': str(second), 'sha256': digest(second.read_bytes()),
                                   'settings': {'prompt': 'A clerk examines plain parchment, closer.'},
                                   'review': {**self.score(78), 'repair_mode': 'targeted', 'mode_reason': 'Small fix.'}})
            write(root/'message-state.json', state)
            with patch('infofilm.image_terminal.score_image', side_effect=AssertionError('Takes are already scored')):
                result = recover(config, store, root, self.case, Mock(), terminal_settings(config))
            self.assertEqual(result['file'], str(second)); self.assertEqual(result['review']['score'], 78)
            self.assertEqual(result['review']['score_source'], 'take_review'); self.assertTrue(result['needs_review'])
            store.db.close()

    def two_failed_takes(self, folder):
        store = Store(folder); root, original = self.setup(folder)
        second = image(root/'takes/take-01/candidate-0.png', 'blue')
        state = read(root/'message-state.json')
        state['takes'][0]['review'] = {**self.score(55), 'repair_mode': 'targeted', 'mode_reason': 'Small fix.'}
        state['takes'].append({'provider': 'qwen_local', 'candidate': str(second), 'sha256': digest(second.read_bytes()),
                               'settings': {'prompt': 'A clerk examines plain parchment, closer.'},
                               'review': {**self.score(76), 'repair_mode': 'targeted', 'mode_reason': 'Small fix.'}})
        write(root/'message-state.json', state)
        return store, root, original, second

    def test_when_no_take_passed_they_are_compared_side_by_side_and_a_misread_subject_loses(self):
        # A spinning top (2026-10-30): the take scored highest looked like a cocktail glass; compared together, the
        # reviewer picks the take whose subject reads as a top, though its own score was lower.
        config = {**self.config, 'image_terminal_recovery': {'enabled': True, 'provider': 'flux_dev_local', 'max_candidates': 0}}
        with tempfile.TemporaryDirectory() as folder:
            store, root, original, second = self.two_failed_takes(folder)
            models = Mock(); models.call.return_value = {'pick': 'A', 'misread': ['B'], 'reason': 'B looks like a glass.'}
            result = recover(config, store, root, self.case, models, terminal_settings(config))
            self.assertEqual(result['file'], str(original))
            self.assertIn('side by side', result['terminal_recovery']['rule'])
            self.assertTrue(result['needs_review'])
            sheet = Image.open(models.call.call_args.kwargs['image_path'])
            self.assertEqual(sheet.size, (2 * 768, 432))
            store.db.close()

    def test_a_take_unfit_for_students_is_never_shown(self):
        # C13 of keph1-06 (2026-10-30): an acrobat's tucked pose came back as a bare body; it is never the best available
        config = {**self.config, 'image_terminal_recovery': {'enabled': True, 'provider': 'flux_dev_local', 'max_candidates': 0}}
        with tempfile.TemporaryDirectory() as folder:
            store, root, original, second = self.two_failed_takes(folder)
            state = read(root/'message-state.json')
            state['takes'][1]['review']['reason_keys'] = ['unsuitable_for_students']
            write(root/'message-state.json', state)
            models = Mock()
            result = recover(config, store, root, self.case, models, terminal_settings(config))
            self.assertEqual(result['file'], str(original))      # the higher-scored take was unfit
            models.call.assert_not_called()                      # one usable take: nothing to compare
            store.db.close()
        with tempfile.TemporaryDirectory() as folder:
            store, root, original, second = self.two_failed_takes(folder)
            state = read(root/'message-state.json')
            for take in state['takes']:
                take['review']['reason_keys'] = ['unsuitable_for_students']
            write(root/'message-state.json', state)
            with self.assertRaisesRegex(RuntimeError, 'unsuitable for students'):
                recover(config, store, root, self.case, Mock(), terminal_settings(config))
            store.db.close()

    def test_people_are_said_to_be_clothed_and_reviewers_check_it(self):
        from infofilm.prompt_styles import compose
        from infofilm.educational_review import review_guidance
        acrobat = compose('structured', setting='A studio.', visual_style='Editorial.', layout='Full frame.',
                          scene='One circus acrobat mid-somersault, knees pulled to the chest.')
        self.assertIn('modestly clothed', acrobat)
        beaker = compose('structured', setting='A lab.', visual_style='Editorial.', layout='Full frame.', scene='A beaker of water.')
        self.assertNotIn('clothed', beaker)
        self.assertIn('unsuitable_for_students', review_guidance({}))

    def test_a_failed_or_contradictory_comparison_leaves_the_score_ranking(self):
        config = {**self.config, 'image_terminal_recovery': {'enabled': True, 'provider': 'flux_dev_local', 'max_candidates': 0}}
        for answer in [{'pick': 'B', 'misread': ['B']}, {'pick': 'Z'}, ValueError('bad JSON')]:
            with tempfile.TemporaryDirectory() as folder:
                store, root, original, second = self.two_failed_takes(folder)
                models = Mock(); models.call.side_effect = [answer] if isinstance(answer, Exception) else None
                if not isinstance(answer, Exception): models.call.return_value = answer
                result = recover(config, store, root, self.case, models, terminal_settings(config))
                self.assertEqual(result['file'], str(second))
                self.assertEqual(result['terminal_recovery']['rule'], 'highest marks; premium wins ties')
                store.db.close()

    def test_score_schema_rejects_nan_boolean_and_out_of_range_marks(self):
        for bad in [float('nan'), True, 51, -1]:
            value = self.score(80);value['message_marks'] = bad
            self.assertTrue(score_errors(value, self.message))
        value = self.score(80);value['score'] = 90
        self.assertTrue(score_errors(value, self.message))
        self.assertFalse(selectable({'review': {'pass': False}, 'needs_review': True}))

    def test_pass_contradicting_its_own_inventory_is_ruled_a_fail_by_code(self):
        models = Mock(); value = self.score(80, True); value.pop('score')
        value['component_inventory'] = [{'component': 'amber coupling rings', 'expected_count': 2, 'visible_count': 4,
            'count_status': 'not_met', 'placement_status': 'not_applicable', 'reference_checked': False,
            'evidence': 'Rings at the tank tops and again on the hoses.'}]
        models.call.return_value = value
        result = score_image(models, self.case, Path('/tmp/actual.png'), 'Actual scene prompt', 'test')
        self.assertEqual(models.call.call_count, 1)                        # no schema-repair round trip
        self.assertFalse(result['pass']); self.assertEqual(result['score'], 80)
        self.assertEqual(result['reason_keys'], ['semantic_mismatch'])
        self.assertIn('amber coupling rings', result['issues'][0]); self.assertIn('overruled', result['code_ruling'])

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
