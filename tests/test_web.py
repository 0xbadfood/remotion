import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from infofilm.auth import Auth
from infofilm.core import write
from infofilm.queue import Queue
from infofilm.web import COOKIE, PUBLIC_ORIGIN, create_app


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 16000)
    return output.getvalue()


class AuthTests(unittest.TestCase):
    def test_password_and_salt_are_not_stored_and_installations_have_different_hashes(self):
        with tempfile.TemporaryDirectory() as root:
            first, second = Auth(Path(root)/'first'), Auth(Path(root)/'second')
            for auth in [first, second]:
                auth.set_password('nitin', 'test-secret-42')
                value = auth.load()
                self.assertNotIn('test-secret-42', auth.path.read_text())
                self.assertNotIn('salt', value)
                self.assertEqual(auth.path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(auth.key_path.stat().st_mode & 0o777, 0o600)
                self.assertTrue(auth.verify('nitin', 'test-secret-42'))
                self.assertIsNone(auth.verify('another-user', 'test-secret-42'))
                self.assertIsNone(auth.verify('नितिन', 'test-secret-42'))
            self.assertNotEqual(first.load()['password_hash'], second.load()['password_hash'])
            old_revision = first.load()['revision']
            first.set_password('nitin', 'replacement-secret')
            self.assertNotEqual(first.load()['revision'], old_revision)
            self.assertIsNone(first.verify('nitin', 'test-secret-42'))


class WebTests(unittest.TestCase):
    def test_animation_chapters_wait_for_registered_assets_then_resume(self):
        import copy
        import shutil
        from test_animation_objects import plan, SVG
        from infofilm.animation_frontier import resolve_objects
        from infofilm.animation_objects import register_object
        from infofilm.core import ROOT, Store, read
        self.login()
        with patch.dict(os.environ,{'OPENAI_TOKEN':'test-only-dummy-key'}):
            response=self.submit(generation_mode='long',durations=[360],animation_chapters=True)
        self.assertEqual(response.status_code,202,response.text)
        job=response.json();queue=self.app.state.queue;original=queue.get(job['id'])
        self.assertTrue(job['settings']['animation_chapters'])
        self.assertIn('animation_provider',read(original['request']['config']))
        self.assertEqual(self.submit(animation_chapters=True).status_code,422)
        self.assertEqual(self.submit(generation_mode='long',durations=[360],animation_chapters=True,tts_engine='omnivoice').status_code,422)
        library=self.root/'animation-library';shutil.copytree(ROOT/'animation-library',library)
        project=Path(original['project']);child=project/'chapters/c01';animation=child/'animation'
        candidate=plan('geometry.web-demand.v1')
        store=Store(animation)
        try:
            write(animation/'components-plan.json',candidate)
            write(animation/'inputs.json',{'library':str(library)})
            _,_,resolution=resolve_objects(store,candidate,library,None)
        finally:store.db.close()
        write(animation/'report.html',{'fixture':'Asset wait'})
        # The media route only publishes an explicit allowlist.
        (animation/'report.html').write_text('<h1>Asset wait</h1>')
        write(animation/'public-report.json',{'schema_version':'infofilm.public-experiment-report.v1','files':['report.html','object-requests.json']})
        chapter={'id':'C01','title':'One demonstration','scope':{'start_year':None,'end_year':None,'description':'A mechanism'},
                 'mood':{'preset':'curious'},'production':{'kind':'animation'}}
        write(project/'longform-status.json',{'chapters':[{'chapter':chapter,'project':str(child),'duration':120,'status':'waiting_for_assets'}],
                                             'completed':0,'failed':0,'total':1})
        queue._update(job['id'],status='running',run_id='asset-wait',pid=999999)
        write(queue._result_path(queue.get(job['id'])),{'status':'waiting_for_assets','error':'Fill cache'})
        self.assertFalse(queue._observe(queue.get(job['id'])))
        self.assertEqual(queue.get(job['id'])['status'],'waiting_for_assets')
        detail=self.client.get('/api/jobs/'+job['id']).json()
        self.assertFalse(detail['can_resume_assets']);self.assertEqual(len(detail['asset_waits'][0]['unresolved']),1)
        self.assertEqual(self.client.get(detail['chapters'][0]['asset_requests_url']).status_code,200)
        self.assertEqual(self.client.get(detail['chapters'][0]['animation_report_url']).status_code,200)
        self.assertEqual(self.client.get(f"/media/{job['id']}/chapter/C01/animation/inputs.json").status_code,404)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/resume",json={}).status_code,409)
        svg=self.root/'new.svg';svg.write_bytes(SVG)
        metadata={'pivot':[.5,.5],'capabilities':['circular'],'provenance':'Original test asset'}
        import hashlib
        review={'pass':True,'asset_sha256':hashlib.sha256(SVG).hexdigest(),'assessed_capabilities':['circular']}
        register_object(resolution['faults'],resolution['faults'][0]['request_id'],svg,metadata,review,library=library)
        self.assertTrue(self.client.get('/api/jobs/'+job['id']).json()['can_resume_assets'])
        resumed=self.client.post(f"/api/jobs/{job['id']}/resume",json={})
        self.assertEqual(resumed.status_code,200,resumed.text)
        self.assertEqual(queue.get(job['id'])['status'],'queued')
        self.assertEqual(queue.get(job['id'])['request'],original['request'])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.reference = self.root/'reference.wav'
        self.reference.write_bytes(wav_bytes())
        self.kane = self.root/'kane.wav'
        self.kane.write_bytes(wav_bytes())
        model = self.root/'omni-model'
        model.mkdir()
        (model/'config.json').write_text('{}')
        self.config = self.root/'config.json'
        write(self.config, {'reference_audio':str(self.reference), 'planner_url':'http://planner/v1',
                           'reviewer_url':'http://reviewer/v1','tts_url':'http://tts','image_url':'http://images',
                           'reference_transcript':'Mumbai is the financial capital.',
                           'narrators': {'kane': {'label':'Kane', 'reference_audio':str(self.kane),
                                                 'reference_transcript':'Exact Kane words.'}},
                           'omni_python':sys.executable, 'omni_model':str(model), 'omni_url':'http://127.0.0.1:8094'})
        self.auth = Auth(self.root/'interface')
        self.auth.set_password('nitin', 'test-secret-42')
        self.root_patch = patch('infofilm.queue.ROOT', self.root)
        self.root_patch.start()
        self.app = create_app(self.config, self.root/'interface', start_worker=False)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.root_patch.stop()
        self.temporary.cleanup()

    def login(self, **kwargs):
        return self.client.post('/api/login', json={'username':'nitin','password':'test-secret-42'}, **kwargs)

    def submit(self, **overrides):
        value = {'subject':'Why volcanoes erupt','durations':[60],'orientation':'both','review_mode':'off',
                 'voice_id':'default','submission_id':'submission-test-01',
                 'tts_engine':'vibevoice'} | overrides          # the fixture has no LuxTTS runtime; luxtts is the default
        return self.client.post('/api/jobs', json=value)

    def test_llm_settings_page_saves_connections_roles_and_freezes_a_run(self):
        from infofilm.core import read
        self.assertEqual(self.client.get('/api/llm-settings').status_code, 401)
        self.login()
        data = self.client.get('/api/llm-settings').json()
        self.assertEqual(sorted(c['url'] for c in data['connections'].values()), ['http://planner/v1', 'http://reviewer/v1'])
        haiku = {'type': 'openrouter', 'label': 'Haiku', 'url': 'https://openrouter.ai/api/v1', 'model': 'anthropic/claude-haiku-5.5',
                 'vision': True, 'api_key': 'sk-or-web-secret'}
        luna = {'type': 'openai_responses', 'label': 'Luna', 'url': 'https://api.openai.com/v1', 'model': 'gpt-6-luna',
                'vision': True, 'api_key': 'sk-luna-web-secret'}
        for body in (haiku, luna):
            response = self.client.post('/api/llm-settings/connections', json=body)
            self.assertEqual(response.status_code, 200); self.assertNotIn('web-secret', response.text)
        bad = self.client.post('/api/llm-settings/roles', json={'planner': 'luna', 'reviewer': 'nope'})
        self.assertEqual(bad.status_code, 422)
        # Saving a configuration takes the roles selected on the page even if "Save roles" was not clicked first.
        saved = self.client.post('/api/llm-settings/configurations',
                                 json={'label': 'Haiku + Luna', 'roles': {'planner': 'haiku', 'reviewer': 'luna'}}).json()
        self.assertEqual(saved['configurations']['haiku-luna']['roles'], {'planner': 'haiku', 'reviewer': 'luna'})
        self.assertEqual(saved['assignments'], {'planner': 'haiku', 'reviewer': 'luna'})
        self.assertEqual(saved['active_configuration'], 'haiku-luna')
        job = self.submit().json()
        self.assertEqual(job['settings']['llm_configuration']['label'], 'Haiku + Luna')
        project = Path(self.app.state.queue.get(job['id'])['project'])
        frozen = read(project / 'config-snapshot.json')
        self.assertEqual(frozen['planner_url'], 'https://openrouter.ai/api/v1#haiku')
        self.assertNotIn('web-secret', (project / 'config-snapshot.json').read_text())
        unknown = self.submit(submission_id='submission-test-02', llm_configuration='missing')
        self.assertEqual(unknown.status_code, 409)

    def test_premium_rescue_is_an_opt_in_per_run(self):
        from infofilm.core import read
        config = read(self.config)
        config['image_terminal_recovery'] = {'enabled': True, 'provider': 'cloud_premium', 'max_candidates': 0, 'opt_in_candidates': 3}
        write(self.config, config)
        self.login()
        frozen = lambda job: read(Path(self.app.state.queue.get(job['id'])['project']) / 'config-snapshot.json')['image_terminal_recovery']
        default = self.submit(submission_id='premium-default').json()
        self.assertFalse(default['settings']['premium_rescue']); self.assertEqual(frozen(default)['max_candidates'], 0)
        opted = self.submit(premium_rescue=True, submission_id='premium-on')
        self.assertEqual(opted.status_code, 202, opted.text)
        self.assertTrue(opted.json()['settings']['premium_rescue']); self.assertEqual(frozen(opted.json())['max_candidates'], 3)
        self.assertEqual(self.submit(premium_rescue='no', submission_id='premium-invalid').status_code, 422)
        self.assertEqual(self.submit(premium_rescue=False, submission_id='premium-on').status_code, 409)

    def test_image_review_switch_and_resume_preserve_upstream_settings(self):
        from infofilm.core import read
        self.login()
        self.assertTrue(self.client.get('/api/settings').json()['default_image_review'])
        default = self.submit(submission_id='image-review-default').json()
        self.assertTrue(default['settings']['image_review'])
        result = self.submit(image_review=False, generation_mode='long', durations=[900],
                             submission_id='image-review-off')
        self.assertEqual(result.status_code, 202, result.text)
        job = result.json()
        self.assertFalse(job['settings']['image_review'])
        self.assertEqual(self.submit(image_review=False, generation_mode='long', durations=[900],
                                     submission_id='image-review-off').json()['id'], job['id'])
        self.assertEqual(self.submit(image_review=True, generation_mode='long', durations=[900],
                                     submission_id='image-review-off').status_code, 409)
        self.assertEqual(self.submit(image_review='off', submission_id='image-review-invalid').status_code, 422)
        queue = self.app.state.queue
        original = queue.get(job['id'])['request']
        snapshot = read(original['config'])
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/resume", json={'image_review': True}).status_code, 409)
        self.assertEqual(queue.get(job['id'])['request'], original)
        queue._update(job['id'], status='failed', error='Missing job')
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/resume", json={'image_review': 'off'}).status_code, 422)
        resumed = self.client.post(f"/api/jobs/{job['id']}/resume", json={'image_review': True})
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertTrue(resumed.json()['settings']['image_review'])
        updated = queue.get(job['id'])['request']
        self.assertEqual(updated, {**original, 'image_review': True})
        self.assertEqual(read(original['config']), snapshot)
        queue._update(job['id'], status='interrupted')
        # Old clients send no body; their existing mode is retained.
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/resume", json={}).status_code, 200)
        self.assertTrue(queue.get(job['id'])['request']['image_review'])

    def test_audio_first_media_and_video_continuation_preserve_saved_checkpoint(self):
        from infofilm.audio_delivery import finish_audio
        from infofilm.core import Store, read
        from test_audio_first import audio_fixture
        from test_longform import creative
        from types import SimpleNamespace
        self.login()
        response=self.submit(subject='An explanation',production_target='audio')
        self.assertEqual(response.status_code,202)
        job=response.json();queue=self.app.state.queue
        saved=queue.get(job['id']);project=Path(saved['project'])
        self.assertEqual(saved['request']['until'],'audio')
        self.assertEqual(job['settings']['production_target'],'audio')
        self.assertNotIn('images',[s['id'] for s in job['stages']])
        self.assertEqual(self.submit(production_target='bogus').status_code,422)
        self.assertEqual(self.submit(subject='An explanation',production_target='video').status_code,409)
        with patch('infofilm.audio_delivery.ROOT',self.root),patch('infofilm.web.ROOT',self.root):
            store=Store(project)
            film=audio_fixture(project,60)
            finish_audio(SimpleNamespace(subject='An explanation',music_engine='none'),{},store,[film],creative())
            store.db.close()
            queue._update(job['id'],status='succeeded')
            detail=self.client.get(f"/api/jobs/{job['id']}").json()
            self.assertTrue(detail['can_continue_video'])
            self.assertEqual(self.client.get(detail['review_url']).status_code,200)
            self.assertEqual(self.client.get(detail['audio_deliveries'][0]['mp3_url']).status_code,200)
            self.assertEqual(self.client.get(f"/media/{job['id']}/listen/%2e%2e%2fconfig.json").status_code,404)
            original=read(project/'audio-delivery.json')
            result=self.client.post(f"/api/jobs/{job['id']}/continue-video",json={})
            self.assertEqual(result.status_code,202)
            self.assertEqual(result.json()['status'],'queued')
            self.assertEqual(queue.get(job['id'])['request']['resume_from'],'scenes')
            self.assertEqual(read(project/'audio-delivery.json'),original)
            self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/continue-video",json={}).status_code,409)
            # The listening version remains available during image production.
            self.assertEqual(self.client.get(detail['audio_deliveries'][0]['mp3_url']).status_code,200)

    def test_authentication_protects_api_and_media_and_password_change_revokes_sessions(self):
        self.assertEqual(self.client.get('/api/jobs').status_code, 401)
        self.assertEqual(self.client.get('/media/saved/review/index.html').status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertEqual(self.client.get('/api/settings').status_code, 200)
        token = self.client.cookies.get(COOKIE)
        with self.app.state.queue.connection() as db:
            stored = db.execute('SELECT token_hash FROM sessions').fetchone()[0]
        self.assertNotEqual(stored, token)
        self.auth.set_password('nitin', 'replacement-secret')
        self.assertEqual(self.client.get('/api/settings').status_code, 401)
        self.assertFalse(self.client.get('/api/session').json()['authenticated'])

    def test_public_origin_is_whitelisted_and_other_origins_cannot_submit(self):
        options = self.client.options('/api/jobs', headers={'Origin':PUBLIC_ORIGIN,
             'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type'})
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options.headers['access-control-allow-origin'], PUBLIC_ORIGIN)
        self.assertEqual(options.headers['access-control-allow-credentials'], 'true')
        self.assertEqual(self.client.get('/api/settings', headers={'Origin':'https://untrusted.example'}).status_code, 403)
        response = self.login(headers={'Origin': PUBLIC_ORIGIN})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Secure', response.headers['set-cookie'])
        self.assertEqual(response.headers['access-control-allow-origin'], PUBLIC_ORIGIN)

    def test_queue_is_idempotent_isolates_subjects_and_survives_reopening(self):
        self.login()
        first = self.submit().json()
        duplicate = self.submit().json()
        self.assertEqual(first['id'], duplicate['id'])
        self.assertEqual(self.submit(subject='A different subject').status_code, 409)
        second = self.submit(subject='How a rainbow forms', submission_id='submission-test-02').json()
        reopened = Queue(self.root/'interface', self.config)
        self.assertEqual(reopened.get(first['id'])['status'], 'queued')
        self.assertNotEqual(reopened.get(first['id'])['project'], reopened.get(second['id'])['project'])
        self.assertEqual(self.submit(durations=[5]).status_code, 422)
        self.assertEqual(self.submit(orientation='square').status_code, 422)
        self.assertEqual(self.submit(tts_engine='unknown', submission_id='bad-engine-test').status_code, 422)

    def test_creative_controls_default_to_auto_and_are_frozen_and_validated(self):
        self.login()
        settings=self.client.get('/api/settings').json()
        self.assertEqual(settings['default_mood'],'auto')
        self.assertEqual(settings['default_omni_speed'],.8)
        first=self.submit().json()
        record=self.app.state.queue.get(first['id'])
        self.assertEqual(record['request']['mood'],'auto')
        self.assertEqual(record['request']['omni_speed'],.8)
        self.assertEqual(self.submit(mood='warm').status_code,409)
        changed=self.submit(mood='funny',visual_balance='image-led',omni_speed=.75,submission_id='new-creative-test')
        self.assertEqual(changed.status_code,202)
        frozen=self.app.state.queue.get(changed.json()['id'])['request']
        self.assertEqual(frozen['mood'],'funny')
        self.assertEqual(frozen['visual_balance'],'image-led')
        for field,value in [('mood','unknown'),('visual_balance','slides-only'),('omni_speed',0),('music_engine','unknown')]:
            self.assertEqual(self.submit(**{field:value,'submission_id':'invalid-creative-test'}).status_code,422)

    def test_long_generation_controls_limits_and_frozen_planning_settings(self):
        from infofilm.core import read
        self.login()
        response=self.submit(subject='The Mughal era in India 1526 to 1857',durations=[900],generation_mode='long',
                             submission_id='long-history-test')
        self.assertEqual(response.status_code,202,response.text)
        job=self.app.state.queue.get(response.json()['id'])
        self.assertEqual(job['request']['generation_mode'],'long')
        self.assertEqual((job['request']['start_year'],job['request']['end_year']),(1526,1857))
        self.assertEqual(read(job['request']['config'])['longform_settings']['planner_role'],read(Path(__file__).resolve().parents[1]/'external-planner.json')['planner_role'])
        self.assertIn('planning',[s['id'] for s in response.json()['stages']])
        self.assertIn('assembly',[s['id'] for s in response.json()['stages']])
        duplicate=self.submit(subject='The Mughal era in India 1526 to 1857',durations=[900],generation_mode='long',submission_id='long-history-test')
        self.assertEqual(duplicate.json()['id'],job['id'])
        for changes in [{'durations':[900]}, {'generation_mode':'long','durations':[60]},
                        {'generation_mode':'long','durations':[900,1200]},
                        {'generation_mode':'long','durations':[1801]}]:
            self.assertEqual(self.submit(**changes,submission_id='invalid-long-test').status_code,422)
        conceptual=self.submit(subject='How the monsoon works',durations=[900],generation_mode='long',submission_id='long-concept-test')
        self.assertEqual(conceptual.status_code,202,conceptual.text)
        request=self.app.state.queue.get(conceptual.json()['id'])['request']
        self.assertIsNone(request['start_year'])
        self.assertIsNone(request['end_year'])
        from infofilm.web import FilmRequest
        self.assertNotIn('start_year',FilmRequest.model_fields)
        self.assertNotIn('end_year',FilmRequest.model_fields)
        self.assertEqual(self.submit(subject='The Mughal era in India 1526 to 1857',durations=[1200],generation_mode='long',submission_id='long-history-test').status_code,409)

    def test_subject_graph_toggle_is_long_only_frozen_and_idempotent(self):
        from infofilm.core import read
        self.login()
        self.assertIs(self.client.get('/api/settings').json()['default_subject_graph'], False)
        off = self.submit(generation_mode='long', durations=[900], submission_id='graph-off-test').json()
        self.assertIs(off['settings']['subject_graph'], False)
        off_job = self.app.state.queue.get(off['id'])
        self.assertNotIn('subject_graph_snapshot', read(off_job['request']['config']))
        on = self.submit(generation_mode='long', durations=[900], subject_graph=True, submission_id='graph-on-test')
        self.assertEqual(on.status_code, 202)
        job = on.json()
        self.assertTrue(job['settings']['subject_graph'])
        self.assertEqual(job['stages'][0]['id'], 'classification')
        snapshot = read(self.app.state.queue.get(job['id'])['request']['config'])
        self.assertEqual(snapshot['subject_graph_snapshot']['schema_version'], 'infofilm.subject-taxonomy.v0')
        self.assertEqual(self.submit(generation_mode='long', durations=[900], subject_graph=True,
                                    submission_id='graph-on-test').json()['id'], job['id'])
        self.assertEqual(self.submit(generation_mode='long', durations=[900], subject_graph=False,
                                    submission_id='graph-on-test').status_code, 409)
        self.assertEqual(self.submit(subject_graph=True, submission_id='graph-short-test').status_code, 422)
        self.assertEqual(self.submit(subject_graph='on', submission_id='graph-string-test').status_code, 422)

    def test_failed_long_parent_keeps_ready_chapter_audio_and_private_paths_protected(self):
        from infofilm.core import read
        self.login()
        response=self.submit(generation_mode='long',durations=[900],submission_id='long-media-test')
        job=self.app.state.queue.get(response.json()['id']);project=Path(job['project'])
        child=project/'chapters/chapter-one';audio=child/'audio/narration.wav';audio.parent.mkdir(parents=True);audio.write_bytes(wav_bytes())
        write(child/'film-120/audio-selection.json',{'audio':str(audio)})
        (child/'film-120/script.txt').write_text('A chapter narration.')
        write(project/'chapter-plan.json',{'schema_version':'infofilm.chapter-plan.v0'})
        write(project/'longform-status.json',{'completed':1,'failed':1,'total':2,'chapters':[
            {'chapter':{'id':'C01','title':'First chapter','scope':{'start_year':None,'end_year':None},'mood':{'preset':'curious'}},
             'project':str(child),'duration':120,'status':'succeeded'}]})
        self.app.state.queue._update(job['id'],status='failed',error='Second chapter failed')
        detail=self.client.get(f"/api/jobs/{job['id']}").json()
        self.assertEqual(detail['chapter_progress']['completed'],1)
        self.assertFalse(detail['can_revise_music'])
        self.assertEqual(self.client.get(detail['chapter_plan_url']).status_code,200)
        self.assertEqual(self.client.get(detail['chapters'][0]['audio_url'],headers={'Range':'bytes=0-43'}).status_code,206)
        self.assertEqual(self.client.get(f"/media/{job['id']}/chapter/C99/audio.wav").status_code,404)
        self.assertEqual(self.client.get(f"/media/{job['id']}/chapter/C01/review/%2e%2e/%2e%2e/auth.json").status_code,404)
        write(child/'film-120/audio-selection.json',{'audio':str(self.reference)})
        self.assertEqual(self.client.get(detail['chapters'][0]['audio_url']).status_code,404)

    def test_omnivoice_switch_is_frozen_and_uploaded_voices_need_transcripts(self):
        self.login()
        selected = self.submit(tts_engine='omnivoice', submission_id='omni-default-test')
        self.assertEqual(selected.status_code, 202)
        job = self.app.state.queue.get(selected.json()['id'])
        self.assertEqual(job['request']['tts_engine'], 'omnivoice')
        self.assertIn('Mumbai is the financial', job['request']['reference_transcript'])
        self.assertEqual(self.submit(tts_engine='vibevoice', submission_id='omni-default-test').status_code, 409)
        uploaded = self.client.post('/api/voices', files={'file':('new-voice.wav',wav_bytes(),'audio/wav')}).json()
        self.assertEqual(self.submit(tts_engine='omnivoice', voice_id=uploaded['id'],
                                     submission_id='omni-no-transcript').status_code, 422)
        custom = self.submit(tts_engine='omnivoice', voice_id=uploaded['id'],
                             reference_transcript='An exact reference transcript.',
                             submission_id='omni-custom-test')
        self.assertEqual(custom.status_code, 202)
        self.assertEqual(self.app.state.queue.get(custom.json()['id'])['request']['reference_transcript'],
                         'An exact reference transcript.')

    def test_voice_upload_validates_real_wav_and_uses_content_addressed_reference(self):
        self.login()
        invalid = self.client.post('/api/voices', files={'file':('bad.wav',b'not audio','audio/wav')})
        self.assertEqual(invalid.status_code, 422)
        uploaded = self.client.post('/api/voices', files={'file':('new-voice.wav',wav_bytes(),'audio/wav')})
        self.assertEqual(uploaded.status_code, 200)
        voice = uploaded.json()
        self.assertEqual(len(voice['id']), 64)
        submitted = self.submit(voice_id=voice['id']).json()
        job = self.app.state.queue.get(submitted['id'])
        self.assertTrue(Path(job['request']['reference_audio']).is_file())
        self.assertEqual(self.submit(voice_id='../auth.json', submission_id='bad-voice-test').status_code, 422)

    def test_kane_selection_freezes_its_reference_and_transcript_for_both_engines(self):
        self.login()
        voices = {v['id']:v for v in self.client.get('/api/settings').json()['voices']}
        self.assertTrue(voices['kane']['has_transcript'])
        for engine in ['vibevoice', 'omnivoice']:
            response = self.submit(voice_id='kane', tts_engine=engine,
                                   reference_transcript='Wrong default words.',
                                   submission_id='kane-test-'+engine)
            self.assertEqual(response.status_code,202)
            saved = self.app.state.queue.get(response.json()['id'])['request']
            self.assertEqual(saved['voice_id'],'kane')
            self.assertEqual(saved['reference_audio'],str(self.kane))
            self.assertEqual(saved['reference_transcript'],'Exact Kane words.')
        self.assertEqual(self.submit(voice_id='default',submission_id='kane-test-vibevoice').status_code,409)

    def test_media_ranges_work_and_private_paths_are_not_served(self):
        self.login()
        job = self.submit().json()
        record = self.app.state.queue.get(job['id'])
        project = Path(record['project'])
        audio = project/'audio/test/narration.wav'
        audio.parent.mkdir(parents=True)
        audio.write_bytes(wav_bytes())
        write(project/'film-60/audio-selection.json', {'audio':str(audio)})
        response = self.client.get(f"/media/{job['id']}/audio/60.wav", headers={'Range':'bytes=0-43'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.content), 44)
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(self.client.get(f"/media/{job['id']}/audio/120.wav").status_code, 404)
        write(project/'film-60/audio-selection.json', {'audio':str(self.reference)})
        self.assertEqual(self.client.get(f"/media/{job['id']}/audio/60.wav").status_code, 404)
        self.assertEqual(self.client.get(f"/media/{job['id']}/review/%2e%2e/auth.json").status_code, 404)

    def test_experiment_reports_require_login_and_explicit_publication(self):
        with patch('infofilm.web.ROOT', self.root):
            base = self.root/'artifacts/experiments/anatomy-pilot'
            base.mkdir(parents=True)
            (base/'report.html').write_text('<h1>Anatomy</h1>')
            image = base/'results/qwen_local/D01/candidate-0.png'
            image.parent.mkdir(parents=True)
            image.write_bytes(b'image-bytes')
            (base/'private.json').write_text('{"private":true}')
            route = '/experiments/anatomy-pilot/'
            self.assertEqual(self.client.get(route+'report.html').status_code, 401)
            self.login()
            self.assertEqual(self.client.get(route+'report.html').status_code, 404)
            write(base/'public-report.json', {
                'schema_version':'infofilm.public-experiment-report.v1',
                'files':['report.html', 'results/qwen_local/D01/candidate-0.png', 'escape.png']})
            response = self.client.get(route+'report.html')
            self.assertEqual(response.status_code, 200)
            self.assertIn('text/html', response.headers['content-type'])
            self.assertEqual(response.headers['cache-control'], 'private, no-store')
            self.assertEqual(self.client.get(route+'results/qwen_local/D01/candidate-0.png').content, b'image-bytes')
            self.assertEqual(self.client.head(route+'report.html').status_code, 200)
            self.assertEqual(self.client.get(route+'private.json').status_code, 404)
            self.assertEqual(self.client.get(route+'%2e%2e/private.json').status_code, 404)
            (base/'escape.png').symlink_to(self.reference)
            self.assertEqual(self.client.get(route+'escape.png').status_code, 404)
            response = self.client.get(route+'report.html', headers={'Origin':'https://untrusted.example'})
            self.assertEqual(response.status_code, 403)

    def test_login_rate_limit_and_validation_do_not_echo_passwords(self):
        password = 'x' * 1025
        invalid = self.client.post('/api/login', json={'username':'nitin','password':password})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(password, invalid.text)
        for _ in range(5):
            self.assertEqual(self.client.post('/api/login',json={'username':'nitin','password':'wrong-secret'}).status_code,401)
        self.assertEqual(self.login().status_code,429)

    def test_restart_recovery_distinguishes_completed_and_stopped_workers(self):
        self.login()
        job = self.submit().json()
        queue = self.app.state.queue
        queue._update(job['id'], status='running', run_id='first-attempt', pid=999999)
        self.assertFalse(queue._observe(queue.get(job['id'])))
        self.assertEqual(queue.get(job['id'])['status'],'interrupted')
        queue.resume(job['id'])
        self.assertEqual(queue.get(job['id'])['status'],'queued')
        queue._update(job['id'], status='running', run_id='second-attempt', pid=999999)
        write(queue._result_path(queue.get(job['id'])), {'status':'succeeded','finished':time.time()})
        self.assertFalse(queue._observe(queue.get(job['id'])))
        self.assertEqual(queue.get(job['id'])['status'],'succeeded')
        with self.assertRaises(ValueError):
            queue.resume(job['id'])

    def test_narration_repair_is_queued_once_and_preserves_the_original_request(self):
        from test_resume_narration import narration_fixture
        from infofilm.core import read
        self.login()
        job = self.submit().json()
        queue = self.app.state.queue
        project = Path(queue.get(job['id'])['project'])
        initial = read(project/'web-request.json')
        narration_fixture(project)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/narration", json={}).status_code, 409)
        queue._update(job['id'], status='succeeded')
        response = self.client.post(f"/api/jobs/{job['id']}/narration", json={})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(queue.get(job['id'])['request']['resume_from'], 'narration')
        self.assertEqual(read(project/'web-request.json'), initial)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/narration", json={}).status_code, 409)
        self.assertEqual(next(s['state'] for s in response.json()['stages'] if s['id'] == 'narration'), 'pending')
        queue._update(job['id'], status='failed')
        queue.resume(job['id'])
        self.assertEqual(queue.get(job['id'])['request']['resume_from'], 'narration')

    def test_score_revision_freezes_per_attempt_request_preserves_initial_request_and_retries_only_music(self):
        from test_resume_music import checkpoint
        from infofilm.core import read
        self.login()
        job = self.submit().json()
        queue = self.app.state.queue
        record = queue.get(job['id'])
        project = Path(record['project'])
        initial = read(project / 'web-request.json')
        snapshot = read(project / 'config-snapshot.json')
        checkpoint(project)
        # The fixture's music runtime is sufficient for admission; generation is not run here.
        model = self.root / 'checkpoints/acestep-v15-turbo'
        model.mkdir(parents=True)
        write(model / 'config.json', {})
        write(project / 'config-snapshot.json', {**snapshot, 'acestep_python': sys.executable,
            'acestep_root': str(self.root), 'acestep_model': 'acestep-v15-turbo'})
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/music", json={}).status_code, 409)
        queue._update(job['id'], status='succeeded')
        response = self.client.post(f"/api/jobs/{job['id']}/music", json={})
        self.assertEqual(response.status_code, 202, response.text)
        queued = queue.get(job['id'])
        self.assertEqual(queued['request']['resume_from'], 'music')
        self.assertEqual(queued['request']['music_engine'], 'acestep')
        self.assertEqual(read(project / 'web-request.json'), initial)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/music", json={}).status_code, 409)
        with patch.object(queue, 'process_factory') as launch:
            launch.return_value.pid = 999999
            queue._launch(queued)
        command = launch.call_args.args[0]
        attempt_request = Path(command[command.index('--request') + 1])
        self.assertEqual(read(attempt_request), queued['request'])
        self.assertNotEqual(attempt_request, project / 'web-request.json')
        queue._update(job['id'], status='failed', error='Score needs review')
        queue.resume(job['id'])
        self.assertEqual(queue.get(job['id'])['request']['resume_from'], 'music')


if __name__ == '__main__':
    unittest.main()
