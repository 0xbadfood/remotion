"""Bounded beat execution, cache pauses and immutable chapter inputs."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infofilm.animation_objects import register_object
from infofilm.animation_workflow import beat_word_spans, motion_errors, placement_plan_errors
from infofilm.core import Store, digest, read, write
from test_animation_experiment import FakeModels, anchor, good_review, narration, requirement
from test_animation_frontier import canvas


def teaching():
    req=requirement()
    return {'schema_version':'infofilm.animation-teaching.v2','requirement_id':req['id'],
        'visual_message':req['visual_message'],'explanation_strategy':'Two distinct demonstrations','assumptions':[],
        'teaching_sequence':[{'id':identity,'message':message,'demonstration':message,
            'demonstration_mode':'guided','required_behaviors':[]}
            for identity,message in [('release','Explain the release'),('landing','Explain the landing')]]}


def script():
    req=requirement();words=narration()['words']
    return {'schema_version':'infofilm.animation-narration-script.v1','requirement_id':req['id'],
        'visual_message':req['visual_message'],'title':'Two demonstrations',
        'points':[{'id':'N01','beat_ids':['release'],'narration':' '.join(w['word'] for w in words[:4])},
                  {'id':'N02','beat_ids':['landing'],'narration':' '.join(w['word'] for w in words[4:])}]}


def timing():
    return {'schema_version':'infofilm.animation-timing-cues.v2','beats':[
        {'beat_id':'release','start_anchor':anchor('W1'),'end_anchor':anchor('W4','end'),
            'repeat_anchor':None,'repeat_reason':None},
        {'beat_id':'landing','start_anchor':anchor('W5'),'end_anchor':anchor('W7','end'),
            'repeat_anchor':None,'repeat_reason':None}]}


def components(svg=False):
    req=requirement()
    result={'schema_version':'infofilm.animation-components.v1','requirement_id':req['id'],
        'scene_id':req['id'],'visual_message':req['visual_message'],'teaching_strategy':'Two views','assumptions':[],
        'objects':[{'id':'dot','kind':'rect','role':'Simple demonstration marker','conveys':'Position',
            'physics_role':'static','asset_id':None,'request_id':None,'required_capabilities':[]}],
        'component_requests':[]}
    if svg:
        result['objects'].append({'id':'gauge','kind':'svg','role':'Second demonstration','conveys':'Landing',
            'physics_role':'static','asset_id':None,'request_id':'R1','required_capabilities':['diagram']})
        result['component_requests']=[{'id':'R1','requested_asset_id':'science.beat-gauge.v1',
            'description':'A square diagram marker','required_capabilities':['diagram'],
            'svg_requirements':{'pivot':[.5,.5]}}]
    return result


def selection(beat_id,identity='dot',continuous=False):
    return {'schema_version':'infofilm.animation-beat-selection.v1','beat_id':beat_id,
        'object_ids':[identity],'object_roles':[{'id':identity,'reason':'Explains this beat'}],
        'transition':{'kind':'continuous' if continuous else 'explained_reset',
            'retained_object_ids':[identity] if continuous else [],'cycle_carries':[],
            'reason':'Retain the existing demonstration' if continuous else 'The narration introduces a new view'},
        'component_requests':[]}


def placement(beat_id,identity='dot',same_pose=False):
    req=requirement();first=beat_id=='release' or same_pose
    obj={'id':identity,'x':.3 if first else .65,'y':.3 if first else .35,
        'width':.1 if first else .15,'height':.1 if first else .08,
        'rotation':0,'color':'#ffffff' if first else '#83e5f5','z_index':1}
    if identity=='gauge':obj.update(width=.1,height=.1*1280/720,color='#ffffff')
    return {'schema_version':'infofilm.animation-placement.v1','scene_id':req['id'],
        'visual_message':req['visual_message'],'explanation':'A clear beat-local view',
        'objects':[obj],'relationships':[]}


def animation(beat_id,identity='dot',same_pose=False,gap=False):
    req=requirement();p=placement(beat_id,identity,same_pose)['objects'][0]
    words=['W1','W2','W4'] if beat_id=='release' else ['W5','W6','W7']
    result={'schema_version':'infofilm.animation-motion-draft.v1','scene_id':req['id'],
        'visual_message':req['visual_message'],'explanation':'Show the scoped narrated explanation',
        'objects':[{'id':identity,'tracks':[{'kind':'hold','x':p['x'],'y':p['y'],
            'start_anchor':anchor('audio_start'),'end_anchor':anchor('audio_end','end')}]}],
        'cycles':[],'animation_phases':[{'id':'guided','kind':'guided','beat_ids':[beat_id],
            'start_anchor':anchor('audio_start'),'end_anchor':anchor('audio_end','end'),
            'cycle_id':None,'transition':'continuous','purpose':'Explain this beat'}],
        'motion_requirements':[],'capability_gaps':[],
        'checkpoints':[{'id':str(i),'anchor':anchor(word,'end' if i==2 else 'start'),
            'reason':'Inspect the demonstration','object_ids':[identity]} for i,word in enumerate(words)]}
    if gap:result['capability_gaps']=[{'behavior':'fluid_solver','object_ids':[identity],
        'importance':'essential','reason':'No solver exists','lost_explanation':'Detailed fluid motion'}]
    return result


class BeatWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'chapter/animation');self.addCleanup(self.store.db.close)
        self.library=self.root/'library'
        write(self.library/'registry.json',{'schema_version':'infofilm.animation-library.v1','revision':'empty','objects':{}})
        self.args=argparse.Namespace(until='animation',library=self.library,bindings=None,max_attempts=1,
            execution_mode='beats',resume=True,orientation='landscape',on_beat=None)
        self.req=requirement();self.teaching=teaching();self.script=script();self.timing=timing()
        self.narration=narration();self.canvas=canvas();self.canvas['teaching_sequence']=self.teaching['teaching_sequence']

    def freeze(self,comp):
        artifacts={'inputs.json':{'contract':'infofilm.animation-workflow.v2','library':str(self.library)},
            'requirement.json':self.req,'teaching-plan.json':self.teaching,'narration-script-plan.json':self.script,
            'timing-cues-plan.json':self.timing,'canvas-landscape-plan.json':self.canvas,
            'components-plan.json':comp,'attached-narration.json':self.narration}
        for name,value in artifacts.items():write(self.store.root/name,value)
        return {name:(self.store.root/name).read_bytes() for name in artifacts}

    def execute(self,model,comp):
        from infofilm.animation_beat_workflow import execute_beats
        return execute_beats(self.args,self.store,model,self.req,self.teaching,self.script,self.timing,
            self.narration,{'landscape':self.canvas},comp,beat_word_spans(self.script,self.narration),
            'Check teaching meaning and concrete semantic defects.',
            {'planner_model':'test-frontier','reviewer_model':'test-reviewer'},placement_plan_errors,motion_errors)

    def model(self,candidates):
        return FakeModels(candidates,[good_review() for _ in candidates])

    def assert_preserved(self,before):
        for name,value in before.items():self.assertEqual((self.store.root/name).read_bytes(),value,name)

    def test_scoped_plans_assemble_one_chapter_and_resume_without_replanning_or_upstream_changes(self):
        comp=components();before=self.freeze(comp);events=[];self.args.on_beat=events.append
        model=self.model([selection('release'),placement('release'),animation('release'),
            selection('landing'),placement('landing'),animation('landing')])
        result=self.execute(model,comp)
        self.assertEqual(result['status'],0);self.assertEqual(len(model.calls),6);self.assertEqual(len(model.review_calls),6)
        chapter=result['compiled_plans']['landscape']
        self.assertEqual(chapter['schema_version'],'infofilm.animation-beat-chapter.v1')
        self.assertEqual([b['beat_id'] for b in chapter['beats']],['release','landing'])
        self.assertEqual([b['duration_frames'] for b in chapter['beats']],[120,120])
        self.assertEqual(' '.join(c['text'] for c in chapter['captions']),self.narration['transcript'])
        self.assertTrue(all(not b['plan']['captions'] for b in chapter['beats']))
        # Explicit reset allows a reused drawing to change size and style per beat.
        self.assertNotEqual(chapter['beats'][0]['plan']['objects'][0]['width'],chapter['beats'][1]['plan']['objects'][0]['width'])
        for call in model.calls[:3]:
            context=json.loads(call['prompt'].split('\nINPUT:\n',1)[1])
            self.assertEqual([w['id'] for w in context['narration']['words']],['W1','W2','W3','W4'])
            self.assertEqual([b['id'] for b in context['canvas_plan']['teaching_sequence']],['release'])
            self.assertEqual(context['current_beat']['id'],'release')
        for call in model.calls[3:]:
            context=json.loads(call['prompt'].split('\nINPUT:\n',1)[1])
            self.assertEqual([w['id'] for w in context['narration']['words']],['W5','W6','W7'])
            self.assertEqual([b['id'] for b in context['canvas_plan']['teaching_sequence']],['landing'])
        self.assertTrue(any(e['beat_id']=='landing' and e['position']==2 and e['total']==2 for e in events))
        self.assertEqual(self.execute(model,comp)['compiled_plans'],result['compiled_plans'])
        self.assertEqual(len(model.calls),6);self.assertEqual(len(model.review_calls),6);self.assert_preserved(before)

    def test_later_asset_pause_resumes_only_that_beat_after_cache_fill(self):
        comp=components(svg=True);before=self.freeze(comp)
        first=self.model([selection('release'),placement('release'),animation('release'),selection('landing','gauge')])
        paused=self.execute(first,comp)
        self.assertEqual(paused['status'],3);self.assertEqual(len(first.calls),4)
        state=read(self.store.root/'state.json');self.assertEqual(state['state'],'waiting_for_components')
        self.assertTrue(Path(state['component_plan']).is_relative_to(self.store.root))
        from infofilm.animation_production import asset_waits
        waits=asset_waits(self.store.root.parent)
        self.assertFalse(waits[0]['ready']);self.assertEqual(waits[0]['unresolved'][0]['reason'],'missing_asset')
        saved={str(p.relative_to(self.store.root)):p.read_bytes() for p in self.store.root.rglob('*-animation-plan.json')}
        self.assertTrue(saved)
        svg=self.root/'gauge.svg';svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><rect width="20" height="20"/></svg>')
        requests=read(self.store.root/'object-requests.json')
        request=requests['requests'][0]
        register_object(requests,request['request_id'],svg,
            {'pivot':[.5,.5],'capabilities':['diagram'],'provenance':'Manual test drawing'},
            {'pass':True,'asset_sha256':digest(svg.read_bytes()),'assessed_capabilities':['diagram']},library=self.library)
        waits=asset_waits(self.store.root.parent)
        self.assertTrue(waits[0]['ready']);self.assertEqual(waits[0]['unresolved'],[])
        resumed=self.model([placement('landing','gauge'),animation('landing','gauge')])
        completed=self.execute(resumed,comp)
        self.assertEqual(completed['status'],0);self.assertEqual(len(resumed.calls),2)
        self.assertTrue(all('landing' in call['name'] for call in resumed.calls))
        for name,value in saved.items():self.assertEqual((self.store.root/name).read_bytes(),value)
        self.assert_preserved(before)

    def test_gui_cache_readiness_preserves_the_applied_binding_in_the_paused_subset(self):
        from infofilm.animation_production import asset_waits
        comp=components(svg=True);before=self.freeze(comp)
        self.args.bindings=write(self.root/'bindings.json',{'gauge':'science.bound-gauge.v2'})
        model=self.model([selection('release'),placement('release'),animation('release'),selection('landing','gauge')])
        self.assertEqual(self.execute(model,comp)['status'],3)
        waits=asset_waits(self.store.root.parent)
        self.assertFalse(waits[0]['ready'])
        self.assertEqual(waits[0]['unresolved'][0]['requested_asset_id'],'science.bound-gauge.v2')
        scoped=read(read(self.store.root/'state.json')['component_plan'])
        self.assertEqual(scoped['objects'][0]['asset_id'],'science.bound-gauge.v2')
        self.assertIsNone(scoped['objects'][0]['request_id'])
        self.assertEqual(scoped['component_requests'],[])
        svg=self.root/'bound-gauge.svg';svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><rect width="20" height="20"/></svg>')
        requests=read(self.store.root/'object-requests.json')
        register_object(requests,requests['requests'][0]['request_id'],svg,
            {'pivot':[.5,.5],'capabilities':['diagram'],'provenance':'Original bound test asset'},
            {'pass':True,'asset_sha256':digest(svg.read_bytes()),'assessed_capabilities':['diagram']},library=self.library)
        self.assertTrue(asset_waits(self.store.root.parent)[0]['ready'])
        resumed=self.model([placement('landing','gauge'),animation('landing','gauge')])
        result=self.execute(resumed,comp)
        self.assertEqual(result['status'],0)
        self.assertIn('science.bound-gauge.v2',result['assets'])
        self.assertEqual(len(resumed.calls),2);self.assert_preserved(before)

    def test_until_placement_stops_without_motion_and_resumes_from_saved_selection(self):
        comp=components();before=self.freeze(comp);self.args.until='placement'
        first=self.model([selection('release'),placement('release')])
        result=self.execute(first,comp)
        self.assertEqual(result['status'],0);self.assertFalse(result['compiled_plans']);self.assertEqual(len(first.calls),2)
        self.args.until='animation'
        resumed=self.model([animation('release'),selection('landing'),placement('landing'),animation('landing')])
        self.assertEqual(self.execute(resumed,comp)['status'],0);self.assertEqual(len(resumed.calls),4)
        self.assertNotIn('selection',resumed.calls[0]['name']);self.assert_preserved(before)

    def test_continuous_boundary_rejects_a_visible_jump_instead_of_accepting_a_reset(self):
        comp=components();self.freeze(comp)
        model=self.model([selection('release'),placement('release'),animation('release'),
            selection('landing',continuous=True),placement('landing'),animation('landing')])
        with self.assertRaisesRegex(RuntimeError,'needs review'):self.execute(model,comp)
        evaluations=[read(p) for p in self.store.root.rglob('evaluation.json')]
        errors=[error for value in evaluations for attempt in value.get('attempts',[]) for error in attempt.get('code_errors',[])]
        self.assertTrue(any('continuous' in e.lower() for e in errors),errors)

    def test_essential_motion_gap_pauses_with_earlier_beat_retained(self):
        comp=components();before=self.freeze(comp)
        model=self.model([selection('release'),placement('release'),animation('release'),
            selection('landing'),placement('landing'),animation('landing',gap=True)])
        result=self.execute(model,comp)
        self.assertEqual(result['status'],4)
        self.assertEqual(read(self.store.root/'state.json')['state'],'waiting_for_motion_capabilities')
        self.assertEqual(read(self.store.root/'motion-capability-requests.json')['requests'][0]['behavior'],'fluid_solver')
        self.assertTrue(list(self.store.root.rglob('*release-animation-plan.json')));self.assert_preserved(before)

    def test_measured_layout_failure_replans_only_its_frame_owning_beat(self):
        comp=components();before=self.freeze(comp)
        first=self.model([selection('release'),placement('release'),animation('release'),
            selection('landing'),placement('landing'),animation('landing')])
        self.assertEqual(self.execute(first,comp)['status'],0)
        release={str(p.relative_to(self.store.root)):p.read_bytes()
            for p in self.store.root.rglob('*-plan.json') if '/01-release/' in str(p)}
        write(self.store.root/'render-feedback.json',{'landscape':{
            'instruction':'Repair the measured rendered layout defect.',
            'examples':[{'frame':180,'problem':'The object covers a label'}],'receipt_sha256':'measured-feedback'}})
        corrected=placement('landing');corrected['objects'][0]['height']=.07
        repaired=self.model([corrected,animation('landing')])
        self.assertEqual(self.execute(repaired,comp)['status'],0)
        self.assertEqual(len(repaired.calls),2)
        self.assertTrue(all('landing' in call['name'] for call in repaired.calls))
        self.assertIn('The object covers a label',repaired.calls[0]['prompt'])
        for name,value in release.items():self.assertEqual((self.store.root/name).read_bytes(),value)
        self.assert_preserved(before)

    def test_full_ordered_workflow_renders_the_assembled_chapter_and_reuses_every_phase(self):
        from infofilm.animation_workflow import run
        args=self.args
        args.output=self.store.root;args.requirement=write(self.root/'requirement.json',self.req)
        args.config=write(self.root/'config.json',{'reviewer_url':'http://reviewer.invalid/v1','browser':'/bin/unused'})
        args.chapter_plan=None;args.situation=None;args.model='test-frontier';args.reviewer_model='test-reviewer'
        args.continuity_model='local';args.reasoning_effort='high';args.max_output_tokens=24000
        args.resume=False;args.until='render';args.max_render_repairs=0
        model=self.model([self.teaching,self.script,self.timing,self.canvas,components(),
            selection('release'),placement('release'),animation('release'),
            selection('landing'),placement('landing'),animation('landing')])
        model.timing_mapper=lambda:model
        events=[];renders=[];original_call=model.call
        def call(role,name,*a,**kw):
            events.append(name);return original_call(role,name,*a,**kw)
        model.call=call
        def audio(store,config):
            events.append('SPEECH_MFA')
            value={**self.narration,'audio':str(args.config)}
            write(store.root/'attached-narration.json',value);return value
        class Child:
            returncode=0
            stdout=iter(['{"state":"rendering","progress":100}\n'])
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def wait(self):return 0
        def render(command,**kwargs):
            self.assertEqual(len(model.calls),11)
            plan=read(args.output/'renders/landscape/compiled-plan.json')
            self.assertEqual(plan['schema_version'],'infofilm.animation-beat-chapter.v1')
            self.assertEqual(len(plan['beats']),2)
            props=read(args.output/'renders/landscape/props.json')
            self.assertEqual(props['plan'],plan)
            self.assertTrue(props['audio_src'].startswith('data:audio/wav;base64,'))
            renders.append(command);return Child()
        with patch('infofilm.animation_workflow.WorkflowModels',return_value=model), \
             patch('infofilm.animation_workflow.prepare_audio',side_effect=audio), \
             patch('infofilm.animation_workflow.publish_report'), \
             patch('infofilm.animation_workflow.subprocess.Popen',side_effect=render), \
             patch('infofilm.animation_workflow.render_reusable',side_effect=lambda *a:bool(renders)):
            self.assertEqual(run(args),0)
            before={p.name:p.read_bytes() for p in args.output.glob('*-plan.json')}
            args.resume=True;self.assertEqual(run(args),0)
        self.assertIn('teaching',events[0]);self.assertIn('narration-script',events[1])
        self.assertEqual(events[2],'SPEECH_MFA');self.assertIn('canvas',events[4]);self.assertIn('components',events[5])
        self.assertEqual(len(model.calls),11);self.assertEqual(len(model.review_calls),11);self.assertEqual(len(renders),1)
        self.assertEqual(read(args.output/'state.json')['state'],'render_ready')
        for name,value in before.items():self.assertEqual((args.output/name).read_bytes(),value)


if __name__=='__main__':unittest.main()
