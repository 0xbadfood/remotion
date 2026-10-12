import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from infofilm.core import ROOT, Store, configuration, digest, read, write
from infofilm.image_providers import escalation_decision, image_review_errors, validate_registry
from infofilm.media import generate_images, image_identity
from tests import test_images as fixtures
from tests.test_images import ACCEPTED, REJECTED, CORRECTED


def pixels():
    image = Image.new('RGB', (32, 32), 'tan')
    image.putpixel((0, 0), (0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def rejected(cause='provider_weakness', confidence=.95):
    return {**REJECTED, 'diagnosis': {'cause': cause, 'confidence': confidence,
        'evidence': ['Both the original composition and the simplified parchment scene retained the same malformed object.'],
        'suggested_action': 'escalate' if cause == 'provider_weakness' else 'repair_prompt'}}


class ProviderTests(unittest.TestCase):
    def setup_run(self, directory):
        fixture = fixtures.ImageRecoveryTests()
        store, config, library, models = fixture.setup_run(directory)
        registry = read(ROOT / 'image-providers.json')
        registry['providers']['qwen_local']['url'] = config['image_url']
        registry['providers']['flux_dev_local']['park_idle_services'] = False
        # These tests exercise escalation to the local Flux tier, whatever the live registry's premium tier is.
        registry['policies']['standard_then_premium']['tiers'][1] = {'provider': 'flux_dev_local', 'max_attempts': 5}
        config['image_provider_registry'] = registry
        return fixture, store, config, library, models

    @contextlib.contextmanager
    def providers(self, fixture):
        def studio(config, store, root, attempt, params):
            root.mkdir(parents=True, exist_ok=True)
            write(root / f'provider-{attempt}.json', {'params': params, 'job_id': 'flux-job'})
            (root / f'candidate-{attempt}.png').write_bytes(pixels())
            return {'engine': 'flux2'}
        with fixture.provider() as (api, wait), \
             patch('infofilm.image_studio.render_studio', side_effect=studio) as render, \
             patch('infofilm.image_studio.release_qwen'), \
             patch('infofilm.media.httpx.Client') as client:
            client.return_value.__enter__.return_value.get.return_value = Mock(content=pixels())
            yield api, wait, render

    def test_escalates_only_failed_asset_after_meaningful_repair_and_resumes_without_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            library['assets'] = [{**library['assets'][0], 'id': 'A005'}, library['assets'][0]]
            models.call.side_effect = [ACCEPTED, rejected(), rejected(), ACCEPTED]
            with self.providers(fixture) as (api, _, studio):
                result = generate_images(config, store, library, models)
            self.assertEqual(result['A005']['provider_id'], 'qwen_local')
            self.assertEqual(result['A006']['provider_id'], 'flux_dev_local')
            self.assertEqual(result['A006']['tier_attempt'], 1)
            self.assertEqual(result['A006']['total_attempts'], 3)
            self.assertEqual(studio.call_count, 1)
            self.assertEqual(len([call for call in api.call_args_list if call.args[3] == '/api/generate']), 3)
            primary = store.root / 'images' / image_identity(config, library, library['assets'][0])[:20] / 'selected.json'
            self.assertTrue(primary.exists())  # legacy Qwen cache identity remains usable
            primary_hash = digest(primary.read_bytes())
            details = json.loads(store.db.execute("SELECT detail FROM events WHERE state='escalating'").fetchone()[0])
            self.assertEqual(details['tier_attempt'], 0)
            self.assertEqual(details['total_attempts'], 2)
            report = read(store.root / 'image-escalation-report.json')
            self.assertEqual(len(report['histories']['A006']), 3)
            # The premium planner sees previous provider settings and reviews.
            self.assertEqual(len(models.loop.call_args.args[2]['previous_attempts']), 2)
            with patch('infofilm.media.gpu_lease', side_effect=AssertionError('Already accepted')):
                self.assertEqual(generate_images(config, store, library, models), result)
            self.assertEqual(read(store.root / 'image-escalation-report.json')['histories'], report['histories'])
            self.assertEqual(digest(primary.read_bytes()), primary_hash)
            store.db.close()

    def test_conflicting_provider_diagnosis_still_requires_confidence_and_repair(self):
        review = rejected()
        review['diagnosis']['suggested_action'] = 'repair_prompt'
        history = [{'settings': {'prompt': 'A fortress amid rocky terrain.'}},
                   {'settings': {'prompt': 'An unoccupied stone fortress across barren rocky hills.'}}]
        self.assertEqual(escalation_decision(review, history, {}), 'escalate')
        self.assertEqual(escalation_decision(review, history[:1], {}), 'recheck')
        self.assertEqual(escalation_decision(review, history[:1] * 5, {}), 'recheck')
        self.assertEqual(escalation_decision(review, history, {'image_prior_count': 1}), 'recheck')
        low_confidence = copy.deepcopy(review)
        low_confidence['diagnosis']['confidence'] = .3
        self.assertEqual(escalation_decision(low_confidence, history, {}), 'recheck')
        no_evidence = copy.deepcopy(review)
        no_evidence['diagnosis']['evidence'] = []
        self.assertEqual(escalation_decision(no_evidence, history, {}), 'recheck')
        for action in ['recheck', 'diagnose_backend', 'unknown']:
            changed = copy.deepcopy(review)
            changed['diagnosis']['suggested_action'] = action
            self.assertEqual(escalation_decision(changed, history, {}), 'recheck')
        accepted = {**review, 'pass': True}
        self.assertNotEqual(escalation_decision(accepted, history, {}), 'escalate')

    def test_unwanted_text_rule_overrides_diagnosis_but_keeps_repair_gates(self):
        review = {**rejected('prompt_weakness', confidence=.1), 'reason_keys': ['unwanted_text']}
        review['diagnosis']['evidence'] = []
        history = [{'settings': {'prompt': 'A dark throne room.'}},
                   {'settings': {'prompt': 'An empty stone hall with a plain polished throne.'}}]
        self.assertEqual(escalation_decision(review, history, {}), 'escalate')
        self.assertEqual(escalation_decision(review, history[:1], {}), 'recheck')
        self.assertEqual(escalation_decision(review, history[:1] * 5, {}), 'recheck')
        self.assertEqual(escalation_decision(review, history, {'image_prior_count': 1}), 'recheck')
        corruption = {**review, 'reason_keys': ['unwanted_text', 'backend_corruption']}
        self.assertEqual(escalation_decision(corruption, history, {}), 'diagnose_backend')
        prose_only = rejected('prompt_weakness')
        prose_only['issues'] = ['Unwanted text']
        self.assertEqual(escalation_decision(prose_only, history, {}), 'repair_prompt')

    def test_strict_reason_keys_cannot_route_malformed_or_accepted_reviews(self):
        self.assertEqual(image_review_errors(ACCEPTED), [])
        self.assertEqual(image_review_errors(REJECTED), [])
        history = [{'settings': {'prompt': 'Original scene.'}}, {'settings': {'prompt': 'Repaired scene.'}}]
        for keys in [None, 'unwanted_text', ['unwanted_text', 'unknown'], ['unwanted_text', 'unwanted_text'], [False], []]:
            review = {**rejected(), 'reason_keys': keys}
            self.assertTrue(image_review_errors(review))
            self.assertEqual(escalation_decision(review, history, {}), 'recheck')
        review = {**ACCEPTED, 'reason_keys': ['unwanted_text']}
        self.assertTrue(image_review_errors(review))
        self.assertNotEqual(escalation_decision(review, history, {}), 'escalate')

    def test_unwanted_text_escalates_with_low_confidence_prompt_diagnosis(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            text_review = {**rejected('prompt_weakness', .1), 'reason_keys': ['unwanted_text']}
            models.call.side_effect = [text_review, text_review, ACCEPTED]
            with self.providers(fixture) as (api, _, studio):
                result = generate_images(config, store, library, models)
                self.assertEqual(len([call for call in api.call_args_list if call.args[3] == '/api/generate']), 2)
                self.assertEqual(studio.call_count, 1)
            self.assertEqual(result['A006']['provider_id'], 'flux_dev_local')
            event = store.db.execute("SELECT detail FROM events WHERE state='escalate_recommended'").fetchone()
            self.assertEqual(json.loads(event[0])['routing_basis'], 'unwanted_text_rule')
            store.db.close()

    def test_cached_exhausted_conflicting_diagnosis_escalates_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            conflict = rejected()
            conflict['diagnosis']['suggested_action'] = 'repair_prompt'
            models.call.side_effect = [rejected('prompt_weakness')] * 4 + [conflict]
            original_decision = escalation_decision
            # Reproduce the former routing rule to save a five-take failed run.
            def old_decision(review, history, current):
                if review.get('diagnosis', {}).get('suggested_action') == 'repair_prompt' and review.get('diagnosis', {}).get('cause') == 'provider_weakness':
                    return 'recheck'
                return original_decision(review, history, current)
            with self.providers(fixture) as (api, _, studio):
                with patch('infofilm.image_providers.escalation_decision', side_effect=old_decision):
                    with self.assertRaisesRegex(RuntimeError, 'A006'):
                        generate_images(config, store, library, models)
                self.assertEqual(read(store.root / 'image-escalation-report.json')['assets']['A006']['action'], 'recheck')
                # Cached legacy reviews predate the strict contract; retain them
                # and recover from their diagnosis without a fresh review call.
                legacy_path = fixture.root(store, config, library) / 'review-4.json'
                legacy = read(legacy_path)
                legacy.pop('reason_keys')
                write(legacy_path, legacy)
                self.assertEqual(len([call for call in api.call_args_list if call.args[3] == '/api/generate']), 5)
                studio.assert_not_called()
                models.call.side_effect = [ACCEPTED]
                result = generate_images(config, store, library, models)
                self.assertEqual(result['A006']['provider_id'], 'flux_dev_local')
                self.assertEqual(result['A006']['total_attempts'], 6)
                self.assertEqual(result['A006']['tier_attempt'], 1)
                self.assertEqual(studio.call_count, 1)
                self.assertEqual(len([call for call in api.call_args_list if call.args[3] == '/api/generate']), 5)
                report = read(store.root / 'image-escalation-report.json')
                self.assertEqual(len(report['histories']['A006']), 6)
            store.db.close()

    def test_prompt_weakness_exhaustion_does_not_spend_premium_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            models.call.return_value = rejected('prompt_weakness')
            with self.providers(fixture) as (api, _, studio):
                with self.assertRaisesRegex(RuntimeError, 'A006'):
                    generate_images(config, store, library, models)
            self.assertEqual(len([call for call in api.call_args_list if call.args[3] == '/api/generate']), 5)
            studio.assert_not_called()
            self.assertEqual(read(store.root / 'image-escalation-report.json')['assets']['A006']['action'], 'repair_prompt')
            store.db.close()

    def test_overall_budget_caps_premium_and_repeating_run_does_not_generate_again(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            config['image_provider_registry']['policies']['standard_then_premium']['max_total_attempts'] = 3
            models.call.return_value = {**rejected('prompt_weakness', .1), 'reason_keys': ['unwanted_text']}
            with self.providers(fixture) as (api, _, studio):
                with self.assertRaises(RuntimeError):
                    generate_images(config, store, library, models)
                self.assertEqual(studio.call_count, 1)
                models.reset_mock()
                with self.assertRaises(RuntimeError):
                    generate_images(config, store, library, models)
                self.assertEqual(studio.call_count, 1)
                self.assertEqual(len([call for call in api.call_args_list if call.args[3] == '/api/generate']), 2)
                models.call.assert_not_called()
                models.loop.assert_not_called()
            report = read(store.root / 'image-escalation-report.json')
            self.assertEqual(len(report['histories']['A006']), 3)
            store.db.close()

    def test_blank_backend_output_is_not_repaired_or_escalated(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            blank = io.BytesIO()
            Image.new('RGB', (32, 32), 'white').save(blank, format='PNG')
            with self.providers(fixture) as (api, _, studio), patch('infofilm.media.httpx.Client') as client:
                client.return_value.__enter__.return_value.get.return_value = Mock(content=blank.getvalue())
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'uniform blank image'):
                        generate_images(config, store, library, models)
                self.assertEqual(len([call for call in api.call_args_list if call.args[3] == '/api/generate']), 1)
                studio.assert_not_called()
                models.call.assert_not_called()
                models.loop.assert_not_called()
            self.assertEqual(read(store.root / 'image-escalation-report.json')['assets']['A006']['status'], 'backend_or_adapter_failed')
            self.assertEqual(read(store.root / 'image-escalation-report.json')['assets']['A006']['total_attempts'], 1)
            store.db.close()

    def test_unknown_submission_cannot_be_hidden_by_escalation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            root = fixture.root(store, config, library)
            fixture.checkpoint(root, 0, candidate=False, review=False, submitted=False)
            with self.providers(fixture) as (api, _, studio):
                with self.assertRaisesRegex(RuntimeError, 'Unknown image submission outcome'):
                    generate_images(config, store, library, models)
            self.assertFalse(any(call.args[3] == '/api/generate' for call in api.call_args_list))
            studio.assert_not_called()
            store.db.close()

    def test_reviewer_backend_diagnosis_stops_instead_of_changing_models(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            models.call.return_value = {**REJECTED, 'diagnosis': {'cause': 'backend_failure',
                'confidence': .95, 'evidence': ['Output appears technically corrupted'], 'suggested_action': 'diagnose_backend'}}
            with self.providers(fixture) as (api, _, studio):
                with self.assertRaisesRegex(RuntimeError, 'needs backend diagnosis'):
                    generate_images(config, store, library, models)
                self.assertEqual(len([call for call in api.call_args_list if call.args[3] == '/api/generate']), 1)
                studio.assert_not_called()
                models.loop.assert_not_called()
            store.db.close()

    def test_partial_acceptance_survives_a_later_transport_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            library['assets'] = [{**library['assets'][0], 'id': 'A005'}, library['assets'][0]]
            with self.providers(fixture) as (_, wait, studio):
                wait.side_effect = [{'id': 'new-job', 'url': '/images/new.png'}, RuntimeError('Provider connection failed')]
                with self.assertRaisesRegex(RuntimeError, 'Provider connection failed'):
                    generate_images(config, store, library, models)
                studio.assert_not_called()
            self.assertEqual(list(read(store.root / 'image_manifest.json')), ['A005'])
            report = read(store.root / 'image-escalation-report.json')
            self.assertEqual(report['assets']['A005']['status'], 'accepted')
            self.assertEqual(report['assets']['A006']['total_attempts'], 1)
            self.assertEqual(len(list((store.root / 'images/provider-selections').glob('*.json'))), 1)
            store.db.close()

    def test_last_tier_spends_only_its_own_budget_even_with_escalation_recommendation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            config['image_provider_policy'] = 'standard_only'
            models.call.return_value = {**rejected('prompt_weakness', .1), 'reason_keys': ['unwanted_text']}
            with self.providers(fixture) as (api, _, studio):
                with self.assertRaises(RuntimeError):
                    generate_images(config, store, library, models)
                self.assertEqual(len([call for call in api.call_args_list if call.args[3] == '/api/generate']), 5)
                studio.assert_not_called()
            self.assertEqual(read(store.root / 'image-escalation-report.json')['assets']['A006']['action'], 'stop')
            store.db.close()

    def test_flux_native_payload_matches_delivery_frame_without_negative_conditioning(self):
        from infofilm.image_providers import provider_config
        from infofilm.image_studio import studio_payload
        config = {'image_url': 'http://qwen'}
        provider = read(ROOT / 'image-providers.json')['providers']['flux_dev_local']
        current = provider_config(config, 'flux_dev_local', provider)
        payload = studio_payload(current, {'prompt': CORRECTED, 'width': '1536', 'height': '864', 'steps': '20', 'seed': '123'})
        self.assertEqual(payload['engine'], 'flux2')
        self.assertEqual((payload['width'], payload['height']), (1536, 864))
        self.assertEqual(payload['negative_prompt'], '')

    def test_policy_is_frozen_and_accepted_checksums_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, store, config, library, models = self.setup_run(directory)
            with self.providers(fixture):
                result = generate_images(config, store, library, models)
            changed = copy.deepcopy(config)
            changed['image_provider_registry']['providers']['qwen_local']['settings']['steps'] = 26
            with self.assertRaisesRegex(ValueError, 'different saved'):
                generate_images(changed, store, library, models)
            Path(result['A006']['file']).write_bytes(b'corrupted')
            with self.assertRaisesRegex(ValueError, 'checkpoint is invalid'):
                generate_images(config, store, library, models)
            store.db.close()

    def test_configuration_embeds_registry_and_queued_snapshot_keeps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root / 'providers.json', read(ROOT / 'image-providers.json'))
            write(root / 'config.json', {'image_provider_settings': 'providers.json'})
            config = configuration(root / 'config.json')
            write(root / 'frozen.json', config)
            (root / 'providers.json').unlink()
            self.assertEqual(configuration(root / 'frozen.json'), config)

    def test_settings_and_diagnosis_cannot_choose_arbitrary_providers_or_unbounded_retries(self):
        registry = read(ROOT / 'image-providers.json')
        for mutation in [lambda r: r['providers']['qwen_local'].update(adapter='shell'),
                         lambda r: r['providers']['qwen_local'].update(url='http://user:secret@host'),
                         lambda r: r['policies']['standard_then_premium']['tiers'][0].update(max_attempts=50),
                         lambda r: r['policies']['standard_then_premium'].update(max_total_attempts=True)]:
            changed = copy.deepcopy(registry)
            mutation(changed)
            with self.assertRaises(ValueError):
                validate_registry(changed)
        history = [{'settings': {'prompt': 'A complex colonial scene with several objects.'}},
                   {'settings': {'prompt': CORRECTED}}]
        self.assertEqual(escalation_decision(rejected(), history, {}), 'escalate')
        self.assertEqual(escalation_decision(rejected(confidence=.3), history, {}), 'recheck')
        self.assertEqual(escalation_decision(rejected(), history[:1], {}), 'recheck')
        self.assertEqual(escalation_decision(rejected(), history[:1] * 3, {}), 'recheck')
        small_repair = [{'settings': {'prompt': 'A parchment document bearing a red legal seal.'}},
                        {'settings': {'prompt': 'A parchment document bearing a red legal stamp.'}}]
        self.assertEqual(escalation_decision(rejected(), small_repair, {}), 'escalate')
        punctuation = [{'settings': {'prompt': 'A parchment bearing the red legal seal.'}},
                       {'settings': {'prompt': 'Parchment bearing a red legal seal!'}}]
        self.assertEqual(escalation_decision(rejected(), punctuation, {}), 'recheck')
        self.assertEqual(escalation_decision({'diagnosis': {'cause': 'provider_weakness', 'confidence': True}}, history, {}), 'recheck')
        self.assertEqual(escalation_decision(rejected(), history, {'image_prior_count': 1}), 'recheck')
