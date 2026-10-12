import copy
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from infofilm.core import ROOT, Store, configuration, digest, read, write
from infofilm.longform import ChapterLogRelay, chapter_context, chapter_creative, create_long, continuity_review, make_orientation, planner_settings, run_chapter, year_bounds, child_stage
from infofilm.longform_video import assemble
from infofilm.media import audio_info
from infofilm.queue import Queue
from infofilm.worker import run


def creative():
    return {'resolved_mood':'serious','requested_mood':'auto','intensity':'restrained','visual_balance':'balanced',
        'requested_visual_balance':'auto','rationale':'Evidence-grounded narrative','narration_direction':'Clear and measured',
        'visual_style':'Generated historical illustration','pacing_direction':'Natural delivery',
        'theme':{'background':'#10353d','accent':'#f3b95e','text':'#ffffff'},
        'music':{'caption':'Quiet instrumental motif','bpm':80,'keyscale':'C Minor','timesignature':'4'}}


def accepted_plan():
    return read(ROOT/'pilots/longform/mughal-chapter-plan.json'),read(ROOT/'pilots/longform/mughal-orientation.json')


def checked_stub(project, orientation='landscape', duration=120):
    video=project/'videos/original/video.mp4';video.parent.mkdir(parents=True,exist_ok=True);video.write_bytes(b'checked chapter')
    write(video.parent/'qa.json',{'passed':True,'video_sha256':digest(video.read_bytes())})
    write(video.parent/'layout/layout-results.json',{'passed':True})
    write(project/'delivery.json',{'generation_mode':'chapter','outputs':[{'video':str(video),'orientation':orientation}]})
    write(project/'sources.json',{'results':[{'url':'https://example.org/chapter','raw_content':'Evidence'}]})



def script_stub(payload, project):
    """Phase A stand-in: the chapter's accepted script."""
    write(Path(project) / f"film-{payload['durations'][0]}" / 'script.json', {'points': [{'id': 'P01', 'narration': 'Stub.'}]})
    return Path(project) / 'attempts/script'


def pair_ok(models, plan, rows):
    return [{'left': rows[0]['chapter']['id'], 'right': rows[-1]['chapter']['id'], 'accepted': True, 'score': 90, 'issues': []}]


class LongformTests(unittest.TestCase):
    def setUp(self):
        # Long films calibrate the voice once at film level before phase A; no voice server in unit tests.
        calibration = patch('infofilm.pipeline.voice_calibration', return_value=150); calibration.start(); self.addCleanup(calibration.stop)

    def test_animation_beat_progress_survives_parent_stage_and_heartbeat_reporting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root)
            try:
                store.event('pipeline:animation','running',{'beat_id':'turn'})
                beat={'orientation':'landscape','beat_id':'turn','position':2,'total':4,'phase':'animation','selected_objects':5}
                write(root/'animation/state.json',{'execution_mode':'beats','beat_progress':beat,'private':'not progress'})
                current=child_stage(root)
                self.assertEqual(current,{'stage':'animation','state':'running','execution_mode':'beats','beat_progress':beat})
                parent=Mock()
                with patch('infofilm.longform.time.monotonic',return_value=0):
                    relay=ChapterLogRelay(root,root,parent,'C02',2,4);relay.poll()
                with patch('infofilm.longform.time.monotonic',return_value=61):relay.poll()
                self.assertEqual(parent.event.call_args.args[2]['beat_progress'],beat)
                self.assertTrue(parent.event.call_args.args[2]['heartbeat'])
            finally:store.db.close()

    def test_model_assignment_is_configuration_not_a_historical_title_heuristic(self):
        settings=read(ROOT/'external-planner.json')
        self.assertEqual(planner_settings({'longform_settings':settings})['planner_role'],settings['planner_role'])
        self.assertEqual(planner_settings({'longform_settings':settings,'longform_planner_role':'reviewer'})['planner_role'],'reviewer')
        with self.assertRaisesRegex(ValueError,'planner_role'):
            planner_settings({'longform_settings':settings,'longform_planner_role':'unknown'})

    def test_chapter_log_relay_preserves_partial_unicode_lines_and_resume_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);log=root/'worker.log';row={};save=Mock();store=Mock()
            relay=ChapterLogRelay(root,root,store,'C01',1,4,row,save)
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                with log.open('wb') as child:
                    child.write('[audio] running\n'.encode());child.flush()
                    relay.drain()
                    text='[image] accepted: हिन्दी\n'.encode()
                    child.write(text[:-3]);child.flush()
                    relay.drain()
                    self.assertNotIn('[image]',output.getvalue())
                    child.write(text[-3:]);child.flush()
                    relay.drain()
                    child.write(b'final diagnostic');child.flush()
                relay.drain(final=True)
                resumed=ChapterLogRelay(root,root,store,'C01',1,4,row,save)
                resumed.drain(final=True)
            self.assertIn('[chapter C01 1/4',output.getvalue())
            self.assertEqual(output.getvalue().count('[audio] running'),1)
            self.assertEqual(output.getvalue().count('हिन्दी'),1)
            self.assertEqual(output.getvalue().count('final diagnostic'),1)
            self.assertEqual(row['log_relay_offset'],log.stat().st_size)
            self.assertNotIn('\ufffd',output.getvalue())
            save.assert_called()

    def test_chapter_poll_only_reports_stage_changes_and_quiet_heartbeats(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Mock()
            with patch('infofilm.longform.time.monotonic',return_value=0),\
                 patch('infofilm.longform.child_stage',return_value={'stage':'images','state':'running'}) as stage:
                relay=ChapterLogRelay(root,root,store,'C01',1,4)
                relay.poll();relay.poll()
                self.assertEqual(store.event.call_count,1)
                with patch('infofilm.longform.time.monotonic',return_value=61):relay.poll()
                self.assertEqual(store.event.call_count,2)
                self.assertTrue(store.event.call_args.args[2]['heartbeat'])
                self.assertEqual(store.event.call_args.args[2]['quiet_seconds'],61)
                stage.return_value={'stage':'video','state':'running'}
                with patch('infofilm.longform.time.monotonic',return_value=62):relay.poll()
                self.assertEqual(store.event.call_count,3)
                self.assertEqual(store.event.call_args.args[2]['stage'],'video')

    def test_finished_chapter_drains_failure_output_before_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def spawn(command,**kwargs):
                kwargs['stdout'].write(b'[A013] rejected: wrong scene\nTraceback: provider failed')
                write(Path(command[-1]),{'status':'failed','error':'Provider exhausted'})
                return Mock(returncode=1,poll=Mock(return_value=1))
            output=io.StringIO()
            with patch('infofilm.longform.subprocess.Popen',side_effect=spawn),contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError,'Provider exhausted'):
                    run_chapter({'project':str(root)},root,Mock(),'C01',1,4)
            self.assertIn('[A013] rejected',output.getvalue())
            self.assertIn('Traceback: provider failed',output.getvalue())

    def test_reattached_chapter_forwards_saved_and_live_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);attempt=root/'attempt';attempt.mkdir()
            (attempt/'worker.log').write_text('[P01] accepted\n')
            write(attempt/'result.json',{'status':'succeeded'})
            row={'attempt_directory':str(attempt),'child_pid':123};output=io.StringIO()
            with patch('infofilm.longform.chapter_alive',return_value=True),contextlib.redirect_stdout(output):
                returned=run_chapter({'project':str(root)},root,Mock(),'C02',2,4,row)
            self.assertEqual(returned,attempt)
            self.assertIn('[chapter C02 2/4',output.getvalue())
            self.assertIn('[P01] accepted',output.getvalue())
            self.assertEqual(row['log_relay_offset'],(attempt/'worker.log').stat().st_size)

    def test_orientation_keeps_spanning_processes_out_of_dated_event_ownership(self):
        models=Mock()
        sources={'results':[{'url':'https://example.org/history','raw_content':'The history of a regional state developed over a long period. ' * 20}]}
        def loop(label,task,context,validate,**kwargs):
            candidate={'anchors':[{'id':f'F{i:02d}','claim':'A sourced claim','source_ids':['S1']} for i in range(1,9)],
                'required_anchor_ids':['F01'],'coverage_requirements':['Major phases'],'cautions':[]}
            candidate['anchors'][0]['event_years']=[1722,1856]
            self.assertTrue(any('at most one' in error for error in validate(candidate)))
            candidate['anchors'][0].pop('event_years')
            self.assertFalse(validate(candidate))
            return candidate
        models.loop.side_effect=loop
        result=make_orientation(models,sources,'History',read(ROOT/'external-planner.json'))
        self.assertNotIn('event_years',result['anchors'][0])

    def test_dates_context_and_creative_preserve_a_single_film(self):
        self.assertEqual(year_bounds('Mughal India 1526 to 1857'),(1526,1857))
        self.assertEqual(year_bounds('How monsoons work'),(None,None))
        with self.assertRaises(ValueError):year_bounds('History',1600,None)
        plan,brief=accepted_plan();chapter=plan['chapters'][3]
        shared=creative();context=chapter_context(plan,chapter,shared,brief)
        self.assertEqual(context['chapter']['opening_role'],'continuation')
        self.assertNotIn('advisory_seconds',context['film'])
        self.assertNotIn('advisory_seconds',context['chapter'])
        direction=chapter_creative(shared,chapter)
        self.assertEqual(direction['theme'],shared['theme'])
        self.assertEqual(direction['music']['keyscale'],shared['music']['keyscale'])
        self.assertIn(chapter['music']['direction'],direction['music']['caption'])
        self.assertEqual(chapter_creative(shared,chapter,'warm')['resolved_mood'],'warm')
        self.assertEqual(shared,creative())

    def test_animation_asset_pause_continues_other_chapters_and_resumes_only_the_waiting_one(self):
        from infofilm.production_state import ProductionPaused
        plan,brief=accepted_plan()
        for chapter in plan['chapters']:
            chapter['production']={'kind':'illustrated','rationale':'A narrative illustrated chapter','animation_brief':None}
        plan['chapters'][1]['production']={'kind':'animation','rationale':'A changing relationship explains this point',
            'animation_brief':{'learning_objective':'Explain the changing relationship','demonstrations':['Show that relationship change'],
                               'assumptions':[],'success_criteria':['The relationship is visible']}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ref=root/'reference.wav';ref.write_bytes(b'reference')
            args=SimpleNamespace(config=write(root/'config.json',{'planner_url':'http://p','reviewer_url':'http://r','reference_audio':str(ref)}),
                project=root/'parent',subject=plan['film']['subject'],durations=[900],orientation='landscape',reference_audio=None,
                reference_transcript=None,language='en_indian',tts_engine='vibevoice',omni_speed=.8,mood='auto',mood_intensity='restrained',
                visual_balance='auto',music_engine='none',review_mode='experimental',animation_chapters=True,
                sources=None,orientation_brief=write(root/'brief.json',brief),chapter_plan=write(root/'plan.json',plan),until='video')
            calls=[]
            def child(payload,project,store,cid,*extra):
                if payload.get('until')=='script':return script_stub(payload,project)   # phase A (scripts) stub
                calls.append(cid)
                if cid=='C02':
                    self.assertEqual(payload['generation_mode'],'animation')
                    self.assertEqual(payload['queue_owner_project'],str(args.project))
                    self.assertTrue(read(payload['chapter_context'])['orientation_sources'])
                    raise ProductionPaused('waiting_for_assets','Draw the missing SVG')
                checked_stub(project);return project/'attempts/test'
            with patch('infofilm.creative.make_creative_brief',return_value=creative()),patch('infofilm.longform.run_chapter',side_effect=child),\
                 patch('infofilm.longform.continuity_review',side_effect=pair_ok),patch('infofilm.longform_video.assemble') as assembly:
                with self.assertRaises(ProductionPaused):create_long(args)
                self.assertEqual(calls,[c['id'] for c in plan['chapters']])
                state=read(args.project/'longform-status.json')
                self.assertEqual(state['completed'],6);self.assertEqual(state['failed'],0)
                self.assertEqual(state['chapters'][1]['status'],'waiting_for_assets')
                assembly.assert_not_called()
            calls.clear()
            def recovered(payload,project,store,cid,*extra):
                if payload.get('until')=='script':return script_stub(payload,project)   # phase A (scripts) stub
                calls.append(cid);checked_stub(project);return project/'attempts/recovered'
            with patch('infofilm.longform.run_chapter',side_effect=recovered),patch('infofilm.longform.continuity_review',side_effect=pair_ok),\
                 patch('infofilm.longform_video.assemble',return_value={'video':'checked.mp4'}),patch('infofilm.delivery.preview'):
                create_long(args)
            self.assertEqual(calls,['C02'])

    def test_all_scripts_and_the_continuity_gate_come_before_any_media(self):
        plan,brief=accepted_plan()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ref=root/'reference.wav';ref.write_bytes(b'reference')
            config=write(root/'config.json',{'planner_url':'http://p','reviewer_url':'http://r',
                'reference_audio':str(ref),'language':'en_indian','tts_url':'http://v','image_url':'http://i'})
            args=SimpleNamespace(config=config,project=root/'parent',subject=plan['film']['subject'],durations=[900],
                orientation='landscape',reference_audio=None,reference_transcript=None,language='en_indian',
                tts_engine='vibevoice',omni_speed=.8,mood='auto',mood_intensity='restrained',visual_balance='auto',music_engine='none',
                review_mode='experimental',sources=write(root/'sources.json',{'results':[{'url':'https://example.org','raw_content':'E'}]}),
                orientation_brief=write(root/'brief.json',brief),chapter_plan=write(root/'plan.json',plan),until='video')
            order=[]
            def child(payload,project,store,cid,*extra):
                order.append((payload['until'],cid))
                if payload['until']=='script':return script_stub(payload,project)
                checked_stub(project);return project/'attempts/test'
            def review(models,plan_,rows):
                order.append(('continuity',rows[1]['chapter']['id']));return pair_ok(models,plan_,rows)
            with patch('infofilm.creative.make_creative_brief',return_value=creative()),patch('infofilm.longform.run_chapter',side_effect=child),\
                 patch('infofilm.longform.continuity_review',side_effect=review),\
                 patch('infofilm.longform_video.assemble',return_value={'video':'checked.mp4'}),patch('infofilm.delivery.preview',return_value=root/'out'):
                create_long(args)
            kinds=[k for k,_ in order]
            last_script=max(i for i,k in enumerate(kinds) if k=='script')
            first_media=kinds.index('video')
            self.assertLess(last_script,first_media)
            self.assertEqual(kinds[last_script+1:first_media],['continuity']*(len(plan['chapters'])-1))
            self.assertEqual(read(args.project/'continuity-report.json')['stage'],'scripts')

    def test_script_gate_redrafts_only_the_later_chapter_once(self):
        from infofilm.longform import script_gate
        plan,_=accepted_plan()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rows=[]
            for chapter in plan['chapters'][:3]:
                project=root/chapter['id'];write(project/'film-120/script.json',{'points':[]})
                rows.append({'chapter':chapter,'project':str(project),'duration':120,'status':'script_ready'})
            bad={'accepted':False,'score':45,'issues':[{'severity':'major','field':'C02.P01','problem':'Retells the coup failure','correction':'Start after it'}]}
            reviews=[{**bad,'left':'C01','right':'C02'},{'left':'C01','right':'C02','accepted':True,'score':90,'issues':[]},
                     {'left':'C02','right':'C03','accepted':True,'score':88,'issues':[]}]
            models=Mock();models.store.root=root;notes={};redrafts=[]
            with patch('infofilm.longform.continuity_review',side_effect=lambda m,p,r:[reviews.pop(0)]):
                report=script_gate(models,plan,rows,notes,root/'notes.json',lambda position,row:redrafts.append(position),Mock())
            self.assertEqual(redrafts,[2])
            self.assertEqual(read(root/'notes.json')['C02'][0]['problem'],'Retells the coup failure')
            self.assertTrue(report[0]['repaired']);self.assertTrue(report[1]['accepted'])

    def test_cloud_llms_work_on_every_chapter_at_once_local_on_one(self):
        from infofilm.longform import chapter_workers
        local={'planner_url':'http://p','reviewer_url':'http://r'}
        self.assertEqual(chapter_workers(local,6),1)
        cloud={'planner_url':'https://a#h','reviewer_url':'https://b#l','llm_endpoints':{
            'https://a#h':{'type':'openrouter','url':'https://a'},'https://b#l':{'type':'openai_responses','url':'https://b'}}}
        self.assertEqual(chapter_workers(cloud,6),6)
        self.assertEqual(chapter_workers({**cloud,'chapter_workers':3},6),3)

    def test_failed_chapter_continues_and_resume_skips_successful_chapters(self):
        plan,brief=accepted_plan()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ref=root/'reference.wav';ref.write_bytes(b'reference')
            config=write(root/'config.json',{'planner_url':'http://p','reviewer_url':'http://r',
                'reference_audio':str(ref),'language':'en_indian','tts_url':'http://v','image_url':'http://i'})
            sources=write(root/'sources.json',{'results':[{'url':'https://example.org','raw_content':'Evidence'}]})
            outline=write(root/'plan.json',plan);orientation=write(root/'brief.json',brief)
            args=SimpleNamespace(config=config,project=root/'parent',subject=plan['film']['subject'],durations=[900],
                orientation='landscape',reference_audio=None,reference_transcript=None,language='en_indian',
                tts_engine='vibevoice',omni_speed=.8,mood='auto',mood_intensity='restrained',visual_balance='auto',music_engine='none',
                review_mode='experimental',sources=sources,orientation_brief=orientation,chapter_plan=outline,until='video',
                image_review=True)
            calls=[]
            def child(payload,project,store,cid,position,total,*extra):
                if payload.get('until')=='script':return script_stub(payload,project)   # phase A (scripts) stub
                calls.append(cid)
                self.assertTrue(payload['image_review'])
                if cid=='C02':raise RuntimeError('Image provider exhausted its budget')
                self.assertEqual(payload['generation_mode'],'short')
                self.assertEqual(read(payload['chapter_context'])['chapter']['id'],cid)
                checked_stub(project);return project/'attempts/test'
            with patch('infofilm.creative.make_creative_brief',return_value=creative()),patch('infofilm.longform.run_chapter',side_effect=child),\
                 patch('infofilm.longform.continuity_review',side_effect=pair_ok) as continuity,patch('infofilm.longform_video.assemble') as join:
                with self.assertRaisesRegex(RuntimeError,'C02'):
                    create_long(args)
                self.assertEqual(calls,[c['id'] for c in plan['chapters']])
                states=read(args.project/'longform-status.json')
                self.assertEqual(states['completed'],6);self.assertEqual(states['failed'],1)
                self.assertFalse((args.project/'delivery.json').exists())
                self.assertEqual(continuity.call_count,6);join.assert_not_called()   # scripts gate, before media
            calls.clear()
            # A visual-tail control must not invalidate the saved parent outline.
            args.image_review = False
            def recovered(payload,project,store,cid,*extra):
                if payload.get('until')=='script':return script_stub(payload,project)   # phase A (scripts) stub
                self.assertFalse(payload['image_review'])
                calls.append(cid);checked_stub(project);return project/'attempts/recovered'
            with patch('infofilm.longform.run_chapter',side_effect=recovered),patch('infofilm.longform.continuity_review',side_effect=pair_ok) as continuity,\
                 patch('infofilm.longform_video.assemble',return_value={'video':'checked.mp4'}) as join,\
                 patch('infofilm.delivery.preview',return_value=root/'output'):
                create_long(args)
                self.assertEqual(calls,['C02'])
                self.assertEqual(continuity.call_count,2);join.assert_called_once()  # only pairs touching C02
            self.assertEqual(read(args.project/'longform-status.json')['completed'],7)
            self.assertEqual(read(args.project/'delivery.json')['generation_mode'],'long')
            self.assertFalse(read(args.project/'delivery.json')['image_review'])
            self.assertEqual(read(args.project/'sources.json')['results'][0]['id'],'C01_S1')

    def test_failed_parent_plan_resume_gets_fresh_calls_and_retains_the_old_attempt(self):
        plan,brief=accepted_plan()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ref=root/'ref.wav';ref.write_bytes(b'reference')
            args=SimpleNamespace(config=write(root/'config.json',{'reference_audio':str(ref)}),project=root/'parent',
                subject=plan['film']['subject'],durations=[900],orientation='landscape',reference_audio=None,
                sources=None,orientation_brief=write(root/'brief.json',brief),chapter_plan=None,until='plan')
            attempts=[]
            def planner(config,store,*other):
                attempts.append(store.root)
                write(store.root/'marker.json',{'attempt':len(attempts)})
                if len(attempts)==1:raise RuntimeError('Drafts exhausted')
                return plan,{}
            with patch('infofilm.longform.plan_film',side_effect=planner):
                with self.assertRaisesRegex(RuntimeError,'Drafts exhausted'):create_long(args)
                create_long(args)
                create_long(args)
            self.assertEqual(len(attempts),2)
            self.assertNotEqual(attempts[0],attempts[1])
            self.assertTrue((attempts[0]/'marker.json').exists())
            self.assertTrue((args.project/'chapter-plan.json').exists())

    def test_continuity_reports_premature_conclusions_without_blocking_assembly(self):
        plan,_=accepted_plan()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);rows=[]
            for chapter in plan['chapters'][:2]:
                p=root/chapter['id']
                write(p/'film-120/script.json',{'requested_seconds':120,'points':[{'narration':'In conclusion, goodbye.'}]})
                write(p/'evidence-final.json',{'facts':[]})
                rows.append({'chapter':chapter,'project':str(p),'duration':120})
            models=Mock();models.store.root=root
            models.call_json.return_value={'pass':True,'score':99,'issues':[{'severity':'major','problem':'Premature conclusion'}]}
            report=continuity_review(models,plan,rows)
            self.assertFalse(report[0]['accepted'])
            self.assertEqual(read(root/'continuity-report.json')['pairs'][0]['issues'][0]['problem'],'Premature conclusion')
            models.call_json.return_value={'pass':True,'score':88,'issues':[{'severity':'minor','problem':'Optional wording improvement'}]}
            self.assertTrue(continuity_review(models,plan,rows)[0]['accepted'])
            self.assertEqual(models.call_json.call_args.kwargs['max_tokens'],6000)
            prompt=models.call_json.call_args.args[2]
            self.assertNotIn('"advisory_seconds"',prompt)
            self.assertNotIn('"requested_seconds"',prompt)

    def test_worker_always_records_failed_outcome_including_system_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);request=write(root/'request.json',{'project':str(root/'project')})
            with patch('infofilm.worker.create',side_effect=SystemExit(1)):
                self.assertEqual(run(request,root/'result.json'),1)
            self.assertEqual(read(root/'result.json')['status'],'failed')
            request.write_text('{broken')
            self.assertEqual(run(request,root/'invalid.json'),1)
            self.assertEqual(read(root/'invalid.json')['status'],'failed')

    def test_queue_failed_or_corrupt_outcome_immediately_launches_the_next_run(self):
        for outcome in [{'status':'failed','error':'provider failure'},'{broken']:
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);ref=root/'reference.wav';ref.write_bytes(b'ref')
                config=write(root/'config.json',{'reference_audio':str(ref)})
                with patch('infofilm.queue.ROOT',root):
                    factory=Mock();factory.return_value.pid=999999
                    queue=Queue(root/'interface',config,process_factory=factory)
                    payload={'subject':'First run','durations':[60],'orientation':'landscape','review_mode':'off','reference_audio':str(ref)}
                    first=queue.submit(payload);second=queue.submit({**payload,'subject':'Next run'})
                    queue._update(first['id'],status='running',run_id='failed-attempt',pid=999998)
                    path=queue._result_path(queue.get(first['id']))
                    if isinstance(outcome,dict):write(path,outcome)
                    else:path.parent.mkdir(parents=True);path.write_text(outcome)
                    queue._tick({})
                    self.assertEqual(queue.get(first['id'])['status'],'failed')
                    self.assertEqual(queue.get(second['id'])['status'],'running')
                    self.assertEqual(factory.call_count,1)

    def test_queue_launch_failure_also_advances_within_the_same_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);ref=root/'ref.wav';ref.write_bytes(b'ref')
            config=write(root/'config.json',{'reference_audio':str(ref)})
            with patch('infofilm.queue.ROOT',root):
                factory=Mock(side_effect=[OSError('cannot spawn'),SimpleNamespace(pid=999999)])
                q=Queue(root/'interface',config,process_factory=factory)
                p={'subject':'First','durations':[60],'orientation':'landscape','review_mode':'off','reference_audio':str(ref)}
                first=q.submit(p);second=q.submit({**p,'subject':'Next'})
                q._tick({})
                self.assertEqual(q.get(first['id'])['status'],'failed')
                self.assertEqual(q.get(second['id'])['status'],'running')


class LongformAssemblyTests(unittest.TestCase):
    def test_real_join_preserves_card_gaps_offsets_and_checked_master(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);store=Store(root/'parent');records=[]
            plan,_=accepted_plan();plan['chapters']=plan['chapters'][:2]
            # Synthetic media exercises FFmpeg and delivery QA without speech/image generation.
            for index,chapter in enumerate(plan['chapters']):
                project=root/chapter['id'];video_root=project/'videos/checked';video_root.mkdir(parents=True)
                voice=video_root/'narration.wav';score=project/'score.wav';picture=video_root/'picture.mp4';video=video_root/'video.mp4'
                subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'anoisesrc=color=pink:seed={17+index}:d=4:r=48000,volume=0.15','-c:a','pcm_s16le',str(voice)],check=True)
                subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'sine=frequency={100+index*80}:duration=4:sample_rate=48000','-c:a','pcm_s16le',str(score)],check=True)
                subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=640x360:rate=30:duration=4','-c:v','libx264','-pix_fmt','yuv420p','-an',str(picture)],check=True)
                # Match the real Remotion clock on one chapter and native
                # x264's different clock on the other; neither may corrupt cards.
                subprocess.run(['ffmpeg','-v','error','-i',str(picture),'-i',str(voice),'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac',
                    '-video_track_timescale',str(90000 if index == 0 else 15360),str(video)],check=True)
                image=project/'approved.png';Image.new('RGB',(640,360),(30,80+index*50,60)).save(image)
                info=audio_info(voice);score_info=audio_info(score)
                script={'title':chapter['title'],'points':[{'id':'P01','heading':'An idea','narration':'One connected explanation.','fact_ids':['F01']}]}
                write(project/'film-120/audio-selection.json',{'audio':str(voice),'audio_info':info,'script':script})
                write(project/'film-120/script.json',script)
                write(project/'film-120/music-selection.json',{'engine':'acestep','score':{'audio':str(score),'audio_sha256':score_info['audio_sha256']},'mix':{'music_gain':.07}})
                write(project/'visual-library.json',{'assets':[{'id':'A01','purpose':chapter['title'],'prompt':'Generated historical scene'}]})
                write(project/'image_manifest.json',{'A01':{'file':str(image),'sha256':digest(image.read_bytes())}})
                write(video_root/'qa.json',{'passed':True,'video_sha256':digest(video.read_bytes())})
                write(video_root/'timeline.json',{'data':{'scenes':[{'id':'scene-01','kind':'image','from':0,'duration':120,'source_ids':['S1']}],'captions':[{'start':0,'end':3.9,'text':'One connected explanation.'}]}})
                write(video_root/'layout/layout-results.json',{'passed':True,'samples':[{'file':'poster.png'}]})
                shutil_image=video_root/'layout/poster.png';shutil_image.write_bytes(image.read_bytes())
                write(project/'delivery.json',{'outputs':[{'video':str(video),'orientation':'landscape'}]})
                write(project/'sources.json',{'results':[{'url':'https://example.org/chapter','raw_content':'Evidence'}]})
                records.append({'chapter':chapter,'project':str(project),'duration':120})
            config=configuration();config.update(assembly_width=640,assembly_height=360)
            try:
                output=assemble(config,store,plan,records,'landscape',900)
                video_root=Path(output['video']).parent
                self.assertTrue(read(video_root/'qa.json')['passed'])
                self.assertEqual(output['actual_seconds'],10)
                data=read(video_root/'timeline.json')['data']
                self.assertEqual(data['durationInFrames'],300)
                self.assertEqual([c['start'] for c in data['captions']],[0,6])
                self.assertEqual([s['source_ids'] for s in data['scenes'] if s['kind']!='chapter-card'],[['C01_S1'],['C02_S1']])
                report=read(video_root/'assembly-report.json')
                self.assertFalse(report['remotion_rerendered'])
                self.assertEqual(report['music']['delivery_levels']['gain_db'], 4)
                self.assertEqual(report['concat_video_timescale'],90000)
                from infofilm.video import video_stream_hash
                for row in records:
                    original = read(Path(row['project'])/'delivery.json')['outputs'][0]['video']
                    self.assertEqual(video_stream_hash(original),video_stream_hash(video_root/'concat-inputs'/f"{row['chapter']['id']}.mp4"))
                self.assertFalse(report['section_cards'][0]['new_image_generated'])
                self.assertEqual(len(report['music']['cue_crossfades']),2)
                self.assertLess(report['music']['cue_crossfades'][1]['start_seconds'],6)
                with wave.open(str(video_root/'narration.wav')) as wav:
                    wav.setpos(4*48000);gap=np.frombuffer(wav.readframes(2*48000),dtype='<i2')
                self.assertTrue(np.all(gap==0))
                with patch('infofilm.longform_video.ffmpeg',side_effect=AssertionError('Checked assembly should be reused')):
                    self.assertEqual(assemble(config,store,plan,records,'landscape',900),output)
            finally:store.db.close()
