"""Ordering, immutable resume, semantic repairs, shared cycles and actual renderer parity."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from infofilm.animation_workflow import (run,teaching_errors,timing_errors,beat_word_spans,motion_errors)
from infofilm.animation_motion import compile_motion_v2,transforms
from infofilm.animation_frontier import main
from infofilm.animation_objects import register_object
from infofilm.core import ROOT,read,write,digest
from test_animation_experiment import FakeModels,requirement,narration,anchor,good_review
from test_animation_frontier import canvas


def teaching():
    req=requirement()
    return {'schema_version':'infofilm.animation-teaching.v2','requirement_id':req['id'],'visual_message':req['visual_message'],
        'explanation_strategy':'Demonstrate slowly, then repeat together','assumptions':[],
        'teaching_sequence':[{'id':'compare','message':'Compare coordinated motion','demonstration':'Move together',
            'demonstration_mode':'guided_then_loop','required_behaviors':['continuous_rotation','synchronized_cycle'],
            'repeat_purpose':'Observe the complete repeated flow'}]}


def script():
    req=requirement()
    return {'schema_version':'infofilm.animation-narration-script.v1','requirement_id':req['id'],
        'visual_message':req['visual_message'],'title':'Demonstration',
        'points':[{'id':'N01','beat_ids':['compare'],'narration':narration()['transcript']}]}


def timing():
    return {'schema_version':'infofilm.animation-timing-cues.v2','beats':[{'beat_id':'compare',
        'start_anchor':anchor('W1'),'end_anchor':anchor('W7','end'),
        'repeat_anchor':anchor('W5'),'repeat_reason':'The narration enters its comparison'}]}


def components():
    req=requirement()
    return {'schema_version':'infofilm.animation-components.v1','requirement_id':req['id'],'scene_id':req['id'],
        'visual_message':req['visual_message'],'teaching_strategy':'Guided then loop','assumptions':[],
        'objects':[{'id':'rotor','kind':'rect','role':'Rotating object','physics_role':'static',
            'conveys':'Rotation','asset_id':None,'request_id':None,'required_capabilities':[]},
            {'id':'label','kind':'text','role':'Label','physics_role':'annotation','conveys':'Context',
             'asset_id':None,'request_id':None,'required_capabilities':[]}],'component_requests':[]}


def placement():
    req=requirement()
    return {'schema_version':'infofilm.animation-placement.v1','scene_id':req['id'],'visual_message':req['visual_message'],
        'explanation':'Initial geometry','relationships':[],
        'objects':[{'id':'rotor','x':.3,'y':.3,'width':.1,'height':.1,'rotation':0,'color':'#ffffff'},
            {'id':'label','x':.6,'y':.3,'width':.3,'height':.08,'rotation':0,
             'text':'Coordinated cycle','font_size':24,'label_card':True,'color':'#ffffff'}]}


def animation():
    req=requirement()
    return {'schema_version':'infofilm.animation-motion-draft.v1','scene_id':req['id'],'visual_message':req['visual_message'],
        'explanation':'Guide rotation then repeat','cycles':[{'id':'C','start_anchor':anchor('W5'),
            'end_anchor':anchor('audio_end','end'),'period_seconds':2}],
        'objects':[{'id':'rotor','tracks':[
            {'kind':'hold','x':.3,'y':.3,'rotation_from':0,'rotation_to':360,'start_anchor':anchor('audio_start'),'end_anchor':anchor('W5')},
            {'kind':'hold','x':.3,'y':.3,'rotation_from':360,'rotation_to':720,'cycle_id':'C',
             'start_anchor':anchor('W5'),'end_anchor':anchor('audio_end','end')}]},
            {'id':'label','tracks':[{'kind':'hold','x':.6,'y':.3,'start_anchor':anchor('audio_start'),'end_anchor':anchor('audio_end','end')}]}],
        'animation_phases':[{'id':'guided','kind':'guided','beat_ids':['compare'],'start_anchor':anchor('audio_start'),
            'end_anchor':anchor('W5'),'cycle_id':None,'transition':'continuous','purpose':'Explain the movement'},
            {'id':'loop','kind':'loop','beat_ids':['compare'],'start_anchor':anchor('W5'),'end_anchor':anchor('audio_end','end'),
             'cycle_id':'C','transition':'continuous','purpose':'Demonstrate the complete cycle'}],
        'motion_requirements':[{'behavior':'continuous_rotation','object_ids':['rotor'],'importance':'essential','reason':'Rotation is the demonstration'},
            {'behavior':'synchronized_cycle','object_ids':['rotor'],'importance':'essential','reason':'Repeat with a shared clock'}],
        'capability_gaps':[],'checkpoints':[{'id':str(i),'anchor':anchor(w),'reason':'Inspect demonstration','object_ids':['rotor']}
            for i,w in enumerate(['W1','W3','W6'])]}


class WorkflowTests(unittest.TestCase):
    def fixture(self,root,until='animation'):
        root=Path(root);library=root/'library';library.mkdir()
        write(library/'registry.json',{'schema_version':'infofilm.animation-library.v1','revision':'empty','objects':{}})
        config=write(root/'config.json',{'reviewer_url':'http://local.invalid/v1','browser':'/bin/unused'})
        return argparse.Namespace(output=root/'trial',requirement=write(root/'requirement.json',requirement()),
            chapter_plan=None,situation=None,orientation='landscape',config=config,library=library,
            model='gpt-6-luna',reviewer_model=None,continuity_model='local',reasoning_effort='high',
            max_output_tokens=24000,max_attempts=3,resume=False,bindings=None,until=until,execution_mode='chapter')

    def fake(self,candidates,reviews=None):
        model=FakeModels(candidates,reviews if reviews is not None else [good_review() for _ in candidates])
        model.timing_mapper=lambda:model
        return model

    def test_exact_order_including_audio_before_canvas_and_resume_without_calls(self):
        with tempfile.TemporaryDirectory() as root:
            args=self.fixture(root);c=canvas();c['teaching_sequence']=teaching()['teaching_sequence']
            candidates=[teaching(),script(),timing(),c,components(),placement(),animation()]
            model=self.fake(candidates);events=[]
            oldcall=model.call
            def call(role,name,*a,**kw):events.append(name);return oldcall(role,name,*a,**kw)
            model.call=call
            def audio(store,config):
                events.append('SPEECH_MFA');write(store.root/'attached-narration.json',narration());return narration()
            with patch('infofilm.animation_workflow.WorkflowModels',return_value=model),patch('infofilm.animation_workflow.prepare_audio',side_effect=audio),patch('infofilm.animation_workflow.publish_report'):
                self.assertEqual(run(args),0)
                self.assertIn('teaching',events[0]);self.assertIn('narration-script',events[1]);self.assertEqual(events[2],'SPEECH_MFA')
                self.assertIn('timing-cues',events[3]);self.assertIn('canvas',events[4]);self.assertIn('components',events[5])
                self.assertIn('placement',events[6]);self.assertIn('animation',events[7])
                self.assertEqual(len(model.review_calls),7)
                args.resume=True;self.assertEqual(run(args),0)
                self.assertEqual(len(model.calls),7);self.assertEqual(len(model.review_calls),7)
            self.assertTrue(read(args.output/'placement-landscape-plan.json')['objects'][1]['label_card'])
            self.assertEqual(read(args.output/'state.json')['state'],'animation_ready')

    def test_asset_fault_after_audio_then_registration_resumes_frozen_phases(self):
        with tempfile.TemporaryDirectory() as root:
            args=self.fixture(root,'objects');c=canvas();c['teaching_sequence']=teaching()['teaching_sequence']
            comp=components();comp['objects']=comp['objects'][:1]
            comp['objects'][0].update(kind='svg',request_id='R',required_capabilities=['rotating'],asset_id=None)
            comp['component_requests']=[{'id':'R','requested_asset_id':'mechanism.rotor.v1','description':'Rotor',
                'required_capabilities':['rotating'],'svg_requirements':{'pivot':[.5,.5]}}]
            model=self.fake([teaching(),script(),timing(),c,comp])
            with patch('infofilm.animation_workflow.WorkflowModels',return_value=model),patch('infofilm.animation_workflow.prepare_audio',return_value=narration()),patch('infofilm.animation_workflow.publish_report'):
                self.assertEqual(run(args),3)
                self.assertEqual(read(args.output/'state.json')['state'],'waiting_for_components')
                before={p.name:p.read_bytes() for p in args.output.glob('*-plan.json')}
                svg=Path(root)/'rotor.svg';svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><rect width="20" height="20"/></svg>')
                register_object(read(args.output/'object-requests.json'),'R',svg,{'pivot':[.5,.5],'capabilities':['rotating'],'provenance':'Test fixture'},
                    {'pass':True,'asset_sha256':digest(svg.read_bytes()),'assessed_capabilities':['rotating']},library=args.library)
                args.resume=True;self.assertEqual(run(args),0);self.assertEqual(len(model.calls),5)
                self.assertEqual(before,{p.name:p.read_bytes() for p in args.output.glob('*-plan.json')})

    def test_semantic_rejection_repairs_only_current_phase(self):
        with tempfile.TemporaryDirectory() as root:
            args=self.fixture(root,'teaching');bad=teaching();fixed=teaching();fixed['teaching_sequence'][0]['message']='Corrected meaning'
            rejection={'pass':False,'score':60,'issues':[{'severity':'major','field':'teaching_sequence[0].message',
                'problem':'Incorrect explanation','correction':'Correct its meaning'}],'strengths':[]}
            model=self.fake([bad,fixed],[rejection,good_review()])
            with patch('infofilm.animation_workflow.WorkflowModels',return_value=model),patch('infofilm.animation_workflow.publish_report'):
                self.assertEqual(run(args),0)
            self.assertIn('Incorrect explanation',model.calls[1]['prompt'])
            self.assertEqual(read(args.output/'teaching-plan.json'),fixed)
            self.assertFalse((args.output/'narration-script-plan.json').exists())

    def test_default_end_to_end_executes_remotion_after_all_planning_and_reuses_it(self):
        with tempfile.TemporaryDirectory() as root:
            args=self.fixture(root,'render');c=canvas();c['teaching_sequence']=teaching()['teaching_sequence']
            model=self.fake([teaching(),script(),timing(),c,components(),placement(),animation()]);rendered=[]
            class Child:
                returncode=0
                stdout=iter(['{"state":"rendering","progress":100}\n'])
                def __enter__(self):return self
                def __exit__(self,*args):return False
                def wait(self):return 0
            def render(command,**kwargs):
                self.assertEqual(len(model.calls),7);self.assertEqual(len(model.review_calls),7)
                self.assertTrue((args.output/'renders/landscape/compiled-plan.json').exists())
                rendered.append(command);return Child()
            with patch('infofilm.animation_workflow.WorkflowModels',return_value=model),patch('infofilm.animation_workflow.prepare_audio',return_value={**narration(),'audio':str(args.config)}), \
                 patch('infofilm.animation_workflow.publish_report'),patch('infofilm.animation_workflow.subprocess.Popen',side_effect=render), \
                 patch('infofilm.animation_workflow.render_reusable',side_effect=lambda *a:bool(rendered)):
                self.assertEqual(run(args),0);args.resume=True;self.assertEqual(run(args),0)
            self.assertEqual(len(rendered),1);self.assertEqual(read(args.output/'state.json')['state'],'render_ready')
            self.assertEqual(len(model.calls),7)

    def test_unavailable_essential_motion_pauses_instead_of_silently_rendering(self):
        with tempfile.TemporaryDirectory() as root:
            args=self.fixture(root);t=teaching();t['teaching_sequence'][0]['required_behaviors'].append('fluid_solver')
            c=canvas();c['teaching_sequence']=t['teaching_sequence'];a=animation()
            a['capability_gaps']=[{'behavior':'fluid_solver','object_ids':['rotor'],'importance':'essential',
                'reason':'No solver exists','lost_explanation':'Detailed fluid behavior'}]
            model=self.fake([t,script(),timing(),c,components(),placement(),a])
            with patch('infofilm.animation_workflow.WorkflowModels',return_value=model),patch('infofilm.animation_workflow.prepare_audio',return_value=narration()),patch('infofilm.animation_workflow.publish_report'):
                self.assertEqual(run(args),4)
            self.assertEqual(read(args.output/'state.json')['state'],'waiting_for_motion_capabilities')
            self.assertEqual(read(args.output/'motion-capability-requests.json')['requests'][0]['behavior'],'fluid_solver')

    def test_actual_layout_feedback_repairs_placement_without_rewriting_upstream(self):
        with tempfile.TemporaryDirectory() as root:
            args=self.fixture(root,'render');c=canvas();c['teaching_sequence']=teaching()['teaching_sequence']
            fixed=placement();fixed['objects'][1]['height']=.1
            model=self.fake([teaching(),script(),timing(),c,components(),placement(),animation(),fixed,animation()]);renders=[]
            class Child:
                def __init__(self,rc):self.returncode=rc;self.stdout=iter([])
                def __enter__(self):return self
                def __exit__(self,*a):return False
                def wait(self):return self.returncode
            def render(command,**kwargs):
                renders.append(command)
                if len(renders)==1:
                    write(Path(command[2])/'render-results.json',{'complete':False,'error':'Animation layout needs review: 2 video frame failures',
                        'video':{'layout_failures':[{'frame':5,'failures':['Text overflow: label']},{'frame':6,'failures':['Text overflow: label']}]}})
                    return Child(1)
                return Child(0)
            with patch('infofilm.animation_workflow.WorkflowModels',return_value=model),patch('infofilm.animation_workflow.prepare_audio',return_value={**narration(),'audio':str(args.config)}), \
                 patch('infofilm.animation_workflow.publish_report'),patch('infofilm.animation_workflow.subprocess.Popen',side_effect=render),patch('infofilm.animation_workflow.render_reusable',return_value=False):
                self.assertEqual(run(args),0)
            self.assertEqual(len(renders),2);self.assertEqual(len(model.calls),9)
            self.assertIn('Text overflow: label',model.calls[7]['prompt'])
            self.assertIn('placement-landscape',model.calls[7]['name']);self.assertIn('animation-landscape',model.calls[8]['name'])
            self.assertEqual(read(args.output/'placement-landscape-plan.json'),fixed)
            self.assertEqual(read(args.output/'state.json')['state'],'render_ready')

    def test_unknown_anchor_and_spontaneous_repeat_are_rejected(self):
        spans=beat_word_spans(script(),narration());v=timing();v['beats'][0]['repeat_anchor']=anchor('invented')
        self.assertTrue(timing_errors(v,teaching(),spans,narration()))
        t=teaching();t['teaching_sequence'][0]['demonstration_mode']='guided'
        self.assertTrue(timing_errors(timing(),t,spans,narration()))

    def test_motion_catches_missing_rotation_different_cycle_and_visible_transition_jump(self):
        c=canvas();c['teaching_sequence']=teaching()['teaching_sequence']
        self.assertEqual(motion_errors(animation(),placement(),c,components(),{},narration(),teaching()),[])
        v=animation()
        for track in v['objects'][0]['tracks']:track.pop('rotation_from');track.pop('rotation_to')
        self.assertTrue(any('no implementation' in e for e in motion_errors(v,placement(),c,components(),{},narration(),teaching())))
        v=animation();v['objects'][0]['tracks'][1]['x']=.5
        self.assertTrue(any('jumps' in e for e in motion_errors(v,placement(),c,components(),{},narration(),teaching())))

    def test_loop_closure_and_too_short_period_window(self):
        from infofilm.animation_frontier import merge_animation
        c=canvas();v=animation();v['objects'][0]['tracks'][1].update(kind='linear',**{'from':[.3,.3],'to':[.5,.3]})
        _,errors=compile_motion_v2(merge_animation(v,placement(),c),components(),{},narration(),1280,720)
        self.assertTrue(any('close seamlessly' in e for e in errors))
        v=animation();v['cycles'][0]['period_seconds']=9
        _,errors=compile_motion_v2(merge_animation(v,placement(),c),components(),{},narration(),1280,720)
        self.assertTrue(any('complete positive period' in e for e in errors))

    def test_new_cli_resume_can_load_saved_handoff(self):
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root);write(folder/'requirement.json',requirement())
            write(folder/'inputs.json',{'contract':'infofilm.animation-workflow.v2','orientations':['portrait'],
                'model':{'planner_model':'custom-frontier','reviewer_model':'custom-reviewer','reasoning_effort':'medium',
                    'max_output_tokens':32000,'continuity_model':'luna'},'library':str(folder/'library')})
            with patch('sys.argv',['experiment','--output',str(folder),'--resume']),patch('infofilm.animation_frontier.run',return_value=0) as runner:
                main()
            args=runner.call_args.args[0];self.assertEqual(args.orientation,'portrait');self.assertEqual(args.workflow,'v2')
            self.assertEqual(args.requirement,folder/'requirement.json')
            self.assertEqual(args.model,'custom-frontier');self.assertEqual(args.reviewer_model,'custom-reviewer')
            self.assertEqual(args.continuity_model,'luna');self.assertEqual(args.library,folder/'library')


class LinkedMotionTests(unittest.TestCase):
    def scene(self):
        cycle={'id':'C','start':0,'end':8,'period_seconds':2}
        base={'start':0,'end':8,'cycle_id':'C'}
        objects=[{'id':'crank','kind':'rect','width':.1,'height':.1,'tracks':[{**base,'kind':'hold','x':.5,'y':.4,'rotation_from':0,'rotation_to':360}]},
            {'id':'piston','kind':'rect','width':.05,'height':.05,'tracks':[{**base,'kind':'slider_crank',
                'crank':{'object_id':'crank','local':[.5,0]},'axis_origin':[.5,.4],'axis_direction':[0,1],
                'rod_length_px':100,'local_pin':[.5,.5],'branch':-1}]},
            {'id':'rod','kind':'rect','width':.02,'height':.1,'tracks':[{**base,'kind':'pin_link',
                'a':{'object_id':'piston','local':[.5,.5]},'b':{'object_id':'crank','local':[.5,0]},'local_a':[.5,0],'local_b':[.5,1]}]}]
        return {'width':1000,'height':1000,'objects':objects,'cycles':[cycle]}

    def test_rigid_link_remains_connected_and_shared_cycle_repeats(self):
        p=self.scene()
        for t in [0,.1,.25,.5,.75,1,1.5,2.1,7.9]:
            a=transforms(p,t,{})
            b=transforms(p,t%2,{})
            for identity in a:
                for key in ('x','y','rotation'):self.assertAlmostEqual(a[identity][key],b[identity][key],places=6)
        bad=self.scene();bad['objects'][2]['height']=.12
        with self.assertRaisesRegex(ValueError,'rigid pin'):transforms(bad,.5,{})

    def test_dependency_cycle_fails_without_infinite_recursion(self):
        p=self.scene();p['objects'][1]['tracks'][0]['crank']['object_id']='rod'
        with self.assertRaisesRegex(ValueError,'dependency cycle'):transforms(p,.5,{})

    def test_actual_typescript_resolver_matches_python_at_multiple_cycle_phases(self):
        with tempfile.TemporaryDirectory() as root:
            source=(ROOT/'renderer/src/animation-experiment/motion.ts').read_text()
            # Use the project's TypeScript transpiler, then execute its real exported resolver in Node.
            command="const ts=require('./node_modules/typescript');const fs=require('fs');fs.writeFileSync(process.argv[1],ts.transpileModule(fs.readFileSync('src/animation-experiment/motion.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText)"
            module=Path(root)/'motion.cjs'
            subprocess.run(['node','-e',command,str(module)],cwd=ROOT/'renderer',check=True,capture_output=True)
            p=self.scene();times=[0,.1,.5,1,1.9,2,7.99,8]
            js="const m=require(process.argv[1]);const p=JSON.parse(process.argv[2]);process.stdout.write(JSON.stringify(JSON.parse(process.argv[3]).map(t=>m.computeSceneTransforms(p,t,{}))))"
            result=subprocess.run(['node','-e',js,str(module),json.dumps(p),json.dumps(times)],check=True,capture_output=True,text=True)
            for t,actual in zip(times,json.loads(result.stdout)):
                expected=transforms(p,t,{})
                for identity in expected:
                    for key in expected[identity]:self.assertAlmostEqual(expected[identity][key],actual[identity][key],places=6)


if __name__=='__main__':unittest.main()
