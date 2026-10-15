import contextlib
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from infofilm.core import Store, configuration, digest, read, write
from infofilm.image_recovery import (RepairContractError, clean_repair, current_image_review,
    generate_message_images, negative_wording, repair_scene, repair_errors, review_errors, render_take)
from infofilm.image_pool import ImageWorkerPool, pool_size, validate_pools
from infofilm.resume import images_checkpoint, resume_images
from infofilm.planning import script_text


class ImageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = {**configuration(), 'image_recovery_max_attempts': 3}   # tests pin the take budget, not the live config
        self.config['image_terminal_recovery'] = {'enabled': False}
        self.message = 'Revenue collection became a major source of Company authority.'
        self.asset = {'id': 'A001', 'visual_message': self.message, 'setting': 'Bengal in the eighteenth century.',
                      'visual_style': 'Restrained cinematic historical illustration.',
                      'prompt': 'A Company clerk considers a folded parchment beside a closed ledger.',
                      'fact_ids': ['F01'], 'purpose': 'Revenue authority', 'acceptance': ['Period setting']}
        self.library = {'assets': [self.asset], 'style': 'Subject title with unwanted title cues',
                        'orientations': ['landscape'], 'scene_contexts_by_asset': {'A001': [{'copy_density': 'minimal'}]}}
        self.case = {'asset_id': 'A001', 'visual_message': self.message, 'library': self.library,
                     'source_context': {'facts': [{'id': 'F01', 'claim': self.message}]},
                     'message_annotation': {'setting': self.asset['setting'], 'essential_constraints': 'Keep period and intent.'}}

    def verdict(self, passed=True, mode='targeted'):
        return {'visual_message': self.message, 'pass': passed, 'reason_keys': [] if passed else ['unwanted_text'],
                'issues': [] if passed else ['Generated caption'], 'limitations': [], 'component_inventory': [],
                'repair_mode': 'none' if passed else mode, 'mode_reason': '' if passed else 'A small concrete fix.',
                'correction': '' if passed else 'Use plain parchment.',
                'message_marks': 44 if passed else 30, 'prompt_marks': 26 if passed else 18, 'usability_marks': 18 if passed else 12}

    def replacement(self, text=None, mode='targeted'):
        return {'visual_message': self.message, 'prompt': text or 'A Company clerk holds folded plain parchment beside a closed unmarked ledger.',
                'repair_mode': mode, 'simplifications': ['Remove optional visible pages'] if mode == 'simplify' else [],
                'message_preserved': 'Period document handling conveys revenue administration.'}

    @contextlib.contextmanager
    def engine(self, verdicts, replacements=(), workers=1):
        def generate(config, store, root, params, label):
            root.mkdir(parents=True, exist_ok=True)
            p = root / 'candidate-0.png'
            p.write_bytes(params['prompt'].encode())
            return p
        with patch('infofilm.image_recovery.provider_session', return_value=contextlib.nullcontext()) as session, \
             patch('infofilm.image_recovery.image_pool', side_effect=lambda config: ImageWorkerPool(workers)), \
             patch('infofilm.image_recovery.render_take', side_effect=generate) as render, \
             patch('infofilm.image_recovery.current_image_review', side_effect=verdicts) as review, \
             patch('infofilm.image_recovery.repair_scene', side_effect=replacements) as repair:
            yield session, render, review, repair

    def test_pass_needs_no_mode_reason_or_redundant_format_call(self):
        models = Mock(); models.call.return_value = self.verdict()
        self.assertFalse(review_errors(self.verdict(), self.message))
        current_image_review(models, self.case, Path('/tmp/image.png'), 'Actual prompt', 'test')
        self.assertEqual(models.call.call_count, 1)
        payload = json.loads(models.call.call_args.args[2].split('CURRENT INPUT:\n')[1])
        self.assertEqual(set(payload), {'visual_message', 'current_prompt'})

    def test_negative_wording_is_advisory_with_one_cleanup_and_fallback(self):
        models = Mock(); value = self.replacement('A Company clerk holds a parchment without legible lettering beside a closed ledger.')
        models.call.side_effect = [value, {'prompt': 'bad'}]
        result = repair_scene(models, self.case, self.asset['prompt'], 'Actual prompt', self.verdict(False), 'test')
        self.assertEqual(result['prompt'], value['prompt'])
        self.assertTrue(result['wording_warning'])
        self.assertEqual(models.call.call_count, 2)
        self.assertFalse(repair_errors(value, self.case, self.asset['prompt'], self.verdict(False)))

    def test_wrappers_are_removed_before_unchanged_scene_comparison(self):
        wrapped = {**self.replacement(), 'prompt': 'Setting: ignored\nVisual treatment: ignored\nComposition: ignored\nScene: ' + self.asset['prompt']}
        clean = clean_repair(wrapped)
        self.assertEqual(clean['prompt'], self.asset['prompt'])
        self.assertTrue(repair_errors(clean, self.case, self.asset['prompt'], self.verdict(False)))
        self.assertEqual(clean_repair({'scene_prompt': 'A plain parchment lies on a period table.'})['prompt'], 'A plain parchment lies on a period table.')

    def test_simplification_requested_cannot_be_ignored_or_change_message(self):
        result = self.replacement()
        self.assertTrue(repair_errors(result, self.case, self.asset['prompt'], self.verdict(False, 'simplify')))
        result = self.replacement(mode='simplify'); result['visual_message'] = 'Different meaning'
        self.assertTrue(repair_errors(result, self.case, self.asset['prompt'], self.verdict(False)))

    def test_pass_stops_and_resume_does_not_load_provider(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([self.verdict()]) as (session, render, review, repair):
                    output = generate_message_images(self.config, store, self.library, Mock())
                    self.assertEqual(output['A001']['total_attempts'], 1)
                    repair.assert_not_called()
                    self.assertEqual(review.call_count, 1)
                    prompt = render.call_args.args[3]['prompt']
                    self.assertNotIn(self.library['style'], prompt)
                    self.assertNotIn('banner', prompt.lower())
                with self.engine([]) as (session, render, review, repair):
                    self.assertEqual(generate_message_images(self.config, store, self.library, Mock()), output)
                    session.assert_not_called(); render.assert_not_called()
                progress = [json.loads(r[0]) for r in store.db.execute("SELECT detail FROM events WHERE stage='images' AND state='progress'")]
                self.assertEqual(progress[-1]['accepted_images'], 1)
                self.assertEqual(progress[-1]['remaining_images'], 0)
            finally:
                store.db.close()

    def test_rejected_image_replaced_using_last_scene_at_full_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([self.verdict(False), self.verdict()], [self.replacement(mode='simplify')]) as (_, render, review, repair):
                    output = generate_message_images(self.config, store, self.library, Mock())
                    self.assertEqual(output['A001']['total_attempts'], 2)
                    calls = render.call_args_list
                    self.assertEqual(calls[0].args[3]['width'], calls[1].args[3]['width'])
                    self.assertEqual(calls[0].args[3]['steps'], calls[1].args[3]['steps'])
                    self.assertEqual(repair.call_count, 1)
                    self.assertEqual(output['A001']['provider_id'], 'qwen_local')
            finally:
                store.db.close()

    def test_artistic_exhaustion_continues_next_asset_within_single_provider_budget(self):
        library = copy.deepcopy(self.library)
        library['assets'].append({**self.asset, 'id': 'A002'})
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([self.verdict(False)]*3+[self.verdict()], [self.replacement(), self.replacement(text="A folded parchment and a closed ledger sit on a plain administrative table.", mode='simplify')]) as (_, render, review, repair):
                    with self.assertRaisesRegex(RuntimeError, 'A001'):
                        generate_message_images(self.config, store, library, Mock())
                    self.assertEqual(render.call_count, 4)
                    self.assertIn('A002', read(Path(d)/'image_manifest.json'))
                    report = read(Path(d)/'image-recovery-report.json')
                    self.assertEqual(report['accepted_images'], 1)
                    self.assertEqual(report['unresolved'][0]['attempts'], 3)
            finally:
                store.db.close()

    def parallel_library(self, count):
        library = copy.deepcopy(self.library)
        library['assets'] = [{**self.asset, 'id': f'A{n:03d}'} for n in range(1, count + 1)]
        return library

    def test_film_renders_as_many_images_at_once_as_its_pool_allows(self):
        library, lock, live, peak = self.parallel_library(5), threading.Lock(), [0], [0]
        def generate(config, store, root, params, label):
            with lock:
                live[0] += 1; peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            with lock:
                live[0] -= 1
            root.mkdir(parents=True, exist_ok=True); p = root / 'candidate-0.png'; p.write_bytes(params['prompt'].encode())
            return p
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([], workers=2) as (_, render, review, _r):
                    render.side_effect, review.side_effect = generate, lambda *a, **k: self.verdict()
                    output = generate_message_images(self.config, store, library, Mock())
                self.assertEqual(peak[0], 2)
                self.assertEqual(sorted(output), [a['id'] for a in library['assets']])
                self.assertEqual(sorted(read(Path(d) / 'image_manifest.json')), sorted(output))
                progress = store.db.execute("SELECT COUNT(*) FROM events WHERE stage='images' AND state='progress'").fetchone()[0]
                self.assertEqual(progress, 5)
            finally:
                store.db.close()

    def test_a_failed_image_stops_new_ones_and_keeps_finished_ones(self):
        library = self.parallel_library(4)
        def generate(config, store, root, params, label):
            if label.startswith('A001'):
                raise RuntimeError('factory down')
            time.sleep(0.05)
            root.mkdir(parents=True, exist_ok=True); p = root / 'candidate-0.png'; p.write_bytes(params['prompt'].encode())
            return p
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([], workers=2) as (_, render, review, _r):
                    render.side_effect, review.side_effect = generate, lambda *a, **k: self.verdict()
                    with self.assertRaisesRegex(RuntimeError, 'factory down'):
                        generate_message_images(self.config, store, library, Mock())
                self.assertLess(render.call_count, 4)
                self.assertIn('A002', read(Path(d) / 'image_manifest.json'))
            finally:
                store.db.close()

    def test_pool_size_is_configured_per_provider_or_reported_by_it(self):
        registry = {'providers': {'qwen_local': {'adapter': 'qwen_async'}, 'cloud': {'adapter': 'openrouter_image'}},
                    'pools': {'qwen_local': {'size': 'auto', 'max_in_flight': 4}, 'cloud': {'size': 3}}}
        config = {'image_url': 'http://f', 'image_provider_registry': registry}
        status = {'state': 'loaded', 'factory': {'dispatch': [{'gpu': 0}, {'gpu': 1}]}}
        with patch('infofilm.media.request', return_value=status) as request:
            self.assertEqual(pool_size({**config, 'image_provider_id': 'qwen_local'}), 3)   # the factory's 2 GPU workers + 1 spare
            spare0 = {**registry, 'pools': {**registry['pools'], 'qwen_local': {'size': 'auto', 'spare': 0}}}
            self.assertEqual(pool_size({**config, 'image_provider_registry': spare0, 'image_provider_id': 'qwen_local'}), 2)
            self.assertEqual(request.call_args.args[1:4], ('image', 'GET', '/api/status'))
            self.assertEqual(pool_size({**config, 'image_provider_id': 'cloud'}), 3)        # a rate-limited service
            self.assertEqual(pool_size({**config, 'image_provider_id': 'other'}), 1)        # no pool: one at a time
            self.assertEqual(pool_size({**config, 'image_provider_id': 'qwen_local', 'image_pool_size': 5}), 5)
        with patch('infofilm.media.request', return_value={'state': 'loaded'}):
            self.assertEqual(pool_size({**config, 'image_provider_id': 'qwen_local'}), 2)   # a server without a factory: 1 + spare
        with patch('infofilm.media.request', side_effect=__import__('httpx').ConnectError('down')):
            self.assertEqual(pool_size({**config, 'image_provider_id': 'qwen_local'}), 2)
        for bad in ({'size': 0}, {'size': 'many'}, {'size': 2, 'max_in_flight': 0}, {'size': 'auto', 'spare': -1}):
            with self.assertRaises(ValueError):
                validate_pools({**registry, 'pools': {'cloud': bad}})
        with self.assertRaises(ValueError):
            validate_pools({**registry, 'pools': {'nobody': {'size': 1}}})
        with self.assertRaises(ValueError):
            ImageWorkerPool(0)

    def test_pool_settings_do_not_change_image_identity(self):
        registry = configuration()['image_provider_registry']
        self.assertNotIn('size', json.dumps(registry['providers']))     # pools live beside the providers, not in them
        self.assertIn('qwen_local', registry['pools'])

    def test_pending_repair_resume_reuses_rejected_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with self.engine([self.verdict(False)], [RepairContractError('Malformed repair')]):
                    with self.assertRaises(RuntimeError):
                        generate_message_images(self.config, store, self.library, Mock())
                with self.engine([self.verdict()], [self.replacement()]) as (_, render, review, repair):
                    output = generate_message_images(self.config, store, self.library, Mock())
                    self.assertEqual(render.call_count, 1)
                    self.assertEqual(output['A001']['total_attempts'], 2)
            finally:
                store.db.close()

    def test_unconfirmed_local_submission_is_submitted_again(self):
        # The Qwen factory is local and unbilled: a POST that never confirmed a job id (power cut) is retried.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            params = {'prompt': 'Actual submitted settings'}
            write(root/'provider-0.json', {'params': params, 'state': 'submission_started'})
            store = Mock()
            with patch('infofilm.person_references.submit_qwen_job', return_value={'id': 'fresh'}) as submit, \
                 patch('infofilm.image_jobs.wait_qwen_job', side_effect=RuntimeError('stop after submit')):
                with self.assertRaisesRegex(RuntimeError, 'stop after submit'):
                    render_take({}, store, root, params, 'A001 1/20')
            submit.assert_called_once()
            self.assertEqual(read(root/'provider-0.json')['job_id'], 'fresh')
            self.assertEqual(store.event.call_args_list[0].args[1], 'unconfirmed_submission_retried')

    def test_first_review_format_failure_keeps_candidate_and_enters_premium_recovery(self):
        from infofilm.image_terminal import PROTOCOL as TERMINAL_PROTOCOL, REVIEW_TAG
        config={**self.config,'image_terminal_recovery':{'enabled':True,'provider':'flux_dev_local','max_candidates':1}}
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            def terminal(config,store,root,case,models,policy,**kwargs):
                state=read(root/'message-state.json')
                self.assertEqual(state['takes'],[])
                take=state['pending_review']
                self.assertEqual(digest(Path(take['candidate']).read_bytes()),take['sha256'])
                review={**self.verdict(False),'message_marks':40,'prompt_marks':24,
                        'usability_marks':16,'score':80,'score_protocol':TERMINAL_PROTOCOL}
                return {'asset':case['asset_id'],'file':take['candidate'],'sha256':take['sha256'],
                        'visual_message':self.message,'review':review,'total_attempts':2,
                        'selection_status':'best_available','needs_review':True,'review_tag':REVIEW_TAG,
                        'terminal_recovery':{'protocol':TERMINAL_PROTOCOL}}
            try:
                with self.engine([RepairContractError('Invalid review JSON')]) as (_,render,_,_), \
                     patch('infofilm.image_terminal.recover',side_effect=terminal) as rescue:
                    output=generate_message_images(config,store,self.library,Mock())
                self.assertEqual(render.call_count,1)
                self.assertEqual(rescue.call_count,1)
                self.assertTrue(output['A001']['needs_review'])
                self.assertEqual(read(Path(folder)/'image-recovery-report.json')['unresolved'],[])
                blocked=[json.loads(r[0]) for r in store.db.execute("SELECT detail FROM events WHERE state='repair_blocked'")]
                self.assertTrue(blocked[0]['terminal_recovery_queued'])
            finally:
                store.db.close()

    def test_missing_message_or_unknown_provider_fails_before_gpu(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            try:
                with patch('infofilm.image_recovery.provider_session') as session:
                    with self.assertRaisesRegex(ValueError, 'Unknown'):
                        generate_message_images({**self.config, 'image_recovery_provider': 'unknown'}, store, self.library, Mock())
                    session.assert_not_called()
            finally:
                store.db.close()

    def test_terminal_best_available_resumes_without_loading_standard_or_premium_again(self):
        from infofilm.image_terminal import PROTOCOL as TERMINAL_PROTOCOL, REVIEW_TAG
        config = {**self.config, 'image_recovery_max_attempts': 1,
                  'image_terminal_recovery': {'enabled': True, 'provider': 'flux_dev_local', 'max_candidates': 1}}
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            active = 0
            @contextlib.contextmanager
            def lease():
                nonlocal active
                self.assertEqual(active, 0, 'GPU leases must not nest')
                active += 1
                try:
                    yield
                finally:
                    active -= 1
            def terminal(config, store, root, case, models, policy, **kwargs):
                self.assertTrue(kwargs['session_managed'])
                take = read(root/'message-state.json')['takes'][0]
                review = {**self.verdict(False), 'message_marks': 40, 'prompt_marks': 24,
                          'usability_marks': 16, 'score': 80, 'score_protocol': TERMINAL_PROTOCOL}
                return {'asset': case['asset_id'], 'file': take['candidate'], 'sha256': take['sha256'],
                        'visual_message': self.message, 'review': review, 'total_attempts': 2,
                        'selection_status': 'best_available', 'needs_review': True, 'review_tag': REVIEW_TAG,
                        'terminal_recovery': {'protocol': TERMINAL_PROTOCOL}}
            try:
                with self.engine([self.verdict(False)]) as (session, render, review, repair), \
                     patch('infofilm.image_terminal.recover', side_effect=terminal):
                    session.side_effect = lambda *args: lease()
                    output = generate_message_images(config, store, self.library, Mock())
                    self.assertEqual(session.call_count, 2)
                    self.assertFalse(output['A001']['review']['pass'])
                    self.assertTrue(output['A001']['needs_review'])
                    self.assertEqual(read(Path(folder)/'image-recovery-report.json')['unresolved'], [])
                with self.engine([]) as (session, render, review, repair):
                    self.assertEqual(generate_message_images(config, store, self.library, Mock()), output)
                    session.assert_not_called()
                    render.assert_not_called()
            finally:
                store.db.close()


class ImagesResumeTests(unittest.TestCase):
    def checkpoint(self, p):
        write(p/'project.json', {'subject': 'Example', 'durations': [60], 'tts_engine': 'vibevoice'})
        write(p/'creative-brief.json', {'resolved_mood': 'serious'})
        write(p/'evidence-final.json', {'facts': []})
        write(p/'sources.json', {'results': []})
        write(p/'config-snapshot.json', {})
        audio = p/'voice.wav'; audio.write_bytes(b'validated audio')
        script = {'points': [{'id': 'P01', 'narration': 'An explanation.'}]}
        write(p/'film-60/audio-selection.json', {'script': script, 'audio': str(audio),
            'audio_info': {'audio_sha256': digest(audio.read_bytes())}, 'audit': {'status': 'passed'}})
        write(p/'film-60/timing.json', {'audio_sha256': digest(audio.read_bytes()),
            'text_sha256': digest(script_text(script)), 'words': [{'id': 'W001'}], 'points': [{'id': 'P01'}]})
        return audio

    def test_valid_failed_visual_project_needs_no_existing_video(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); audio = self.checkpoint(p)
            self.assertEqual(len(images_checkpoint(p)[-1]), 1)
            audio.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'narration changed'):
                images_checkpoint(p)

    def test_changed_script_or_failed_audit_blocks_reuse(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); self.checkpoint(p)
            f = read(p/'film-60/audio-selection.json'); f['script']['points'][0]['narration'] = 'Changed.'
            write(p/'film-60/audio-selection.json', f)
            with self.assertRaisesRegex(ValueError, 'script no longer'):
                images_checkpoint(p)
            self.checkpoint(p)
            f = read(p/'film-60/audio-selection.json'); f['audit']['status'] = 'failed'
            write(p/'film-60/audio-selection.json', f)
            with self.assertRaisesRegex(ValueError, 'audio audit'):
                images_checkpoint(p)


class ImageRevisionTests(unittest.TestCase):
    def test_selected_chapter_archive_preserves_c01_and_upstream_then_queues_same_job(self):
        from infofilm.queue import Queue
        from test_longform import checked_stub
        import time
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); parent = root/'parent'; parent.mkdir()
            config = write(root/'config.json', {'image_recovery_mode':'message_v1',
                'image_recovery_provider':'qwen_local', 'image_recovery_max_attempts':3})
            queue = Queue(root/'interface', config)
            c1, c2 = parent/'c01', parent/'c02'
            checked_stub(c1)
            # Use valid audio/MFA checkpoints; no completed video required for C02.
            helper = ImagesResumeTests()
            audio = helper.checkpoint(c2)
            film = read(c2/'film-60/audio-selection.json'); film['requested_seconds'] = 60
            write(c2/'film-60/audio-selection.json', film)
            write(c2/'scene-image-prompts.json', {'old':'plan'})
            write(c2/'images/old/review.json', {'pass':False})
            rows = [{'chapter':{'id':'C01'},'project':str(c1),'duration':60,'status':'succeeded'},
                    {'chapter':{'id':'C02'},'project':str(c2),'duration':60,'status':'failed'}]
            write(parent/'longform-status.json', {'chapters':rows,'completed':1,'failed':1,'total':2})
            payload = {'subject':'Example','generation_mode':'long','orientation':'landscape','project':str(parent)}
            now=time.time()
            with queue.connection() as db:
                db.execute('INSERT INTO jobs(id,subject,request,project,status,created,updated) VALUES(?,?,?,?,?,?,?)',
                    ('job','Example',json.dumps(payload),str(parent),'failed',now,now))
            before = {str(p):p.read_bytes() for p in c1.rglob('*') if p.is_file()}
            preserved = {str(p):p.read_bytes() for p in [audio,c2/'film-60/audio-selection.json',c2/'film-60/timing.json']}
            result = queue.revise_images('job',['C02'])
            self.assertEqual(result['job']['status'],'queued')
            self.assertEqual(result['job']['request']['image_chapters'],['C02'])
            self.assertEqual(result['job']['request']['resume_from'],'images')
            self.assertFalse((c2/'scene-image-prompts.json').exists())
            self.assertTrue((Path(result['revision'])/'C02/scene-image-prompts.json').exists())
            self.assertEqual(before,{str(p):p.read_bytes() for p in c1.rglob('*') if p.is_file()})
            self.assertTrue(all(Path(p).read_bytes()==data for p,data in preserved.items()))
            with self.assertRaisesRegex(ValueError,'stopped'):
                queue.revise_images('job',['C02'])

    def test_long_image_resume_dispatches_selected_chapters_without_research_or_replanning(self):
        from infofilm.longform import create_long
        from test_longform import accepted_plan, checked_stub, creative
        from types import SimpleNamespace
        plan, brief = accepted_plan()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); parent=root/'parent'; parent.mkdir()
            ref=root/'ref.wav';ref.write_bytes(b'reference')
            config=write(parent/'config-snapshot.json', {'planner_url':'http://p','reviewer_url':'http://r',
                'reference_audio':str(ref),'language':'en_indian','tts_url':'http://v','image_url':'http://i'})
            write(parent/'longform-project.json', {'subject':plan['film']['subject'],'orientation':'landscape'})
            write(parent/'chapter-plan.json',plan);write(parent/'orientation-brief.json',brief)
            write(parent/'creative-brief.json',creative())
            selected=['C02','C03']; snapshots={}
            for chapter in plan['chapters']:
                child=parent/'chapters'/f"{parent.name}-{chapter['id'].lower()}";child.mkdir(parents=True)
                write(child/'config-snapshot.json',read(config))
                if chapter['id'] not in selected:
                    checked_stub(child)
                    snapshots[str(child)]={str(p):p.read_bytes() for p in child.rglob('*') if p.is_file()}
            args=SimpleNamespace(config=config,project=parent,subject=plan['film']['subject'],durations=[900],
                orientation='landscape',reference_audio=None,reference_transcript=None,language='en_indian',
                tts_engine='vibevoice',omni_speed=.8,mood='auto',mood_intensity='restrained',visual_balance='auto',music_engine='none',
                review_mode='experimental',until='video',resume_from='images',image_chapters=selected,
                image_recovery_settings={'image_recovery_mode':'message_v1','image_recovery_provider':'qwen_local',
                                         'image_recovery_max_attempts':3})
            calls=[]
            def child(payload,project,store,cid,*extra):
                if payload.get('until')=='script':return project/'attempts/script'   # phase A (scripts) stub
                calls.append(cid);self.assertEqual(payload['resume_from'],'images')
                self.assertEqual(read(payload['config'])['image_recovery_mode'],'message_v1')
                if cid=='C02':raise RuntimeError('Visual failure')
                checked_stub(project);return project/'attempt'
            with patch('infofilm.resume.images_checkpoint') as checkpoint, \
                 patch('infofilm.pipeline.research',side_effect=AssertionError('No research')), \
                 patch('infofilm.longform.plan_film',side_effect=AssertionError('No planning')), \
                 patch('infofilm.longform.run_chapter',side_effect=child), \
                 patch('infofilm.longform.continuity_review') as continuity:
                with self.assertRaisesRegex(RuntimeError,'C02'):
                    create_long(args)
                self.assertEqual(calls,selected)
                self.assertEqual(checkpoint.call_count,2)
                continuity.assert_not_called()
            for project, files in snapshots.items():
                self.assertEqual(files,{str(p):p.read_bytes() for p in Path(project).rglob('*') if p.is_file()})

    def test_image_resume_goes_directly_to_picture_tail(self):
        from infofilm.pipeline import create
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);ImagesResumeTests().checkpoint(p)
            args=SimpleNamespace(project=p,config=p/'config-snapshot.json',durations=[60],
                                 subject='Example',resume_from='images')
            with patch('infofilm.pipeline.finish_visuals') as finish, \
                 patch('infofilm.pipeline.prepare_audio',side_effect=AssertionError('No TTS')), \
                 patch('infofilm.pipeline.research',side_effect=AssertionError('No research')), \
                 patch('infofilm.pipeline.align_audio',side_effect=AssertionError('No MFA')):
                create(args)
                finish.assert_called_once()


class MessagePlanningTests(unittest.TestCase):
    def test_planning_requires_message_and_propagates_it_to_scene_contexts(self):
        from infofilm.planning import make_image_prompts
        library={'style':'Natural illustration','assets':[{'id':'A1','purpose':'Rain supports growth',
            'prompt':'A rain-fed field with thriving crops.', 'acceptance':['Crops visible'],'fact_ids':['F1']}]}
        film={'requested_seconds':60,'script':{'points':[{'id':'P1','narration':'Rain supports crop growth.', 'fact_ids':['F1']}]},
            'timing':{'points':[{'start':0,'end':5}]},
            'storyboard':{'scenes':[{'point_id':'P1','kind':'image','asset_id':'A1','heading':'Rain','rationale':'Crop context'}]}}
        class Planner:
            def loop(self,stage,task,context,validator,**kwargs):
                result={'style':'Natural illustration','assets':copy.deepcopy(context['used_assets']),
                        'banners':[{'scene_key':'60:P1','text':'Seasonal rains'}]}
                assert validator(result), 'Legacy shape must fail the new contract'
                result['assets'][0].update(visual_message='Rain sustains agricultural growth.',setting='A cultivated field during the rainy season.',
                                           visual_style='Natural photorealistic documentary imagery.')
                assert not validator(result), validator(result)
                return result
        result=make_image_prompts(Planner(),library,[film],{'title':'Rainfall','facts':[{'id':'F1'}]},['landscape'],message_recovery=True)
        self.assertEqual(result['scene_contexts_by_asset']['A1'][0]['visual_message'],result['assets'][0]['visual_message'])

    def test_invalid_json_review_gets_bounded_recovery(self):
        helper=ImageRecoveryTests();helper.setUp()
        models=Mock();models.call.side_effect=[ValueError('invalid JSON'),helper.verdict()]
        review=current_image_review(models,helper.case,Path('/tmp/image.png'),'Actual prompt','test')
        self.assertTrue(review['pass']); self.assertEqual(review['score'],88)       # every take gets a code total
        self.assertEqual(models.call.call_count,2)
