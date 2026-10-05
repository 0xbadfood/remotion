import contextlib
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image

from infofilm.core import ROOT, Store, digest, read, write
from infofilm.image_experiment import collect_cases, compare, ladder_transition, render_take, resolution_details, run_case, run_ladder, validate_corpus
from infofilm.media import image_identity
from tests.test_images import ACCEPTED, REJECTED


TEXT = {**REJECTED, 'reason_keys': ['unwanted_text'],
        'diagnosis': {'cause': 'prompt_weakness', 'confidence': .1, 'evidence': [], 'suggested_action': 'repair_prompt'}}


class LadderExperimentTests(unittest.TestCase):
    def setup_case(self, directory):
        store = Store(directory)
        config = {'image_url': 'http://qwen', 'tts_url': 'http://tts', 'image_width': 1536, 'image_height': 864,
                  'image_provider_registry': read(ROOT / 'image-providers.json')}
        library = {'style': 'editorial', 'assets': [{'id': 'A001', 'prompt': 'A rugged plateau with a stone fortress.', 'purpose': 'Historical setting', 'acceptance': []}],
                   'orientations': ['landscape'], 'scene_contexts_by_asset': {'A001': [{'copy_density': 'minimal'}]}}
        settings = read(ROOT / 'pilots/image-ladder/settings.json')
        return store, config, library, settings

    @contextlib.contextmanager
    def engine(self, reviews):
        generated = []
        repairs = []
        def render(config, store, root, params):
            root.mkdir(parents=True, exist_ok=True)
            candidate = root / 'candidate-0.png'
            candidate.write_bytes(b'test image ' + str(len(generated)).encode())
            write(root / 'provider-0.json', {'params': params})
            generated.append((config['image_provider_id'], copy.deepcopy(params)))
            return candidate
        def repair(*args):
            repairs.append(args)
            return f'A plain stone fortress on dry rugged terrain, repaired viewpoint {len(repairs)}.'
        with patch('infofilm.image_experiment.provider_session', return_value=contextlib.nullcontext()) as session, \
             patch('infofilm.image_experiment.render_take', side_effect=render), \
             patch('infofilm.media.review_scene_image', side_effect=reviews) as review, \
             patch('infofilm.media.repair_image_prompt', side_effect=repair):
            yield generated, repairs, session, review

    def test_easy_draft_skips_normal_preview_then_reviews_final(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            with self.engine([ACCEPTED, ACCEPTED]) as (generated, repairs, session, review):
                result = run_ladder(config, store, library, Mock(), settings)
                self.assertEqual([(p['width'], p['steps']) for _, p in generated], [('1024', '8'), ('1536', '25')])
                self.assertEqual([c.kwargs['review_stage'] for c in review.call_args_list], ['preview', 'final'])
                self.assertEqual(session.call_count, 1)
                self.assertFalse(repairs)
                full_config = {**config, 'image_steps': 25, 'image_url': config['image_provider_registry']['providers']['qwen_local']['url']}
                self.assertEqual(generated[0][1]['seed'], str(int(image_identity(full_config, library, library['assets'][0])[:7], 16)))
                self.assertEqual(result['A001']['total_attempts'], 2)
            # Accepted checkpoints return without reopening GPU sessions.
            with patch('infofilm.image_experiment.provider_session', side_effect=AssertionError('already accepted')):
                self.assertEqual(run_ladder(config, store, library, Mock(), settings), result)
            store.db.close()

    def test_draft_text_failure_gets_correction_and_normal_steps_not_escalation(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            with self.engine([TEXT, ACCEPTED, ACCEPTED]) as (generated, repairs, _, _):
                run_ladder(config, store, library, Mock(), settings)
                self.assertEqual([p['steps'] for _, p in generated], ['8', '25', '25'])
                self.assertEqual(len(repairs), 1)
                self.assertIn('repaired viewpoint 1', generated[1][1]['prompt'])
                self.assertTrue(all(provider == 'qwen_local' for provider, _ in generated))
            store.db.close()

    def test_only_normal_step_repairs_can_escalate_and_new_tier_resets_counter(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            with self.engine([TEXT, TEXT, TEXT, ACCEPTED, ACCEPTED]) as (generated, repairs, session, _):
                result = run_ladder(config, store, library, Mock(), settings)
                self.assertEqual([p for p, _ in generated], ['qwen_local'] * 3 + ['flux_dev_local'] * 2)
                self.assertEqual([p['steps'] for _, p in generated], ['8', '25', '25', '6', '20'])
                self.assertEqual(len(repairs), 3)
                self.assertEqual(session.call_count, 2)
                self.assertEqual(result['A001']['total_attempts'], 5)
            store.db.close()

    def test_final_semantic_failure_returns_to_normal_preview(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            with self.engine([ACCEPTED, REJECTED, ACCEPTED, ACCEPTED]) as (generated, repairs, _, _):
                run_ladder(config, store, library, Mock(), settings)
                self.assertEqual([p['width'] for _, p in generated], ['1024', '1536', '1024', '1536'])
                self.assertEqual(len(repairs), 1)
            store.db.close()

    def test_final_detail_failure_repairs_at_full_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            artifact = {**REJECTED, 'reason_keys': ['visual_artifact']}
            with self.engine([ACCEPTED, artifact, ACCEPTED]) as (generated, _, _, _):
                run_ladder(config, store, library, Mock(), settings)
                self.assertEqual([p['width'] for _, p in generated], ['1024', '1536', '1536'])
            store.db.close()

    def test_late_preview_approval_preserves_final_render_slot(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            with self.engine([REJECTED, REJECTED, REJECTED, ACCEPTED, ACCEPTED]) as (generated, _, _, _):
                result = run_ladder(config, store, library, Mock(), settings)
                self.assertEqual(result['A001']['total_attempts'], 5)
                self.assertEqual([p['width'] for _, p in generated], ['1024'] * 4 + ['1536'])
                self.assertEqual(result['A001']['provider_id'], 'qwen_local')
            store.db.close()

    def test_low_steps_and_normal_steps_share_tier_and_overall_budgets(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            config['image_provider_registry']['policies']['standard_then_premium']['max_total_attempts'] = 3
            with self.engine([TEXT] * 3) as (generated, _, _, _):
                self.assertEqual(run_ladder(config, store, library, Mock(), settings), {})
                self.assertEqual(len(generated), 2)
                self.assertTrue(all(p == 'qwen_local' for p, _ in generated))
            self.assertEqual(read(store.root / 'ladder-state.json')['status'], 'exhausted')
            store.db.close()

    def test_standard_only_does_not_recycle_budget_or_escalate(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            config['image_provider_policy'] = 'standard_only'
            with self.engine([TEXT] * 5) as (generated, _, session, _):
                self.assertEqual(run_ladder(config, store, library, Mock(), settings), {})
                self.assertEqual(len(generated), 4)
                self.assertEqual(session.call_count, 1)
            store.db.close()

    def test_flux_fallback_after_exhausted_prompt_repairs_is_explicit_and_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            weakness = {**REJECTED, 'diagnosis': {'cause': 'prompt_weakness', 'confidence': .95,
                'evidence': ['Scene wording still fails'], 'suggested_action': 'repair_prompt'}}
            with self.engine([weakness] * 4 + [ACCEPTED, ACCEPTED]) as (generated, _, _, _):
                result = run_ladder(config, store, library, Mock(), settings)
                self.assertEqual([p for p, _ in generated], ['qwen_local'] * 4 + ['flux_dev_local'] * 2)
                self.assertEqual(result['A001']['total_attempts'], 6)
                takes = read(store.root / 'ladder-state.json')['takes']
                self.assertEqual(takes[3]['escalation_trigger'], 'exhaustion_fallback')
                detail = resolution_details('ladder', result['A001'], takes, True)
                self.assertEqual(detail['verified_resolution'], 'solved_through_flux')
                self.assertEqual(detail['preview_approval_stage'], 'draft')
            store.db.close()

    def test_full_ladder_and_flux_exhaustion_stops_at_ten_including_drafts(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            weakness = {**REJECTED, 'diagnosis': {'cause': 'prompt_weakness', 'confidence': .95,
                'evidence': ['Scene wording still fails'], 'suggested_action': 'repair_prompt'}}
            with self.engine([weakness] * 10) as (generated, _, _, _):
                self.assertEqual(run_ladder(config, store, library, Mock(), settings), {})
                self.assertLessEqual(len(generated), 10)
                self.assertEqual([p for p, _ in generated], ['qwen_local'] * 4 + ['flux_dev_local'] * 4)
            self.assertEqual(read(store.root / 'ladder-state.json')['status'], 'exhausted')
            store.db.close()

    def test_solution_categories_require_independent_final_acceptance(self):
        takes = [{'provider': 'qwen_local', 'stage': 'preview', 'review': ACCEPTED},
                 {'provider': 'qwen_local', 'stage': 'final', 'review': ACCEPTED}]
        selected = {'provider_id': 'qwen_local'}
        good = resolution_details('ladder', selected, takes, True)
        self.assertEqual(good['verified_resolution'], 'solved_through_ladder')
        self.assertEqual(good['preview_approval_stage'], 'preview')
        self.assertEqual(resolution_details('ladder', selected, takes, False)['verified_resolution'], 'unresolved')
        self.assertEqual(resolution_details('ladder', selected, takes, None)['verified_resolution'], 'audit_pending')
        self.assertEqual(resolution_details('ladder', None, takes, None)['verified_resolution'], 'unresolved')

    def test_repair_interruption_resumes_without_regenerating_reviewed_take(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            with self.engine([TEXT]):
                with patch('infofilm.media.repair_image_prompt', side_effect=RuntimeError('LLM unavailable')):
                    with self.assertRaisesRegex(RuntimeError, 'LLM unavailable'):
                        run_ladder(config, store, library, Mock(), settings)
            state = read(store.root / 'ladder-state.json')
            self.assertEqual(state['pending_transition'], 'preview')
            self.assertEqual(len(state['takes']), 1)
            with self.engine([ACCEPTED, ACCEPTED]) as (generated, repairs, _, _):
                result = run_ladder(config, store, library, Mock(), settings)
                self.assertEqual(len(generated), 2)
                self.assertEqual(len(repairs), 1)
                self.assertEqual(result['A001']['total_attempts'], 3)
            store.db.close()

    def test_corruption_stops_before_prompt_repair_or_escalation(self):
        review = {**REJECTED, 'reason_keys': ['backend_corruption']}
        self.assertEqual(ladder_transition('draft', review, 'recheck', has_next_tier=True, attempts_left=True), 'backend_error')
        with tempfile.TemporaryDirectory() as d:
            store, config, library, settings = self.setup_case(d)
            with self.engine([review]) as (_, repairs, _, _):
                with self.assertRaisesRegex(RuntimeError, 'backend diagnosis'):
                    run_ladder(config, store, library, Mock(), settings)
                self.assertFalse(repairs)
            store.db.close()

    def test_unknown_qwen_submission_cannot_be_repeated(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, _, _ = self.setup_case(d)
            root = store.root / 'take'
            params = {'prompt': 'A fortress', 'width': '1024', 'height': '576'}
            write(root / 'provider-0.json', {'state': 'submission_started', 'params': params})
            with patch('infofilm.media.request') as request:
                with self.assertRaisesRegex(RuntimeError, 'Unknown Qwen submission'):
                    render_take(config, store, root, params)
                request.assert_not_called()
            store.db.close()

    def test_cached_qwen_take_checks_dimensions_without_resubmission(self):
        with tempfile.TemporaryDirectory() as d:
            store, config, _, _ = self.setup_case(d)
            root = store.root / 'take'
            root.mkdir()
            image = Image.new('RGB', (1024, 576), 'tan')
            image.putpixel((0, 0), (0, 0, 0))
            image.save(root / 'candidate-0.png')
            params = {'prompt': 'A fortress', 'width': '1024', 'height': '576'}
            with patch('infofilm.media.request') as request:
                self.assertEqual(render_take(config, store, root, params), root / 'candidate-0.png')
                with self.assertRaisesRegex(ValueError, 'expected requested'):
                    render_take(config, store, root, {**params, 'width': '1536'})
                request.assert_not_called()
            store.db.close()

    def test_current_arm_calls_production_loop_and_separate_final_audit(self):
        from tests.test_image_providers import ProviderTests
        with tempfile.TemporaryDirectory() as d:
            fixture, original_store, config, library, models = ProviderTests().setup_run(Path(d) / 'fixture')
            case = {'case_id': 'T01', 'asset_id': library['assets'][0]['id'], 'library': library,
                    'historical_status': 'recovered', 'source_context': {'subject': 'History', 'evidence_title': 'History', 'facts': [], 'creative_brief': {}}}
            with ProviderTests().providers(fixture) as (api, _, studio), patch('infofilm.image_experiment.MeasuredModels', return_value=models):
                result = run_case(case, config, Path(d) / 'experiment', 'current', {})
                self.assertEqual(result['verified_resolution'], 'solved_through_current')
                self.assertEqual(result['attempts'], 1)
                self.assertEqual(len([c for c in api.call_args_list if c.args[3] == '/api/generate']), 1)
                self.assertEqual(models.call.call_count, 2)
                self.assertIn('"previous_provider_attempts": []', models.call.call_args.args[2])
                studio.assert_not_called()
                self.assertEqual(run_case(case, config, Path(d) / 'experiment', 'current', {}), result)
                self.assertEqual(models.call.call_count, 2)
            original_store.db.close()

    def test_collection_preserves_failed_initial_prompt_and_filters_review_context_files(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / 'project'
            store, config, library, _ = self.setup_case(project)
            config['image_steps'] = 25
            write(project / 'config-snapshot.json', config)
            write(project / 'scene-image-prompts.json', library)
            root = project / 'images' / image_identity(config, library, library['assets'][0])[:20]
            original = 'An original fortress overlooking a rocky valley.'
            for i, review in enumerate([REJECTED, ACCEPTED]):
                write(root / f'provider-{i}.json', {'params': {'prompt': 'Shared visual style: historic\nScene illustration: ' + (original if i == 0 else 'Repaired version')}})
                write(root / f'review-{i}.json', review)
            write(root / 'review-context.json', {'not': 'a numbered review'})
            before = digest((project / 'scene-image-prompts.json').read_bytes())
            corpus = collect_cases(d, 1)
            self.assertEqual(validate_corpus(corpus)[0]['library']['assets'][0]['prompt'], original)
            self.assertEqual(corpus['cases'][0]['historical_status'], 'recovered')
            self.assertEqual(digest((project / 'scene-image-prompts.json').read_bytes()), before)
            corpus['cases'][0]['library']['assets'][0]['prompt'] = 'tampered'
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                validate_corpus(corpus)
            store.db.close()

    def test_comparison_rejects_unmatched_plans_and_marks_partial_pairs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            plan = {'case_ids': ['T01'], 'settings': {'preview_size': [1024, 576]}}
            write(root / 'current/plan.json', plan)
            write(root / 'ladder/plan.json', plan)
            report = compare(root)
            self.assertFalse(report['complete'])
            self.assertEqual(report['paired_cases'], 0)
            write(root / 'ladder/plan.json', {**plan, 'settings': {}})
            with self.assertRaisesRegex(ValueError, 'different cases'):
                compare(root)


if __name__ == '__main__':
    unittest.main()
