"""Beat execution coverage, immutable catalogue reuse and compiler continuity."""
import copy
import unittest

from infofilm.animation_beats import (
    CHAPTER_SCHEMA, SELECTION_SCHEMA, apply_cycle_carries, assemble_chapter, beat_narration,
    build_manifest, compile_beat, component_subset, continuity_errors,
    dependency_receipt, invalidated_phases, namespace_plan, namespaced_id,
    placement_continuity_errors, selection_errors, terminal_state,
)
from infofilm.animation_experiment import anchor_time
from infofilm.animation_motion import transforms


def anchor(identity, edge='start'):
    return {'word_id':identity,'edge':edge,'offset_seconds':0}


def fixture():
    words = [{'id':'w01','word':'First,','start':.31,'end':.65},
        {'id':'w02','word':'observe.','start':.7,'end':1.4},
        {'id':'w03','word':'Then','start':3.17,'end':3.6},
        {'id':'w04','word':'compare.','start':3.7,'end':4.4},
        {'id':'w05','word':'Finally,','start':6.1,'end':6.6},
        {'id':'w06','word':'conclude.','start':6.7,'end':7.8}]
    narration = {'duration_seconds':8.23,'words':words,'transcript':' '.join(w['word'] for w in words)}
    teaching = {'teaching_sequence':[{'id':identity,'message':'Teach '+identity,
        'demonstration':'Demonstrate '+identity,'demonstration_mode':'guided','required_behaviors':[]}
        for identity in ('B1','B2','B3')]}
    timing = {'beats':[{'beat_id':identity,'start_anchor':anchor(words[index*2]['id']),
        'end_anchor':anchor(words[index*2+1]['id'],'end'),'repeat_anchor':None}
        for index,identity in enumerate(('B1','B2','B3'))]}
    canvas = {'viewport':{'width':640,'height':360,'fps':10},
        'palette':{'background':'#071b29'},'orientation':'landscape'}
    components = {'schema_version':'infofilm.animation-components.v1','requirement_id':'concept',
        'scene_id':'concept','visual_message':'Observe then compare', 'teaching_strategy':'Show clear steps',
        'objects':[{'id':'ball','kind':'svg','physics_role':'static','role':'Example',
            'conveys':'Motion','asset_id':'ball.v1','request_id':None,'required_capabilities':['circular']},
            {'id':'unused','kind':'rect','physics_role':'static','role':'Optional',
             'conveys':'Context','asset_id':None,'request_id':None,'required_capabilities':[]}],
        'component_requests':[]}
    spans = {identity:words[index*2:index*2+2] for index,identity in enumerate(('B1','B2','B3'))}
    manifest = build_manifest(teaching,timing,narration,canvas,components,fps=10,word_spans=spans)
    assets = {'ball.v1':{'sha256':'a'*64,'pivot':[.5,.5],'capabilities':['circular']}}
    return manifest,narration,teaching,timing,canvas,components,assets,spans


def selection(beat_id, ids=('ball',), transition='explained_reset'):
    return {'schema_version':SELECTION_SCHEMA,'beat_id':beat_id,'object_ids':list(ids),
        'object_roles':[{'id':identity,'reason':'Needed for this demonstration'} for identity in ids],
        'transition':{'kind':transition,'retained_object_ids':[],'reason':'Begin a new demonstration'},
        'component_requests':[]}


def compile_fixture(beat, narration, canvas, components, assets, size=.1, x=.4, rotation=0,
                    previous=None, transition=None, cycle=False):
    components = component_subset(components,['ball'])
    placed = {'schema_version':'infofilm.animation-placement.v1','scene_id':components['scene_id'],
        'visual_message':components['visual_message'],'explanation':'Clear setup','relationships':[],
        'objects':[{'id':'ball','x':x,'y':.3,'width':size,'height':size*640/360,'rotation':rotation}]}
    track = {'kind':'hold','x':x,'y':.3,'rotation':rotation,
             'start_anchor':anchor('audio_start'),'end_anchor':anchor('audio_end','end')}
    motion = {'schema_version':'infofilm.animation-motion-draft.v1','scene_id':components['scene_id'],
        'visual_message':components['visual_message'],'explanation':'Demonstrate',
        'objects':[{'id':'ball','tracks':[track]}], 'cycles':[],
        'checkpoints':[{'id':identity,'anchor':anchor(word),'reason':'Inspect','object_ids':['ball']}
            for identity,word in [('entry','audio_start'),('middle',narration['words'][0]['id']),('exit','audio_end')]]}
    if cycle:
        motion['cycles'] = [{'id':'spin','start_anchor':anchor('audio_start'),
            'end_anchor':anchor('audio_end','end'),'period_seconds':1}]
        track.pop('rotation'); track.update(cycle_id='spin',rotation_from=0,rotation_to=360)
    compiled,errors = compile_beat(motion,placed,canvas,components,assets,narration,beat,
        previous_state=previous,transition=transition,fps=10)
    return compiled,errors,placed,motion,components


class BeatTimingTests(unittest.TestCase):
    def test_contiguous_ownership_covers_leading_pauses_and_trailing_silence(self):
        manifest,*_ = fixture(); rows = manifest['beats']
        self.assertEqual([(b['start_frame'],b['duration_frames']) for b in rows],[(0,31),(31,30),(61,22)])
        frames = [frame for beat in rows for frame in range(beat['start_frame'],beat['start_frame']+beat['duration_frames'])]
        self.assertEqual(frames,list(range(83)))
        self.assertEqual(rows[0]['narration_window']['end'],1.4)
        self.assertEqual(rows[0]['visual_window']['end'],3.1)
        self.assertEqual(manifest['duration_seconds'],8.3)

    def test_global_word_ids_and_repeat_anchors_rebase_without_new_ids(self):
        manifest,narration,_,timing,*_ = fixture()
        local = beat_narration(manifest,'B2',narration)
        self.assertEqual([w['id'] for w in local['words']],['w03','w04'])
        self.assertAlmostEqual(local['words'][0]['start'],.07)
        self.assertEqual(local['words'][0]['global_start'],3.17)
        self.assertAlmostEqual(anchor_time(timing['beats'][1]['start_anchor'],local),.07)
        self.assertEqual(anchor_time(anchor('audio_end'),local),3)
        self.assertEqual(local['clock']['global_offset_seconds'],3.1)

    def test_interior_teaching_cue_cannot_strip_earlier_paragraph_words(self):
        _,narration,teaching,timing,canvas,components,_,spans = fixture()
        timing['beats'][1]['start_anchor'] = anchor('w04')
        manifest = build_manifest(teaching,timing,narration,canvas,components,fps=10,word_spans=spans)
        self.assertEqual(manifest['beats'][1]['start_frame'],31)
        self.assertEqual(beat_narration(manifest,'B2',narration)['words'][0]['id'],'w03')

    def test_overlapping_paragraphs_rejected_instead_of_duplication(self):
        _,narration,teaching,timing,canvas,components,_,spans = fixture()
        spans['B2'].insert(0,spans['B1'][-1])
        with self.assertRaisesRegex(ValueError,'partition all MFA'):
            build_manifest(teaching,timing,narration,canvas,components,fps=10,word_spans=spans)

    def test_assembly_owns_audio_clock_and_captions_once_with_namespaced_checkpoints(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture()
        plans = {}
        for beat in manifest['beats']:
            plan,errors,*_ = compile_fixture(beat,beat_narration(manifest,beat['beat_id'],narration),canvas,components,assets)
            self.assertEqual(errors,[]);plans[beat['beat_id']] = plan
        chapter = assemble_chapter(manifest,plans,narration)
        self.assertEqual(chapter['schema_version'],CHAPTER_SCHEMA)
        self.assertEqual(chapter['scene_id'],'concept')
        self.assertTrue(chapter['captions'])
        self.assertTrue(all(row['plan']['captions'] == [] for row in chapter['beats']))
        self.assertEqual(len({row['id'] for row in chapter['checkpoints']}),9)
        b2 = [row for row in chapter['checkpoints'] if row['beat_id'] == 'B2']
        self.assertEqual(b2[0]['time'],3.1)
        with self.assertRaisesRegex(ValueError,'each accepted'):
            assemble_chapter(manifest,{'B1':plans['B1']},narration)


class BeatCatalogueTests(unittest.TestCase):
    def test_subset_reuses_svg_at_independent_scale_and_never_mutates_catalogue(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture(); original = copy.deepcopy(components)
        first,errors,*_ = compile_fixture(manifest['beats'][0],beat_narration(manifest,'B1',narration),canvas,components,assets,size=.1)
        self.assertEqual(errors,[])
        second,errors,*_ = compile_fixture(manifest['beats'][1],beat_narration(manifest,'B2',narration),canvas,components,assets,size=.2)
        self.assertEqual(errors,[])
        self.assertEqual(first['objects'][0]['asset_id'],second['objects'][0]['asset_id'])
        self.assertNotEqual(first['objects'][0]['width'],second['objects'][0]['width'])
        self.assertEqual(components,original)

    def test_dependency_closure_includes_nested_link_without_unrelated_instances(self):
        _,_,_,_,_,components,*_ = fixture()
        rod = {**components['objects'][1],'id':'rod','dependency_ids':['ball']}
        components['objects'].append(rod)
        subset = component_subset(components,['rod'])
        self.assertEqual([obj['id'] for obj in subset['objects']],['ball','rod'])
        self.assertEqual(subset['selection_provenance']['dependency_object_ids'],['ball'])
        motion = {'objects':[{'id':'rod','tracks':[{'kind':'pin_link','a':{'object_id':'ball'},'b':{'object_id':'ball'}}]}]}
        self.assertEqual(len(component_subset(components,['rod'],motion=motion)['objects']),2)

    def test_missing_dependencies_and_dependency_cycles_stop_before_model_execution(self):
        _,_,_,_,_,components,*_ = fixture()
        components['objects'][0]['dependency_ids'] = ['missing']
        with self.assertRaisesRegex(ValueError,'Missing object dependency'):
            component_subset(components,['ball'])
        components['objects'][0]['dependency_ids'] = ['unused']
        components['objects'][1]['dependency_ids'] = ['ball']
        with self.assertRaisesRegex(ValueError,'dependency cycle'):
            component_subset(components,['ball'])

    def test_scoped_projectile_setup_override_is_reviewable_and_does_not_change_asset(self):
        manifest,_,_,_,_,components,*_ = fixture(); components['objects'][0]['physics_role'] = 'projectile'
        chosen = selection('B1'); chosen['object_roles'][0]['physics_role'] = 'static'
        self.assertEqual(selection_errors(chosen,manifest['beats'][0],components),[])
        subset = component_subset(components,chosen)
        self.assertEqual(subset['objects'][0]['physics_role'],'static')
        self.assertEqual(components['objects'][0]['physics_role'],'projectile')
        self.assertEqual(subset['objects'][0]['asset_id'],'ball.v1')
        components['objects'][0]['kind'] = 'text'
        self.assertTrue(any('annotation' in error for error in selection_errors(chosen,manifest['beats'][0],components)))

    def test_selection_validates_exact_roles_and_unknown_objects(self):
        manifest,_,_,_,_,components,*_ = fixture(); chosen = selection('B1')
        self.assertEqual(selection_errors(chosen,manifest['beats'][0],components),[])
        chosen['object_roles'] = []
        self.assertTrue(selection_errors(chosen,manifest['beats'][0],components))
        chosen = selection('B1',('missing',))
        self.assertTrue(selection_errors(chosen,manifest['beats'][0],components))


class BeatContinuityTests(unittest.TestCase):
    def test_terminal_receipt_uses_actual_last_rendered_pose_and_asset_styles(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture()
        plan,errors,*_ = compile_fixture(manifest['beats'][0],beat_narration(manifest,'B1',narration),canvas,components,assets)
        self.assertEqual(errors,[])
        plan['objects'][0]['tracks'] = [{'kind':'linear','start':0,'end':3.1,'from':[.4,.3],'to':[.6,.3],'opacity':1}]
        plan['objects'][0]['color'] = '#ff0000'
        state = terminal_state(plan,assets,'B1')
        self.assertEqual(state['at_frame'],30)
        self.assertAlmostEqual(state['objects']['ball']['x'],.4+.2*3/3.1)
        self.assertEqual(state['objects']['ball']['styles'],{'color':'#ff0000'})
        self.assertEqual(state['objects']['ball']['asset_sha256'],'a'*64)

    def test_omitting_retained_list_does_not_allow_visible_teleport_or_resize(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture()
        before,_,*_ = compile_fixture(manifest['beats'][0],beat_narration(manifest,'B1',narration),canvas,components,assets)
        previous = terminal_state(before,assets,'B1'); transition = selection('B2',transition='continuous')['transition']
        _,errors,*_ = compile_fixture(manifest['beats'][1],beat_narration(manifest,'B2',narration),canvas,components,assets,x=.6,previous=previous,transition=transition)
        self.assertTrue(any('jumps' in error for error in errors))
        _,errors,*_ = compile_fixture(manifest['beats'][1],beat_narration(manifest,'B2',narration),canvas,components,assets,size=.2,previous=previous,transition=transition)
        self.assertTrue(any('changes artwork' in error for error in errors))
        reset = selection('B2')['transition']
        _,errors,*_ = compile_fixture(manifest['beats'][1],beat_narration(manifest,'B2',narration),canvas,components,assets,size=.2,previous=previous,transition=reset)
        self.assertEqual(errors,[])

    def test_matching_retained_entry_passes_and_hidden_reintroduction_is_allowed(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture()
        before,_,*_ = compile_fixture(manifest['beats'][0],beat_narration(manifest,'B1',narration),canvas,components,assets)
        previous = terminal_state(before,assets,'B1'); transition = selection('B2',transition='continuous')['transition']
        transition['retained_object_ids'] = ['ball']
        _,errors,*_ = compile_fixture(manifest['beats'][1],beat_narration(manifest,'B2',narration),canvas,components,assets,previous=previous,transition=transition)
        self.assertEqual(errors,[])
        previous['objects']['ball']['visible'] = False; transition['retained_object_ids'] = []
        _,errors,*_ = compile_fixture(manifest['beats'][1],beat_narration(manifest,'B2',narration),canvas,components,assets,x=.6,previous=previous,transition=transition)
        self.assertEqual(errors,[])

    def test_placement_catches_carry_error_before_freezing_a_motion_dead_end(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture()
        before,_,placed,_,subset = compile_fixture(manifest['beats'][0],beat_narration(manifest,'B1',narration),canvas,components,assets)
        previous = terminal_state(before,assets,'B1');transition = selection('B2',transition='continuous')['transition']
        self.assertEqual(placement_continuity_errors(previous,placed,subset,assets,transition,canvas),[])
        placed['objects'][0]['x'] = .6
        self.assertTrue(any('jumps' in error for error in placement_continuity_errors(previous,placed,subset,assets,transition,canvas)))
        transition['kind'] = 'explained_reset'
        self.assertEqual(placement_continuity_errors(previous,placed,subset,assets,transition,canvas),[])

    def test_cycle_carry_keeps_measured_phase_and_rejects_silent_restart(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture()
        before,errors,*_ = compile_fixture(manifest['beats'][0],beat_narration(manifest,'B1',narration),canvas,components,assets,cycle=True)
        self.assertEqual(errors,[])
        # 31-frame first beat ends at t=3.0 exactly; choose a nonzero measured phase.
        before['cycles'][0]['period_seconds'] = 1.3
        previous = terminal_state(before,assets,'B1'); phase = previous['cycles']['spin']['phase']
        local = beat_narration(manifest,'B2',narration)
        _,_,placed,motion,subset = compile_fixture(manifest['beats'][1],local,canvas,components,assets,cycle=True)
        placed['objects'][0]['rotation'] = phase*360; motion['cycles'][0]['period_seconds'] = 1.3
        transition = selection('B2',transition='continuous')['transition'];transition['retained_object_ids'] = ['ball']
        transition['cycle_carries'] = [{'cycle_id':'spin','from_cycle_id':'spin'}]
        after,errors = compile_beat(motion,placed,canvas,subset,assets,local,manifest['beats'][1],previous,transition,fps=10)
        self.assertEqual(errors,[])
        self.assertAlmostEqual(after['cycles'][0]['phase_offset'],phase)
        self.assertAlmostEqual(transforms(after,0,assets)['ball']['rotation'],phase*360)
        repaired,errors = apply_cycle_carries(motion,previous,transition)
        self.assertEqual(errors,[])
        self.assertAlmostEqual(repaired['cycles'][0]['phase_offset'],phase)
        self.assertNotIn('phase_offset',motion['cycles'][0])
        transition['cycle_carries'] = []
        _,errors = compile_beat(motion,placed,canvas,subset,assets,local,manifest['beats'][1],previous,transition,fps=10)
        self.assertTrue(any('cycle carry' in error for error in errors))

    def test_retained_link_requires_all_dependency_objects(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture()
        plan,_,*_ = compile_fixture(manifest['beats'][0],beat_narration(manifest,'B1',narration),canvas,components,assets)
        previous = terminal_state(plan,assets,'B1');previous['objects']['ball']['dependency_ids'] = ['rod']
        errors = continuity_errors(previous,plan,assets,{'kind':'continuous','retained_object_ids':['ball']})
        self.assertTrue(any('complete dependency' in error for error in errors))

    def test_coincident_cycle_pose_does_not_allow_restart_by_omitting_retained_list(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture()
        before,errors,*_ = compile_fixture(manifest['beats'][0],beat_narration(manifest,'B1',narration),canvas,components,assets,cycle=True)
        self.assertEqual(errors,[])
        previous = terminal_state(before,assets,'B1')
        self.assertEqual(previous['cycles']['spin']['phase'],0)
        transition = selection('B2',transition='continuous')['transition']
        after,errors,*_ = compile_fixture(manifest['beats'][1],beat_narration(manifest,'B2',narration),canvas,components,assets,cycle=True)
        self.assertEqual(errors,[])
        # Poses coincide at phase zero; an undeclared new clock is still a restart.
        self.assertEqual(transforms(after,0,assets)['ball']['rotation'],previous['objects']['ball']['rotation'] % 360)
        errors = continuity_errors(previous,after,assets,transition)
        self.assertTrue(any('explicit shared cycle carry' in error for error in errors))
        self.assertTrue(any('hold the computed incoming pose' in error for error in errors))
        self.assertFalse(any('jumps' in error for error in errors))
        held,errors,*_ = compile_fixture(manifest['beats'][1],beat_narration(manifest,'B2',narration),canvas,components,assets)
        self.assertEqual(errors,[])
        self.assertEqual(continuity_errors(previous,held,assets,transition),[])

    def test_namespace_transforms_links_cycles_and_checkpoints_without_touching_svg_ids(self):
        plan = {'objects':[{'id':'left','asset_id':'ball.v1','tracks':[{'kind':'hold','cycle_id':'spin'}]},
            {'id':'rod','asset_id':'rod.v1','tracks':[{'kind':'pin_link','a':{'object_id':'left'},'b':{'object_id':'left'}}]}],
            'cycles':[{'id':'spin'}], 'checkpoints':[{'id':'entry','object_ids':['left','rod']}], 'captions':[]}
        renamed = namespace_plan(plan,'B1')
        self.assertEqual(renamed['objects'][1]['tracks'][0]['a']['object_id'],renamed['objects'][0]['id'])
        self.assertEqual(renamed['objects'][0]['tracks'][0]['cycle_id'],renamed['cycles'][0]['id'])
        self.assertEqual(renamed['objects'][0]['asset_id'],'ball.v1')
        self.assertNotEqual(namespaced_id('B1','left'),namespaced_id('B2','left'))
        self.assertEqual(plan['objects'][0]['id'],'left')


class BeatInvalidationTests(unittest.TestCase):
    def test_motion_only_invalidates_compiled_render_and_unused_previous_changes_do_not_propagate(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture();beat = manifest['beats'][1]
        chosen = selection('B2',transition='continuous');chosen['transition']['retained_object_ids'] = ['ball']
        kwargs = dict(canvas=canvas,components=components,assets=assets,narration=beat_narration(manifest,'B2',narration),
            selection=chosen,placement={'geometry':'frozen'},motion={'objects':[]},
            incoming_state={'objects':{'ball':{'x':.4},'unused':{'x':.9}}})
        before = dependency_receipt(beat,**kwargs)
        changed = copy.deepcopy(kwargs); changed['incoming_state']['objects']['unused']['x'] = .1
        self.assertEqual(dependency_receipt(beat,**changed),before)
        changed['motion']['explanation'] = 'Correct this beat'
        self.assertEqual(invalidated_phases(before,dependency_receipt(beat,**changed)),['compiled','render'])
        changed = copy.deepcopy(kwargs);changed['placement']['geometry'] = 'New positions'
        self.assertEqual(invalidated_phases(before,dependency_receipt(beat,**changed)),['motion','compiled','render'])
        changed = copy.deepcopy(kwargs);changed['incoming_state']['objects']['ball']['x'] = .5
        self.assertEqual(invalidated_phases(before,dependency_receipt(beat,**changed)),['placement','motion','compiled','render'])

    def test_omitted_retained_list_cannot_bypass_actual_visible_dependency_invalidation(self):
        manifest,narration,_,_,canvas,components,assets,_ = fixture();beat = manifest['beats'][1]
        chosen = selection('B2',transition='continuous')
        kwargs = dict(canvas=canvas,components=components,assets=assets,narration=beat_narration(manifest,'B2',narration),
            selection=chosen,placement={},motion={'objects':[]},
            incoming_state={'objects':{'ball':{'x':.4,'visible':True}}})
        before = dependency_receipt(beat,**kwargs)
        self.assertEqual(before['incoming_object_ids'],['ball'])
        kwargs['incoming_state']['objects']['ball']['x'] = .6
        self.assertEqual(invalidated_phases(before,dependency_receipt(beat,**kwargs)),['placement','motion','compiled','render'])


if __name__ == '__main__':
    unittest.main()
