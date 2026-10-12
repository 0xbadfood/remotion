"""Single-model phase boundaries, provenance and credential-free Responses requests."""
import copy
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

import httpx

from infofilm.animation_experiment import phase
from infofilm.animation_frontier import (canvas_errors, placement_errors, animation_errors, merge_animation,
    run, situation_requirement, situation_handoff, chapter_handoff, chapter_requirement, GENERAL_MOTION_RULES, main,
    beat_report_sections)
from infofilm.animation_objects import register_object
from infofilm.animation_openai import OpenAIAnimationModels, OutputBudgetError
from infofilm.core import Store, read, write, digest
from infofilm.llm import ModelOutputError
from test_animation_experiment import ASSETS, FakeModels, components, motion, narration, requirement


def canvas():
    req=requirement()
    return {'schema_version':'infofilm.animation-canvas.v1','requirement_id':req['id'],
        'visual_message':req['visual_message'],'learning_objective':'Equal vertical acceleration',
        'viewport':{'width':1280,'height':720,'fps':30},'visual_area':[0,0,1,.8],
        'caption_area':[0,.8,1,.2],'palette':{'background':'#071b29','foreground':'#ffffff','accent':'#83e5f5'},
        'layout_strategy':'Compare balls side by side','teaching_sequence':[{'id':'compare',
          'message':'Both balls land together','demonstration':'Release simultaneously'}],
        'zones':[{'id':'demo','role':'Demonstration','box':[.1,.1,.8,.6]}]}


def placement():
    draft=motion();objects=[]
    for obj in draft['objects']:
        item={k:v for k,v in obj.items() if k!='tracks'}
        item.update(x=obj['tracks'][0]['x'],y=obj['tracks'][0]['y'],rotation=0)
        objects.append(item)
    return {'schema_version':'infofilm.animation-placement.v1','scene_id':draft['scene_id'],
        'visual_message':draft['visual_message'],'explanation':'Explicit initial placement',
        'objects':objects,'relationships':[]}


def animation():
    draft=motion()
    return {**draft,'objects':[{'id':obj['id'],'tracks':obj['tracks']} for obj in draft['objects']]}


class FrontierTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'trial');self.addCleanup(self.store.db.close)

    def test_code_only_phase_never_calls_reviewer_and_reuses_accepted_result(self):
        model=FakeModels([canvas()])
        validator=lambda value:canvas_errors(value,requirement(),canvas()['viewport'])
        result=phase(model,self.store,'canvas','Plan canvas',{},validator,2000,reviewed=False)
        self.assertEqual(result,canvas())
        self.assertEqual(len(model.calls),1)
        self.assertEqual(model.review_calls,[])
        self.assertEqual(model.calls[0]['role'],'planner')
        self.assertEqual(phase(model,self.store,'canvas','Plan canvas',{},validator,2000,reviewed=False),result)
        self.assertEqual(len(model.calls),1)
        evaluation=read(next((self.store.root/'planning').glob('canvas/*/evaluation.json')))
        self.assertEqual(evaluation['semantic_review'],'not_run')

    def test_canvas_reserves_caption_area_and_rejects_invalid_zones(self):
        value=canvas();self.assertEqual(canvas_errors(value,requirement(),value['viewport']),[])
        value['zones'][0]['box']=[.1,.7,.8,.2]
        self.assertIn('zone leaves the upper 80% visual area',canvas_errors(value,requirement(),value['viewport']))

    def test_static_placement_and_motion_validate_with_preserved_geometry(self):
        self.assertEqual(placement_errors(placement(),components(),ASSETS,narration(),canvas()),[])
        self.assertEqual(animation_errors(animation(),placement(),canvas(),components(),ASSETS,narration()),[])
        merged=merge_animation(animation(),placement(),canvas())
        for obj,placed in zip(merged['objects'],placement()['objects']):
            self.assertEqual(obj['width'],placed['width'])
            self.assertEqual(obj['height'],placed['height'])
        self.assertEqual(merged['objects'][-1]['text'],placement()['objects'][-1]['text'])

    def test_static_placement_validates_without_audio_or_invented_word_timing(self):
        self.assertEqual(placement_errors(placement(),components(),ASSETS,None,canvas()),[])
        moved=placement();moved['objects'][0]['x']=2
        self.assertTrue(any('visual area' in err for err in placement_errors(moved,components(),ASSETS,None,canvas())))

    def test_animation_cannot_silently_replace_placement_or_initial_position(self):
        value=animation();value['objects'][0]['width']=.2
        self.assertTrue(any('placement is frozen' in err for err in animation_errors(value,placement(),canvas(),components(),ASSETS,narration())))
        value=animation();value['objects'][0]['tracks'][0]['x']=.3
        self.assertTrue(any('frozen placement coordinates' in err for err in animation_errors(value,placement(),canvas(),components(),ASSETS,narration())))

    def api(self,response):
        mock=patch('infofilm.animation_openai.httpx.Client');client=mock.start();self.addCleanup(mock.stop)
        client.return_value.__enter__.return_value.post.return_value=httpx.Response(200,json=response)
        return client.return_value.__enter__.return_value

    def model(self,identity='gpt-6-luna'):
        return OpenAIAnimationModels({'planner_url':'https://api.openai.com/v1',
            'planner_model':identity,'reasoning_effort':'high','max_output_tokens':24000},self.store)

    def test_responses_adapter_caches_by_model_and_excludes_secret_from_artifacts(self):
        raw={'status':'completed','model':'gpt-6-luna','output':[{'type':'message',
            'content':[{'type':'output_text','text':'{"plan":true}'}]}],'usage':{'input_tokens':10,'output_tokens':5}}
        client=self.api(raw)
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-secret-not-for-artifacts'}):
            model=self.model();self.assertEqual(model.call('planner','canvas','JSON plan'),{'plan':True})
            model.call('planner','canvas','JSON plan')
            self.assertEqual(client.post.call_count,1)
            self.model('different-model').call('planner','canvas','JSON plan')
            self.assertEqual(client.post.call_count,2)
        body=client.post.call_args.kwargs['json']
        self.assertFalse(body['store'])
        self.assertEqual(body['text']['format']['type'],'json_object')
        self.assertNotIn('temperature',body)
        for path in (self.store.root/'requests').glob('*.json'):
            self.assertNotIn('test-secret-not-for-artifacts',path.read_text())
        with self.assertRaises(ValueError):self.model().call('reviewer','review','JSON review')

    def test_incomplete_response_is_saved_but_never_cached_as_a_valid_plan(self):
        client=self.api({'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[]})
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only'}),self.assertRaises(ModelOutputError):
            self.model().call('planner','motion','JSON animation')
        self.assertEqual([call.kwargs['json']['max_output_tokens'] for call in client.post.call_args_list],
                         [24000,48000,64000])
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only'}),self.assertRaises(ModelOutputError):
            self.model().call('planner','motion','JSON animation')
        self.assertEqual(client.post.call_count,3)
        self.assertEqual(len(list((self.store.root/'requests').glob('*.response.json'))),3)
        self.assertEqual(list((self.store.root/'requests').glob('*.result.json')),[])

    def test_budget_recovery_accepts_only_complete_json_and_reuses_it(self):
        client=self.api({})
        client.post.side_effect=[httpx.Response(200,json={'status':'incomplete',
            'incomplete_details':{'reason':'max_output_tokens'},'output':[],
            'usage':{'output_tokens':24000,'output_tokens_details':{'reasoning_tokens':12000}}}),
            httpx.Response(200,json={'status':'completed','output':[{'type':'message',
                'content':[{'type':'output_text','text':'{"plan":true}'}]}]})]
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only'}):
            self.assertEqual(self.model().call('planner','motion','JSON animation'),{'plan':True})
            self.assertEqual(self.model().call('planner','motion','JSON animation'),{'plan':True})
        self.assertEqual([call.kwargs['json']['max_output_tokens'] for call in client.post.call_args_list],
                         [24000,48000])
        self.assertEqual(len(list((self.store.root/'requests').glob('*.result.json'))),1)

    def test_detailed_animation_starts_with_larger_budget(self):
        client=self.api({'status':'completed','output':[{'type':'message',
            'content':[{'type':'output_text','text':'{}'}]}]})
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only'}):
            self.model().call('planner','animation-animation-landscape-test-0','JSON motion')
        self.assertEqual(client.post.call_args.kwargs['json']['max_output_tokens'],48000)

    def test_explicit_budget_ceiling_stops_after_one_exhausted_request(self):
        client=self.api({'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[]})
        model=OpenAIAnimationModels(self.model().config,self.store,
            animation_output_tokens=24000,max_output_recovery_tokens=24000)
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only'}),self.assertRaises(ModelOutputError):
            model.call('planner','animation-animation-landscape-test-0','JSON motion')
        self.assertEqual(client.post.call_count,1)

    def test_non_budget_failures_do_not_raise_the_budget(self):
        failures=[{'status':'incomplete','incomplete_details':{'reason':'content_filter'},'output':[]},
            {'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'declined'}]}]},
            {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"plan":'}]}]}]
        client=self.api({})
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-only'}):
            for number,raw in enumerate(failures):
                client.post.return_value=httpx.Response(200,json=raw)
                with self.assertRaises(ModelOutputError):
                    self.model().call('planner',f'failure-{number}','JSON plan')
        self.assertEqual(client.post.call_count,3)

    def test_phase_does_not_spend_semantic_attempts_after_budget_ceiling(self):
        model=FakeModels()
        with patch.object(model,'call',side_effect=OutputBudgetError(64000,{})) as call:
            with self.assertRaises(RuntimeError):
                phase(model,self.store,'canvas','JSON plan',{},lambda v:[],2000,max_attempts=3)
        self.assertEqual(call.call_count,1)
        evaluation=read(next((self.store.root/'planning').glob('canvas/*/evaluation.json')))
        self.assertTrue(evaluation['attempts'][0]['output_budget_exhausted'])
        self.assertEqual(len(evaluation['attempts']),1)

    def test_review_format_retry_does_not_replay_an_exhausted_budget(self):
        from infofilm.llm import Models
        model=FakeModels()
        with patch.object(model,'call',side_effect=OutputBudgetError(64000,{})) as call:
            with self.assertRaises(OutputBudgetError):
                Models.call_json(model,'reviewer','review','JSON review',max_tokens=3500)
        self.assertEqual(call.call_count,1)

    def runner_fixture(self):
        root=Path(self.temp.name)
        audio=root/'narration.wav'
        with wave.open(str(audio),'wb') as handle:
            handle.setnchannels(1);handle.setsampwidth(2);handle.setframerate(16000)
            handle.writeframes(b'\0\0'*(8*16000))
        value=narration();audio_hash=digest(audio.read_bytes())
        timing=write(root/'timing.json',{'audio_sha256':audio_hash,'words':value['words']})
        bundle=write(root/'narration-input.json',{'schema_version':'infofilm.animation-narration.v1',**value,
            'audio':str(audio),'audio_sha256':audio_hash,'timing':str(timing),
            'timing_sha256':digest(timing.read_bytes()),'transcript_sha256':digest(value['transcript'])})
        library=root/'library';library.mkdir()
        write(library/'registry.json',{'schema_version':'infofilm.animation-library.v1','revision':'empty','objects':{}})
        args=argparse.Namespace(model='gpt-6-luna',reasoning_effort='high',max_output_tokens=24000,max_attempts=1,
            requirement=write(root/'requirement.json',requirement()),narration_input=bundle,output=root/'experiment',
            library=library,config=root/'unused-config.json',orientation='landscape',until='objects',bindings=None,resume=False)
        return args

    def test_missing_object_registration_resume_preserves_canvas_and_object_plan(self):
        args=self.runner_fixture();candidate=components();candidate['objects']=candidate['objects'][:1]
        candidate['objects'][0].update(asset_id=None,request_id='R01')
        candidate['component_requests']=[{'id':'R01','requested_asset_id':'geometry.new-ball.v1',
            'description':'Circular ball','required_capabilities':['circular'],'svg_requirements':{'pivot':[.5,.5]}}]
        initial=FakeModels([canvas(),candidate])
        def factory(config,store):initial.config=config;return initial
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=factory):
            self.assertEqual(run(args),3)
        before={name:(args.output/name).read_bytes() for name in ['canvas-landscape-plan.json','components-plan.json']}
        svg=Path(self.temp.name)/'ball.svg'
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><circle cx="10" cy="10" r="9"/></svg>')
        register_object(read(args.output/'object-requests.json'),'R01',svg,
            {'pivot':[.5,.5],'capabilities':['circular'],'provenance':'Test manual registration'},
            {'pass':True,'asset_sha256':digest(svg.read_bytes()),'assessed_capabilities':['circular']},library=args.library)
        resumed=FakeModels()
        def resumed_factory(config,store):resumed.config=config;return resumed
        args.resume=True
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=resumed_factory):
            self.assertEqual(run(args),0)
        self.assertEqual(resumed.calls,[]);self.assertEqual(resumed.review_calls,[])
        for name,data in before.items():self.assertEqual((args.output/name).read_bytes(),data)
        self.assertTrue(read(args.output/'object-resolution.json')['ready'])

    def test_full_staged_plan_compiles_without_any_review_calls(self):
        args=self.runner_fixture();args.until='animation'
        svg=Path(self.temp.name)/'ball.svg'
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><circle cx="10" cy="10" r="9"/></svg>')
        write(args.library/'registry.json',{'schema_version':'infofilm.animation-library.v1','revision':'ball',
            'objects':{'geometry.ball.v1':{'file':'ball.svg','sha256':digest(svg.read_bytes()),
                'pivot':[.5,.5],'capabilities':['circular'],'provenance':'Fixture'}}})
        (args.library/'ball.svg').write_bytes(svg.read_bytes())
        model=FakeModels([canvas(),components(),placement(),animation()])
        def factory(config,store):model.config=config;return model
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=factory):
            self.assertEqual(run(args),0)
        self.assertEqual(len(model.calls),4);self.assertEqual(model.review_calls,[])
        self.assertTrue(all(call['role']=='planner' for call in model.calls))
        self.assertTrue((args.output/'renders/landscape/compiled-plan.json').is_file())
        self.assertEqual(read(args.output/'state.json')['review_mode'],'none')

    def test_situation_stops_at_object_demands_without_loading_any_narration(self):
        args=self.runner_fixture();args.situation='Explain the working of a four-stroke engine'
        args.requirement=None;args.narration_input=None
        req=situation_requirement(args.situation)
        plan=canvas();plan.update(requirement_id=req['id'],visual_message=req['visual_message'],
            learning_objective='Understand intake, compression, power and exhaust')
        plan['teaching_sequence']=[{'id':'intake','message':'Intake','demonstration':'Piston descends'}]
        candidate=components();candidate.update(requirement_id=req['id'],scene_id=req['id'],visual_message=req['visual_message'])
        candidate['objects']=candidate['objects'][:1]
        candidate['objects'][0].update(asset_id=None,request_id='R01',role='Engine cylinder',
            conveys='Show the piston inside the cylinder',physics_role='static',required_capabilities=['cutaway'])
        candidate['component_requests']=[{'id':'R01','requested_asset_id':'mechanics.cylinder.v1',
            'description':'Engine cylinder cutaway','required_capabilities':['cutaway'],'svg_requirements':{'view':'side'}}]
        model=FakeModels([plan,candidate])
        def factory(config,store):model.config=config;return model
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=factory), \
             patch('infofilm.animation_frontier.load_narration') as audio:
            self.assertEqual(run(args),3)
            audio.assert_not_called()
        self.assertEqual(len(model.calls),2);self.assertEqual(model.review_calls,[])
        self.assertTrue(all('There is no narration yet' in row['prompt'] or '"narration": null' in row['prompt'] for row in model.calls))
        self.assertEqual(read(args.output/'inputs.json')['narration'],None)
        self.assertEqual(read(args.output/'requirement.json'),req)
        self.assertEqual(read(args.output/'state.json')['state'],'waiting_for_components')
        self.assertEqual(read(args.output/'object-requests.json')['requests'][0]['requested_asset_id'],'mechanics.cylinder.v1')
        self.assertIn('Pre-audio planning', (args.output/'report.html').read_text())
        self.assertFalse((args.output/'placement-landscape-plan.json').exists())

    def test_pre_audio_plan_can_attach_narration_without_replanning_early_phases(self):
        args=self.runner_fixture();bundle=args.narration_input;args.narration_input=None
        candidate=components();candidate['objects']=candidate['objects'][:1]
        candidate['objects'][0].update(kind='rect',asset_id=None,physics_role='static',required_capabilities=[])
        first=FakeModels([canvas(),candidate])
        def factory(config,store):first.config=config;return first
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=factory):
            self.assertEqual(run(args),0)
        before={name:(args.output/name).read_bytes() for name in ['canvas-landscape-plan.json','components-plan.json']}
        args.narration_input=bundle;args.resume=True;args.until='placement'
        placed=placement();placed['objects']=placed['objects'][:1]
        resumed=FakeModels([placed])
        def resumed_factory(config,store):resumed.config=config;return resumed
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=resumed_factory):
            self.assertEqual(run(args),0)
        self.assertEqual(len(resumed.calls),1)
        for name,data in before.items():self.assertEqual((args.output/name).read_bytes(),data)
        self.assertTrue((args.output/'attached-narration.json').is_file())
        self.assertIsNone(read(args.output/'inputs.json')['narration'])
        # Later resumptions can reuse the attached, hash-verified bundle.
        args.narration_input=None
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=resumed_factory):
            self.assertEqual(run(args),0)
        self.assertEqual(len(resumed.calls),1)

    def test_pre_audio_placement_is_reused_after_attaching_audio(self):
        args=self.runner_fixture();bundle=args.narration_input;args.narration_input=None;args.until='placement'
        candidate=components();candidate['objects']=candidate['objects'][:1]
        candidate['objects'][0].update(kind='rect',asset_id=None,physics_role='static',required_capabilities=[])
        placed=placement();placed['objects']=placed['objects'][:1]
        model=FakeModels([canvas(),candidate,placed])
        def factory(config,store):model.config=config;return model
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=factory):
            self.assertEqual(run(args),0)
        self.assertEqual(len(model.calls),3)
        self.assertIsNone(read(args.output/'inputs.json')['narration'])
        self.assertFalse((args.output/'attached-narration.json').exists())
        self.assertEqual(read(args.output/'state.json')['state'],'placement_ready')
        args.resume=True;args.narration_input=bundle
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=factory):
            self.assertEqual(run(args),0)
        self.assertEqual(len(model.calls),3)

    def test_missing_narration_blocks_later_phases_before_model_calls(self):
        args=self.runner_fixture();args.narration_input=None;args.until='animation'
        with patch('infofilm.animation_frontier.OpenAIAnimationModels') as model:
            with self.assertRaisesRegex(ValueError,'--narration-input'):run(args)
            model.assert_not_called()

    def test_situation_cli_defaults_to_ordered_full_workflow_and_an_isolated_output(self):
        with patch('sys.argv',['experiment','--situation','Explain an engine']), \
             patch('infofilm.animation_frontier.run',return_value=0) as runner:
            self.assertEqual(main(),0)
        args=runner.call_args.args[0]
        self.assertIsNone(args.requirement);self.assertIsNone(args.narration_input)
        self.assertEqual(args.until,'render')
        self.assertEqual(args.workflow,'v2')
        self.assertEqual(args.execution_mode,'beats')
        self.assertIn('animation-v2-explain-an-engine-',args.output.name)

    def test_existing_ordered_workflow_can_switch_execution_without_rewriting_frozen_inputs(self):
        output=Path(self.temp.name)/'existing-v2';output.mkdir()
        frozen={'contract':'infofilm.animation-workflow.v2','orientations':['landscape'],
            'library':str(Path(self.temp.name)/'library'),
            'model':{'planner_model':'existing-planner','reviewer_model':'existing-reviewer',
                'reasoning_effort':'medium','max_output_tokens':16000,'continuity_model':'local'}}
        path=write(output/'inputs.json',frozen)
        write(output/'requirement.json',situation_requirement('A circular orbit'))
        before=path.read_bytes()
        for extra,expected in [([], 'beats'),(['--execution-mode','chapter'], 'chapter')]:
            with patch('sys.argv',['experiment','--output',str(output),'--resume',*extra]), \
                 patch('infofilm.animation_frontier.run',return_value=0) as runner:
                self.assertEqual(main(),0)
            args=runner.call_args.args[0]
            self.assertEqual(args.execution_mode,expected)
            self.assertEqual(args.model,'existing-planner')
            self.assertEqual(args.reviewer_model,'existing-reviewer')
            self.assertEqual(args.max_output_tokens,16000)
            self.assertEqual(path.read_bytes(),before)

    def test_beat_report_publishes_scoped_plans_and_progress_but_not_raw_requests(self):
        root=self.store.root;execution=root/'executions/beat-v1/source/landscape'
        manifest=write(execution/'manifest.json',{'fps':30,'beats':[{'beat_id':'turn','start_frame':0,'duration_frames':90}]})
        selected=write(execution/'02-turn/beat-turn-selection-plan.json',{'object_ids':['ball']})
        review=write(execution/'02-turn/planning/beat-turn-animation/source/evaluation.json',{'accepted':True})
        terminal=write(execution/'02-turn/terminal-state.json',{'objects':{}})
        write(execution/'requests/provider.request.json',{'private':'raw input'})
        outside=write(Path(self.temp.name)/'outside/secret-plan.json',{'private':'outside root'})
        write(root/'beat-execution.json',{'schema_version':'infofilm.animation-beat-execution.v1',
            'execution_roots':{'landscape':str(execution),'outside':str(outside.parent)},
            'manifest_paths':{'landscape':str(manifest),'outside':str(outside)}})
        write(root/'state.json',{'execution_mode':'beats','beat_progress':{
            'orientation':'landscape','beat_id':'turn','position':1,'total':3,'phase':'animation'}})
        files,sections=beat_report_sections(root)
        self.assertIn(str(selected.relative_to(root)),files)
        self.assertIn(str(review.relative_to(root)),files)
        self.assertIn(str(terminal.relative_to(root)),files)
        self.assertIn('beat 1/3',''.join(sections));self.assertIn('3.00s',''.join(sections))
        self.assertFalse(any('requests' in name or 'secret' in name for name in files))

    def test_existing_chapter_context_preserves_scope_continuity_and_grounding(self):
        handoff=situation_handoff('Four-stroke engines')
        chapter=handoff['chapter']
        chapter.update(objective='Explain the four strokes',exclude=['Engine history'],
            entry_context='The viewer has seen the basic engine parts',exit_handoff='Next: efficiency')
        handoff['orientation_anchors']=[{'id':'F01','claim':'Intake admits a fresh charge','source_ids':['S1']}]
        mapped=chapter_requirement(chapter_handoff(handoff))
        self.assertEqual(mapped['visual_message'],chapter['objective'])
        self.assertEqual(mapped['chapter_context'],handoff)
        self.assertEqual(mapped['facts'],['Intake admits a fresh charge'])
        self.assertNotIn('two balls in the comparison',GENERAL_MOTION_RULES)

    def test_parent_plan_selects_one_chapter_without_other_chapter_topics(self):
        first=situation_handoff('Engine operation')['chapter']
        second={**first,'id':'C02','title':'Efficiency','objective':'Explain energy losses'}
        parent={'schema_version':'infofilm.chapter-plan.v0','film':{'subject':'Engines'},
            'chapters':[first,second],
            'coverage':[{'id':'T01','owner_chapter_id':'C01'},{'id':'T02','owner_chapter_id':'C02'}],
            'research_gaps':[{'chapter_id':'C02','question':'Which losses dominate?'}]}
        with self.assertRaisesRegex(ValueError,'--chapter-id'):chapter_handoff(parent)
        selected=chapter_handoff(parent,'C02')
        self.assertEqual(selected['chapter'],second)
        self.assertEqual(selected['owned_topics'],[parent['coverage'][1]])
        self.assertEqual(selected['previous_chapter'],first['title'])
        self.assertIsNone(selected['next_chapter'])
        self.assertEqual(selected['research_gaps'],parent['research_gaps'])
        self.assertEqual(selected['orientation_anchors'],[])
        with self.assertRaisesRegex(ValueError,'does not match'):chapter_handoff(selected,'C01')

    def test_chapter_context_cli_uses_existing_envelope_and_stops_before_objects(self):
        args=self.runner_fixture();args.requirement=None;args.narration_input=None;args.until='canvas'
        context=situation_handoff('Engine operation')
        args.chapter_plan=write(Path(self.temp.name)/'chapter-context.json',context);args.chapter_id=None
        req=chapter_requirement(context)
        candidate=canvas();candidate.update(requirement_id=req['id'],visual_message=req['visual_message'])
        model=FakeModels([candidate])
        def factory(config,store):model.config=config;return model
        with patch('infofilm.animation_frontier.OpenAIAnimationModels',side_effect=factory):
            self.assertEqual(run(args),0)
        self.assertEqual(read(args.output/'chapter-context.json'),context)
        self.assertEqual(len(model.calls),1);self.assertEqual(model.review_calls,[])
        self.assertIn('"chapter_context"',model.calls[0]['prompt'])
        self.assertFalse((args.output/'components-plan.json').exists())

    def test_cli_honors_handoff_orientation_unless_explicitly_overridden(self):
        context=situation_handoff('An engine');context['film']['orientation']='portrait'
        path=write(Path(self.temp.name)/'chapter.json',context)
        for extra,expected in [([], 'portrait'),(['--orientation','landscape'], 'landscape')]:
            with patch('sys.argv',['experiment','--chapter-plan',str(path),*extra]), \
                 patch('infofilm.animation_frontier.run',return_value=0) as runner:
                main()
            self.assertEqual(runner.call_args.args[0].orientation,expected)
            self.assertIsNone(runner.call_args.args[0].narration_input)


if __name__=='__main__':unittest.main()
