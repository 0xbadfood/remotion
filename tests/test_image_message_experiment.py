import contextlib
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from infofilm.core import ROOT, Store, digest, read, write
from infofilm.image_message_experiment import (RepairContractError, annotated_corpus,
    current_image_review, generation_prompt, initial_seed, prepare, repair_scene,
    review_errors, run_case, run_message_loop, validate_messages)
from infofilm.media import image_identity


class MessageExperimentTests(unittest.TestCase):
    def setUp(self):
        self.parent = read(ROOT / 'pilots/image-ladder/cases.json')
        self.annotations = read(ROOT / 'pilots/image-message-repair/messages.json')
        self.corpus = annotated_corpus(self.parent, self.annotations)
        self.case = self.corpus['cases'][0]
        self.message = self.case['visual_message']
        self.settings = read(ROOT / 'pilots/image-message-repair/settings.json')
        self.config = {'image_url': 'http://qwen', 'tts_url': 'http://tts',
            'planner_url': 'http://planner', 'reviewer_url': 'http://reviewer',
            'image_width': 1536, 'image_height': 864, 'image_steps': 25,
            'image_provider_registry': read(ROOT / 'image-providers.json')}

    def verdict(self, passed=False, mode='targeted', reason='semantic_mismatch'):
        return {'visual_message': self.message, 'pass': passed,
            'reason_keys': [] if passed else [reason], 'issues': [] if passed else ['The measuring rod is missing.'],
            'limitations': [], 'repair_mode': 'none' if passed else mode,
            'mode_reason': 'The concept is useful; correct its main prop.',
            'correction': '' if passed else 'Show the measuring implement clearly.'}

    def replacement(self, mode='targeted', text=None):
        return {'visual_message': self.message, 'repair_mode': mode,
            'prompt': text or 'A Mughal surveyor measures cultivated land using a clearly visible plain brass rod.',
            'simplifications': ['Replace the abstract grid with a surveyor using a measuring implement.'] if mode == 'simplify' else [],
            'message_preserved': 'The instrument and surveying activity illustrate comparable land measurement.'}

    @contextlib.contextmanager
    def engine(self, reviews, repairs=()):
        generated = []
        def render(config, store, root, params):
            root.mkdir(parents=True, exist_ok=True)
            path = root / 'candidate-0.png'
            path.write_bytes(b'image-' + str(len(generated)).encode())
            write(root / 'provider-0.json', {'params': params, 'job_id': 'fixture'})
            generated.append(copy.deepcopy(params))
            return path
        with patch('infofilm.image_message_experiment.provider_session', return_value=contextlib.nullcontext()) as session, \
             patch('infofilm.image_message_experiment.render_take', side_effect=render), \
             patch('infofilm.image_message_experiment.current_image_review', side_effect=reviews) as review, \
             patch('infofilm.image_message_experiment.repair_scene', side_effect=repairs) as repair:
            yield generated, session, review, repair

    def test_annotations_preserve_frozen_parent_and_cover_all_messages(self):
        before = digest(self.parent)
        cases = validate_messages(self.corpus)
        self.assertEqual(len(cases), 20)
        self.assertEqual(digest(self.parent), before)
        self.assertEqual(self.corpus['parent_corpus_sha256'], before)
        for case in cases:
            self.assertEqual(case['historical_history'], next(c for c in self.parent['cases'] if c['case_id'] == case['case_id'])['historical_history'])
        broken = copy.deepcopy(self.annotations)
        broken['cases'].pop('T20')
        with self.assertRaisesRegex(ValueError, 'exactly'):
            annotated_corpus(self.parent, broken)

    def test_changed_message_cannot_be_hidden_by_rehashing(self):
        broken = copy.deepcopy(self.corpus)
        broken['cases'][0]['library']['assets'][0]['visual_message'] = 'A different meaning.'
        broken['case_sha256'] = digest(broken['cases'])
        with self.assertRaisesRegex(ValueError, 'identical'):
            validate_messages(broken)

    def test_initial_seed_ignores_added_message_metadata(self):
        old = self.parent['cases'][0]
        self.assertEqual(initial_seed(self.config, self.case),
            int(image_identity(self.config, old['library'], old['library']['assets'][0])[:7], 16))

    def test_generator_wrapper_omits_shared_titles_and_overlay_cues(self):
        for case in self.corpus['cases']:
            prompt = generation_prompt(case, case['message_annotation']['initial_scene'])
            self.assertNotIn('The Long Unraveling', prompt)
            self.assertNotIn('banner', prompt.lower())
            self.assertNotIn('text overlay', prompt.lower())
            self.assertIn(case['message_annotation']['setting'], prompt)

    def test_review_sees_only_current_message_prompt_and_image(self):
        models = Mock()
        models.call.return_value = self.verdict(True)
        candidate = Path('/tmp/current-image.png')
        result = current_image_review(models, self.case, candidate, 'Current actual submitted prompt', 'test')
        payload = models.call.call_args.args[2].split('CURRENT INPUT:\n')[1]
        import json
        self.assertEqual(json.loads(payload), {'visual_message': self.message, 'current_prompt': 'Current actual submitted prompt'})
        self.assertEqual(models.call.call_args.kwargs['image_path'], candidate)
        self.assertNotIn('historical_history', payload)
        self.assertEqual(result['repair_mode'], 'none')

    def test_invalid_review_cannot_accept_an_image(self):
        models = Mock()
        models.call.return_value = {**self.verdict(True), 'visual_message': 'wrong'}
        with self.assertRaises(RepairContractError):
            current_image_review(models, self.case, Path('/tmp/image.png'), 'prompt', 'test')
        self.assertEqual(models.call.call_count, 2)
        self.assertTrue(review_errors({**self.verdict(True), 'issues': ['Blocking error']}, self.message))
        self.assertTrue(review_errors({**self.verdict(), 'reason_keys': None}, self.message))

    def test_unchanged_repair_is_rejected_relative_to_last_scene(self):
        models = Mock()
        last = 'A Mughal surveyor measures cultivated land using a clearly visible plain brass rod.'
        models.call.return_value = self.replacement(text=last.upper() + ' !!!')
        with self.assertRaisesRegex(RepairContractError, 'another image'):
            repair_scene(models, self.case, last, 'actual prompt', self.verdict(), 'test')
        self.assertEqual(models.call.call_count, 2)

    def test_planner_may_simplify_immediately_but_not_ignore_simplify_request(self):
        models = Mock()
        models.call.return_value = self.replacement('simplify')
        old_scene = self.case['message_annotation']['initial_scene']
        result = repair_scene(models, self.case, old_scene, 'actual prompt', self.verdict(), 'test')
        self.assertEqual(result['repair_mode'], 'simplify')
        models.call.return_value = self.replacement('targeted')
        with self.assertRaises(RepairContractError):
            repair_scene(models, self.case, old_scene, 'actual prompt', self.verdict(mode='simplify'), 'test')

    def test_pass_ends_loop_without_repair_audit_or_provider_switch(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([self.verdict(True)]) as (images, session, review, repair):
                    state = run_message_loop(self.config, store, self.case, Mock(), self.settings)
                    self.assertEqual(state['status'], 'accepted')
                    self.assertEqual(len(images), 1)
                    self.assertEqual(images[0]['width'], '1536')
                    self.assertEqual(images[0]['steps'], '25')
                    self.assertEqual(review.call_count, 1)
                    repair.assert_not_called()
                    self.assertEqual(session.call_count, 1)
                with patch('infofilm.image_message_experiment.provider_session', side_effect=AssertionError('accepted')):
                    self.assertEqual(run_message_loop(self.config, store, self.case, Mock(), self.settings), state)
            finally:
                store.db.close()

    def test_first_failure_can_change_depiction_and_pass_next_image(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([self.verdict(mode='simplify'), self.verdict(True)], [self.replacement('simplify')]) as (images, _, _, _):
                    state = run_message_loop(self.config, store, self.case, Mock(), self.settings)
                    self.assertEqual(state['status'], 'accepted')
                    self.assertEqual(len(images), 2)
                    self.assertNotEqual(images[0]['prompt'], images[1]['prompt'])
                    self.assertIn('surveyor', images[1]['prompt'])
                    for take in state['takes']:
                        self.assertEqual(take['visual_message'], self.message)
                    self.assertEqual(read(Path(d) / 'takes/take-00/repair.json')['visual_message'], self.message)
            finally:
                store.db.close()

    def test_budget_bounds_all_full_resolution_takes(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                simpler = self.replacement('simplify', 'A plain measuring rod rests beside a newly measured field boundary in period Mughal India.')
                with self.engine([self.verdict()] * 3, [self.replacement(), simpler]) as (images, _, _, repair):
                    state = run_message_loop(self.config, store, self.case, Mock(), self.settings)
                    self.assertEqual(state['status'], 'exhausted')
                    self.assertEqual(len(images), 3)
                    self.assertEqual(repair.call_count, 2)
                    self.assertTrue(all((p['width'], p['height']) == ('1536', '864') for p in images))
                    self.assertTrue(all(t['provider'] == 'qwen_local' for t in state['takes']))
            finally:
                store.db.close()

    def test_interrupted_repair_resumes_without_repeating_failed_image(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([self.verdict()], [RuntimeError('planner offline')]):
                    with self.assertRaisesRegex(RuntimeError, 'planner offline'):
                        run_message_loop(self.config, store, self.case, Mock(), self.settings)
                self.assertTrue(read(Path(d) / 'message-state.json')['pending_repair'])
                with self.engine([self.verdict(True)], [self.replacement('simplify')]) as (images, _, _, repair):
                    state = run_message_loop(self.config, store, self.case, Mock(), self.settings)
                    self.assertEqual(len(images), 1)
                    self.assertEqual(len(state['takes']), 2)
                    self.assertEqual(repair.call_count, 1)
                    self.assertEqual(state['status'], 'accepted')
            finally:
                store.db.close()

    def test_backend_corruption_stops_without_artistic_repair(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([self.verdict(mode='diagnose_backend', reason='backend_corruption')]) as (images, _, _, repair):
                    self.assertEqual(run_message_loop(self.config, store, self.case, Mock(), self.settings)['status'], 'backend_error')
                    self.assertEqual(len(images), 1)
                    repair.assert_not_called()
            finally:
                store.db.close()

    def test_preparation_is_no_network_and_freezes_executed_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('infofilm.image_message_experiment.provider_session', side_effect=AssertionError('no network')):
                config, path = prepare(self.corpus, self.config, self.settings, [self.case], d)
                self.assertTrue(path.is_file())
                self.assertEqual(config['image_provider_id'], 'qwen_local')
            write(Path(d) / 'T01/result.json', {})
            with self.assertRaisesRegex(ValueError, 'Frozen'):
                prepare(self.corpus, self.config, {**self.settings, 'max_image_attempts': 4}, [self.case], d)
        with self.assertRaisesRegex(ValueError, 'frozen ladder'):
            prepare(self.corpus, self.config, self.settings, [self.case], ROOT / 'artifacts/image-ladder-experiment/new')

    def test_blocked_repair_has_no_additional_image_and_remains_in_result(self):
        with tempfile.TemporaryDirectory() as d:
            with self.engine([self.verdict()], [RepairContractError('unchanged replacement')]) as (images, _, _, _):
                result = run_case(self.case, self.config, d, self.settings)
                self.assertEqual(result['status'], 'repair_blocked')
                self.assertEqual(result['attempts'], 1)
                self.assertEqual(len(images), 1)
                self.assertFalse(result['resumable'])
                self.assertEqual(result['visual_message'], self.message)


if __name__ == '__main__':
    unittest.main()
