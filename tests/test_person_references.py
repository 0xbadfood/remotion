import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from PIL import Image

from infofilm.core import Store, digest, read, write
from infofilm.image_jobs import wait_qwen_job
from infofilm.image_recovery import current_image_review, generation_prompt, render_take
from infofilm.image_studio import studio_payload
from infofilm.llm import Models
from infofilm.person_reference_worker import (commons_candidates, download_portrait, public_url,
                                            research_person, selection_errors, tavily_candidates)
from infofilm.person_references import (bind_reference_params, build_contact_card, person_errors,
    person_key, prepare_person_references, reference_settings, submit_qwen_job)
from infofilm.planning import make_image_prompts
from test_prompt_batches import PromptBatchTests


PERSON = {'name': 'Albert Einstein', 'appearance_context': 'Later adult life, 1940s and 1950s',
          'reference_kind': 'photograph'}


def pixels(size=(240, 320)):
    image = Image.new('RGB', size, '#b09070')
    image.putpixel((0, 0), (0, 0, 0))
    output = io.BytesIO(); image.save(output, format='PNG')
    return output.getvalue()


class PersonReferenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name); self.store = Store(self.root/'project')
        self.addCleanup(self.store.db.close)
        self.settings = reference_settings({'person_references': {'enabled': True}})
        self.person = copy.deepcopy(PERSON)
        self.portrait = self.root/'portrait.png'; self.portrait.write_bytes(pixels())
        self.ref = {'file': str(self.portrait), 'sha256': digest(self.portrait.read_bytes()),
                    'person': self.person, 'status': 'ready', 'depiction_kind': 'photograph',
                    'source_url': 'https://example.org/archive/einstein', 'license': 'Public domain'}

    def card(self):
        return build_contact_card(self.root/'references', [self.ref], self.settings)

    def test_card_has_only_scene_cast_labels_and_source_hashes_and_reuses_file(self):
        card = self.card()
        self.assertEqual([p['person']['name'] for p in card['people']], ['Albert Einstein'])
        self.assertEqual(card['people'][0]['source_sha256'], self.ref['sha256'])
        self.assertEqual(card['people'][0]['source_url'], self.ref['source_url'])
        with Image.open(card['file']) as image:
            self.assertEqual(list(image.size), card['size'])
        with patch('infofilm.person_references.Image.new', side_effect=AssertionError('No rebuild')):
            self.assertEqual(self.card(), card)
        self.portrait.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            self.card()

    def test_changed_card_label_cannot_rebind_a_valid_photo_to_another_person(self):
        card = self.card()
        manifest = Path(card['file']).parent/'manifest.json'
        corrupted = copy.deepcopy(card)
        corrupted['people'][0]['person']['name'] = 'Another public figure'
        write(manifest, corrupted)
        with self.assertRaisesRegex(ValueError, 'source bindings changed'):
            self.card()

    def test_empty_cast_needs_no_research_or_card(self):
        config = {'person_references': {'enabled': True}, 'planner_url': 'http://planner', 'reviewer_url': 'http://reviewer'}
        library = {'assets': [{'id': 'A001', 'people_in_frame': []}]}
        with patch('infofilm.person_references.subprocess.run') as worker:
            result = prepare_person_references(config, self.store, library)
        worker.assert_not_called()
        self.assertNotIn('reference_card', result['assets'][0])
        self.assertEqual(result['assets'][0]['person_reference_status']['referenced'], 0)

    def test_research_ready_and_missing_references_are_bound_only_to_their_scene(self):
        config = {'person_references': {'enabled': True}, 'planner_url': 'http://planner', 'reviewer_url': 'http://reviewer'}
        ancient = {'name': 'Ancient ruler', 'appearance_context': 'Reign before photography', 'reference_kind': 'historical_portrait'}
        library = {'assets': [{'id': 'A001', 'people_in_frame': [self.person]},
                              {'id': 'A002', 'people_in_frame': []}, {'id': 'A003', 'people_in_frame': [ancient]}]}
        def worker(command, **kwargs):
            request = read(command[-1])
            write(Path(request['directory'])/'result.json', {'people': {
                person_key(self.person): self.ref,
                person_key(ancient): {'status': 'unavailable', 'person': ancient, 'reason': 'No documented likeness'}}})
        with patch('infofilm.person_references.subprocess.run', side_effect=worker) as run:
            result = prepare_person_references(config, self.store, library)
            self.assertEqual(run.call_count, 1)
        self.assertIn('reference_card', result['assets'][0])
        self.assertNotIn('reference_card', result['assets'][1])
        self.assertNotIn('reference_card', result['assets'][2])
        self.assertEqual(result['assets'][2]['person_reference_status']['unavailable'][0]['reason'], 'No documented likeness')
        with patch('infofilm.person_references.subprocess.run') as run:
            self.assertEqual(prepare_person_references(config, self.store, library), result)
            run.assert_not_called()

    def test_sourcing_budget_and_plan_schema_stop_before_network(self):
        self.assertFalse(reference_settings({})['enabled'])
        self.assertFalse(person_errors([]))
        self.assertTrue(person_errors(None))
        self.assertTrue(person_errors([{**PERSON, 'reference_kind': 'imagined_photo'}]))
        self.assertTrue(person_errors([PERSON, PERSON]))
        config = {'person_references': {'enabled': True, 'max_people': 1},
                  'planner_url': 'http://p', 'reviewer_url': 'http://r'}
        library = {'assets': [{'id': 'A001', 'people_in_frame': [PERSON, {**PERSON, 'name': 'Another public figure'}]}]}
        with patch('infofilm.person_references.subprocess.run') as worker:
            with self.assertRaisesRegex(ValueError, 'budget exceeded'):
                prepare_person_references(config, self.store, library)
            worker.assert_not_called()

    def test_native_qwen_initial_submit_and_missing_job_recovery_upload_same_card(self):
        card = self.card()
        asset = {'reference_card': card}
        params = bind_reference_params({'prompt': 'A documentary scene.', 'width': '512', 'height': '512', 'seed': '12'}, asset)
        submitted = []
        def api(config, service, method, path, **kwargs):
            if path == '/api/status': return {'state': 'loaded'}
            self.assertEqual(path, '/api/generate')
            self.assertNotIn('reference_images', kwargs['data'])
            field, (name, handle, mime) = kwargs['files'][0]
            self.assertEqual(field, 'references'); self.assertEqual(mime, 'image/png')
            submitted.append((kwargs['data'], handle.read()))
            return {'id': 'lost' if len(submitted) == 1 else 'replacement'}
        root = self.root/'take'
        request = httpx.Request('GET', 'http://images/api/jobs/lost')
        error = httpx.HTTPStatusError('Missing', request=request, response=httpx.Response(404, request=request))
        with patch('infofilm.media.request', side_effect=api), \
             patch('infofilm.media.wait_job', side_effect=[error, {'url': '/outputs/scene.png'}]), \
             patch('infofilm.media.httpx.Client') as client:
            client.return_value.__enter__.return_value.get.return_value = Mock(content=pixels((512,512)))
            candidate = render_take({'image_url': 'http://images'}, self.store, root, params, 'A001')
        self.assertTrue(candidate.exists())
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[0], submitted[1])
        self.assertEqual(submitted[0][1], Path(card['file']).read_bytes())
        self.assertEqual(read(root/'provider-0.json')['params'], params)
        self.assertEqual(read(root/'provider-0.json')['job_id'], 'replacement')

    def test_changed_card_blocks_upload_instead_of_silently_losing_conditioning(self):
        card = self.card(); params = bind_reference_params({'prompt': 'Scene'}, {'reference_card': card})
        Path(card['file']).write_bytes(b'changed')
        api = Mock()
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            submit_qwen_job({}, params, api)
        api.assert_not_called()

    def test_flux_transport_preserves_reference_bytes_and_unsupported_engine_blocks(self):
        params = bind_reference_params({'prompt': 'Scene', 'width': '512', 'height': '512', 'steps': '20', 'seed': '7'},
                                       {'reference_card': self.card()})
        payload = studio_payload({'image_engine': 'flux2'}, params)
        import base64
        self.assertEqual(base64.b64decode(payload['referenceImages'][0]), Path(params['reference_images'][0]['file']).read_bytes())
        with self.assertRaisesRegex(ValueError, 'cannot accept'):
            studio_payload({'image_engine': 'zimage'}, params)

    def test_scene_prompt_and_reviewer_receive_reference_with_labels_as_conditioning(self):
        card = self.card()
        asset = {'id': 'A001', 'reference_card': card, 'people_in_frame': [PERSON], 'visual_style': 'Documentary'}
        case = {'visual_message': 'An important scientific discovery.', 'asset_id': 'A001',
                'message_annotation': {'setting': 'Princeton in the 1940s'}, 'library': {'assets': [asset],
                'orientations': ['landscape'], 'scene_contexts_by_asset': {'A001': [{'copy_density': 'minimal'}]}}}
        prompt = generation_prompt(case, 'Einstein at his desk.')
        self.assertIn('panel 1: Albert Einstein', prompt)
        self.assertIn('one composed scene', prompt)
        models = Mock(); models.call.return_value = {'visual_message': case['visual_message'], 'pass': True,
            'reason_keys': [], 'issues': [], 'limitations': [], 'correction': '', 'repair_mode': 'none', 'component_inventory': []}
        current_image_review(models, case, self.portrait, prompt, 'review')
        self.assertEqual(models.call.call_args.kwargs['reference_image_paths'], [Path(card['file'])])
        self.assertIn('first image', models.call.call_args.args[2])

    def test_plan_requires_visible_cast_only_when_new_feature_is_enabled(self):
        fixture = PromptBatchTests(); library, film, evidence = fixture.fixture(2)
        def loop(stage, task, context, validator, **kwargs):
            assets = copy.deepcopy(context['used_assets'])
            for asset in assets:
                asset.update(visual_message='A grounded informative meaning for the illustration.',
                             setting='A concrete subject-appropriate setting.', visual_style='Documentary illustration.')
            result = {'style': context['style'], 'assets': assets,
                'banners': [{'scene_key': c['scene_key'], 'text': '', 'reason': ''}
                            for rows in context['scene_contexts_by_asset'].values() for c in rows]}
            self.assertTrue(validator(result))
            assets[0]['people_in_frame'] = [PERSON]; assets[1]['people_in_frame'] = []
            self.assertEqual(validator(result), [])
            self.assertIn('merely mentioned in narration', task)
            return result
        result = make_image_prompts(Mock(loop=Mock(side_effect=loop)), library, [film], evidence, ['landscape'],
                                    message_recovery=True, person_references=True)
        self.assertEqual(result['assets'][0]['people_in_frame'], [PERSON])
        self.assertEqual(result['assets'][1]['people_in_frame'], [])

    def test_required_cast_left_out_gets_a_targeted_repair_of_chosen_assets_only(self):
        fixture = PromptBatchTests(); library, film, evidence = fixture.fixture(2)
        cast = [{**PERSON, 'chapters': ['C01'], 'show_in': ['C01']}]
        stages = []
        def loop(stage, task, context, validator, **kwargs):
            stages.append(stage)
            if stage == 'scene-image-prompts-cast':
                self.assertEqual(context['missing_people'], ['Albert Einstein'])
                self.assertIn('Repair only', task)
                original = next(a for a in context['current_assets'] if a['id'] == 'A002')
                fixed = {**self.planned[1], 'prompt': 'Einstein at his desk. ' + original['prompt'], 'people_in_frame': [PERSON]}
                self.assertTrue(validator({'assets': [{**fixed, 'people_in_frame': []}]}))   # still missing → error
                self.assertTrue(validator({'assets': [fixed, self.planned[0], self.planned[0]]}))
                self.assertEqual(validator({'assets': [fixed]}), [])
                return {'assets': [fixed]}
            self.assertIn('historical documentary', task); self.assertEqual(context['required_people'], ['Albert Einstein'])
            assets = copy.deepcopy(context['used_assets'])
            for asset in assets:
                asset.update(visual_message='A grounded informative meaning for the illustration.',
                             setting='A concrete subject-appropriate setting.', visual_style='Documentary illustration.',
                             people_in_frame=[])
            self.planned = assets
            return {'style': context['style'], 'assets': assets,
                    'banners': [{'scene_key': c['scene_key'], 'text': '', 'reason': ''}
                                for rows in context['scene_contexts_by_asset'].values() for c in rows]}
        result = make_image_prompts(Mock(loop=Mock(side_effect=loop), spec=['loop']), library, [film], evidence, ['landscape'],
                                    message_recovery=True, person_references=True, film_cast=cast,
                                    required_people=['Albert Einstein'])
        self.assertEqual(stages[-1], 'scene-image-prompts-cast')
        self.assertEqual([a['people_in_frame'] for a in result['assets']], [[], [PERSON]])
        self.assertEqual(result['cast_coverage']['repaired_assets'], ['A002'])
        self.assertEqual(result['scene_contexts_by_asset']['A002'][0]['visual_message'], result['assets'][1]['visual_message'])

    def test_failed_cast_repair_keeps_the_prompts(self):
        fixture = PromptBatchTests(); library, film, evidence = fixture.fixture(1)
        def loop(stage, task, context, validator, **kwargs):
            if stage == 'scene-image-prompts-cast':
                raise RuntimeError('exhausted')
            assets = copy.deepcopy(context['used_assets'])
            for asset in assets:
                asset.update(visual_message='A grounded informative meaning for the illustration.',
                             setting='A concrete subject-appropriate setting.', visual_style='Documentary illustration.', people_in_frame=[])
            return {'style': context['style'], 'assets': assets,
                    'banners': [{'scene_key': c['scene_key'], 'text': '', 'reason': ''}
                                for rows in context['scene_contexts_by_asset'].values() for c in rows]}
        result = make_image_prompts(Mock(loop=Mock(side_effect=loop), spec=['loop']), library, [film], evidence, ['landscape'],
                                    message_recovery=True, person_references=True,
                                    film_cast=[{**PERSON, 'chapters': ['C01']}], required_people=['Albert Einstein'])
        self.assertEqual(result['assets'][0]['people_in_frame'], [])
        self.assertIn('exhausted', result['cast_coverage']['error'])

    def test_pipeline_orders_source_cards_before_generation_and_preserves_review_off(self):
        from types import SimpleNamespace
        from test_creative import example
        from infofilm.pipeline import finish_visuals
        script, timing, storyboard, brief = example()
        film = {'script': script, 'timing': timing, 'requested_seconds': 60}
        config = {'person_references': {'enabled': True}}
        args = SimpleNamespace(review_mode='off', image_review=False, visual_plan_dir=None,
                               image_manifest=None, until='images', orientation='landscape')
        calls = []
        def plan(models, library, *rest, **kwargs):
            self.assertTrue(kwargs['person_references'])
            calls.append('plan')
            assets = [{**a, 'prompt': 'A subject-appropriate historical scene.',
                       'visual_message': 'A sourced informative message for this scene.',
                       'setting': 'An appropriate documented setting.', 'visual_style': 'Documentary',
                       'people_in_frame': [PERSON] if i == 0 else []} for i, a in enumerate(library['assets'])]
            return {**library, 'assets': assets, 'banners': []}
        def research(current, store, library):
            calls.append('research')
            result = copy.deepcopy(library)
            for a in result['assets']:
                a['person_reference_status'] = {'referenced': int(bool(a['people_in_frame'])), 'unavailable': []}
            result['assets'][0]['reference_card'] = self.card()
            return result
        with patch('infofilm.pipeline.make_visual_beats', return_value=storyboard), \
             patch('infofilm.pipeline.make_image_prompts', side_effect=plan), \
             patch('infofilm.person_references.prepare_person_references', side_effect=research), \
             patch('infofilm.pipeline.generate_images') as generate:
            generate.side_effect = lambda *args, **kwargs: calls.append('images') or {}
            finish_visuals(args, config, self.store, Mock(), {'title':'Puppies','facts':[{'id':'F1'}]}, {}, brief, [film])
        self.assertEqual(calls, ['plan', 'research', 'images'])
        self.assertFalse(generate.call_args.args[0]['image_review'])
        first = generate.call_args.args[2]['assets'][0]
        self.assertTrue(first['reference_card']['sha256'])
        saved = read(self.store.root/'film-60/storyboard.json')['scenes'][0]
        self.assertEqual(saved['people_in_frame'], [PERSON])
        self.assertEqual(saved['reference_card_sha256'], first['reference_card']['sha256'])


class PortraitResearchTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {'id': 'S01', 'image_url': 'https://example.org/einstein.jpg',
            'source_url': 'https://example.org/archives/einstein', 'caption': 'Albert Einstein, photographed in 1947.',
            'title': 'Albert Einstein portrait', 'date': '1947', 'artist': 'Photographer',
            'license': 'Public domain', 'license_url': '', 'source_type': 'Archive file metadata'}
        self.selection = {'status': 'ready', 'candidate_id': 'S01', 'caption_quote': self.candidate['caption'],
            'depiction_kind': 'photograph', 'appearance_fit': 'A documented later-life portrait.', 'reason': 'Caption names the sitter.'}

    def test_candidate_selection_cannot_invent_source_quote_or_photograph_type(self):
        self.assertEqual(selection_errors(self.selection, [self.candidate], PERSON), [])
        for changed in [{'candidate_id': 'S99'}, {'caption_quote': 'Invented source caption'},
                        {'depiction_kind': 'historical_portrait'}]:
            self.assertTrue(selection_errors({**self.selection, **changed}, [self.candidate], PERSON))
        nbsp = {**self.candidate, 'caption': self.candidate['caption'].replace(' ', '\u00a0')}
        self.assertEqual(selection_errors(self.selection, [nbsp], PERSON), [])

    def test_same_person_in_other_scenes_reuses_film_wide_portrait_without_new_search(self):
        from infofilm import person_reference_worker as worker
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup); self.root = Path(folder.name)
        self.person, self.settings = copy.deepcopy(PERSON), reference_settings({'person_references': {'enabled': True}})
        portrait = self.root/'portrait.png'; portrait.write_bytes(pixels())
        self.ref = {'file': str(portrait), 'sha256': digest(portrait.read_bytes()), 'status': 'ready',
                    'depiction_kind': 'photograph', 'source_url': 'https://example.org/archive/einstein'}
        later = {**self.person, 'appearance_context': 'Elderly scientist at Princeton, 1950s'}
        request = write(self.root/'request.json', {'people': {'a': self.person}, 'settings': self.settings,
            'directory': str(self.root/'run-a'), 'cache_root': str(self.root/'film-refs'),
            'project': str(self.root/'project'), 'model_config': {'planner_url': 'p', 'reviewer_url': 'r'}})
        def research(person, settings, archive, models, store, tavily_factory, search_root):
            self.assertEqual(search_root.parent.parent.name, 'people-by-name')
            return {**self.ref, 'person': person}
        with patch.object(worker, 'research_person', side_effect=research) as researched, \
             patch.object(worker.sys, 'argv', ['worker', str(request)]):
            worker.main()
            write(request, {**read(request), 'people': {'b': later}, 'directory': str(self.root/'run-b')})
            worker.main()
        self.assertEqual(researched.call_count, 1)
        reused = read(self.root/'run-b'/'result.json')['people']['b']
        self.assertEqual(reused['person'], later)
        self.assertEqual(reused['sha256'], self.ref['sha256'])

    def test_people_born_before_1850_get_no_reference_search(self):
        from infofilm import person_reference_worker as worker
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup); root = Path(folder.name)
        napoleon = {'name': 'Napoleon Bonaparte', 'appearance_context': 'First Consul, 1800', 'reference_kind': 'historical_portrait',
                   'birth_year': 1769}
        self.assertEqual(person_errors([napoleon]), [])
        self.assertTrue(person_errors([{**napoleon, 'birth_year': '1769'}]))
        request = write(root/'request.json', {'people': {'n': napoleon}, 'settings': reference_settings({}),
            'directory': str(root/'run'), 'cache_root': str(root/'refs'), 'project': str(root/'project'),
            'model_config': {'planner_url': 'p', 'reviewer_url': 'r'}})
        with patch.object(worker, 'research_person') as researched, patch.object(worker.sys, 'argv', ['worker', str(request)]):
            worker.main()
        researched.assert_not_called()
        result = read(root/'run'/'result.json')['people']['n']
        self.assertEqual(result['status'], 'unavailable'); self.assertIn('1850', result['reason'])

    def test_reference_download_failure_tries_other_documented_candidate_and_saves_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            models = Mock(); store = Mock()
            second = {**self.candidate, 'id': 'S02', 'image_url': 'https://example.org/other.jpg'}
            models.loop.side_effect = [self.selection, {**self.selection, 'candidate_id': 'S02'}]
            def download(url, path):
                if url == self.candidate['image_url']: raise ValueError('Unavailable download')
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(pixels())
                return {'file': str(path), 'sha256': digest(path.read_bytes())}
            with patch('infofilm.person_reference_worker.commons_candidates', return_value=[self.candidate, second]), \
                 patch('infofilm.person_reference_worker.download_portrait', side_effect=download):
                result = research_person(PERSON, reference_settings({}), root, models, store, Mock())
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(result['image_url'], second['image_url'])
                self.assertEqual(result['source_url'], self.candidate['source_url'])
                self.assertEqual(result['license'], 'Public domain')
                self.assertEqual(len(models.loop.call_args.args[2]['candidates']), 1)
                models.reset_mock()
                self.assertEqual(research_person(PERSON, reference_settings({}), root, models, store, Mock()), result)
                models.loop.assert_not_called()

    def test_no_matching_documented_likeness_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            models = Mock(); models.loop.return_value = {'status': 'unavailable', 'reason': 'Wrong age or sitter'}
            with patch('infofilm.person_reference_worker.commons_candidates', return_value=[self.candidate]), \
                 patch('infofilm.person_reference_worker.tavily_candidates', return_value=[]), \
                 patch('infofilm.person_reference_worker.download_portrait') as download:
                result = research_person(PERSON, reference_settings({}), Path(folder), models, Mock(), Mock())
            self.assertEqual(result['status'], 'unavailable')
            self.assertIn('Wrong age or sitter', result['reason'])
            download.assert_not_called()

    def test_structural_selection_failure_is_saved_as_gap_not_a_whole_film_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            models = Mock(); models.loop.side_effect = RuntimeError('Source selection exhausted')
            with patch('infofilm.person_reference_worker.commons_candidates', return_value=[self.candidate]), \
                 patch('infofilm.person_reference_worker.tavily_candidates', return_value=[]):
                result = research_person(PERSON, reference_settings({}), Path(folder), models, Mock(), Mock())
            self.assertEqual(result['status'], 'unavailable')
            self.assertIn('Source selection exhausted', result['reason'])

    def test_forbidden_commons_uses_other_archives_and_total_fetch_budget_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            request = httpx.Request('GET', 'https://commons.wikimedia.org/w/api.php')
            forbidden = httpx.HTTPStatusError('Robots policy', request=request, response=httpx.Response(403, request=request))
            with patch('infofilm.person_reference_worker.commons_candidates', side_effect=forbidden), \
                 patch('infofilm.person_reference_worker.tavily_candidates', return_value=[]) as search:
                research_person(PERSON, reference_settings({}), Path(folder), Mock(), Mock(), Mock())
            self.assertEqual(search.call_count, 2)
            self.assertTrue(search.call_args_list[0].kwargs['avoid_wikimedia'])
            self.assertFalse(search.call_args_list[0].kwargs['broad'])
            self.assertTrue(search.call_args_list[1].kwargs['broad'])
            self.assertEqual(sum(c.args[3] for c in search.call_args_list), 3)

    def test_private_urls_and_redirects_are_blocked_and_download_is_normalized(self):
        with patch('infofilm.person_reference_worker.socket.getaddrinfo',
                   return_value=[(None,None,None,None,('127.0.0.1',80))]):
            with self.assertRaisesRegex(ValueError, 'private or local'):
                public_url('https://localhost/image.png')
        for url in ['file:///tmp/photo.png', 'https://user:password@example.org/p.png']:
            with self.assertRaises(ValueError): public_url(url)
        with tempfile.TemporaryDirectory() as folder, patch('infofilm.person_reference_worker.public_url') as public, \
             patch('infofilm.person_reference_worker.httpx.Client') as client:
            redirect = Mock(is_redirect=True, headers={'location': 'http://127.0.0.1/image.png'})
            response = Mock(is_redirect=False); response.iter_bytes.return_value = [pixels()]
            client.return_value.__enter__.return_value.stream.return_value.__enter__.side_effect = [redirect, response]
            public.side_effect = [None, ValueError('private or local')]
            with self.assertRaisesRegex(ValueError, 'private or local'):
                download_portrait('https://example.org/photo.png', Path(folder)/'portrait.png')
            public.side_effect = None
            client.return_value.__enter__.return_value.stream.return_value.__enter__.side_effect = [response]
            result = download_portrait('https://example.org/photo.png', Path(folder)/'portrait.png')
            self.assertEqual(result['original_sha256'], digest(pixels()))
            with Image.open(result['file']) as image: self.assertEqual(image.mode, 'RGB')

    def test_tavily_uses_images_from_fetched_pages_not_unbound_search_image_hits(self):
        tavily = Mock()
        tavily.search.return_value = {'results': [{'url': self.candidate['source_url']}],
                                     'images': [{'url': 'https://example.org/unbound.jpg'}]}
        tavily.extract.return_value = {'results': [{'url': self.candidate['source_url'],
            'raw_content': self.candidate['caption'], 'images': [self.candidate['image_url']]}]}
        with tempfile.TemporaryDirectory() as folder, patch('infofilm.person_reference_worker.public_url'):
            results = tavily_candidates(PERSON, tavily, 5, 3, Path(folder))
        self.assertEqual([r['image_url'] for r in results], [self.candidate['image_url']])
        self.assertTrue(tavily.extract.call_args.kwargs['include_images'])

    def test_multimodal_review_transmits_candidate_then_card_and_checkpoints_only_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); candidate = root/'scene.png'; card = root/'card.png'
            candidate.write_bytes(pixels()); card.write_bytes(pixels((400,500)))
            store = Store(root)
            try:
                models = Models({'reviewer_url': 'http://reviewer/v1'}, store)
                with patch('infofilm.llm.httpx.Client') as client:
                    api = client.return_value.__enter__.return_value
                    api.get.return_value.json.return_value = {'data':[{'id':'vision-reviewer'}]}
                    api.post.return_value.json.return_value = {'choices':[{'message': {'content':'{"pass":true}'}, 'finish_reason':'stop'}]}
                    models.call('reviewer', 'reference-review', 'Judge appearance', image_path=candidate, reference_image_paths=[card])
                content = api.post.call_args.kwargs['json']['messages'][1]['content']
                self.assertEqual([c['type'] for c in content], ['text','image_url','image_url'])
                request = read(next((root/'requests').glob('*.request.json')))
                self.assertEqual(request['reference_images'], [str(card)])
                self.assertNotIn('base64', json.dumps(request))
            finally: store.db.close()
