"""Resumable, scoped visual execution of accepted teaching beats.

The chapter owns its speech and catalogue. A beat owns placement and motion;
models never compute chapter offsets or claim an outgoing rendered state.
"""
from __future__ import annotations

import copy
from pathlib import Path

from .animation_beats import (SELECTION_SHAPE, build_manifest, beat_narration,
    selection_errors, component_subset, compile_beat, terminal_state,
    assemble_chapter, dependency_receipt, apply_cycle_carries, placement_continuity_errors)
from .animation_experiment import ANCHOR_SCHEMA, MOTION_SCHEMA, phase
from .animation_frontier import PLACEMENT_SCHEMA, GENERAL_MOTION_RULES, resolve_objects
from .animation_motion import CAPABILITIES, RULES
from .core import Store, digest, read, write

EXECUTION_SCHEMA = 'infofilm.animation-beat-execution.v1'


def scoped_feedback(feedback, manifest, beat):
    """Map actual renderer failures to their frame-owning beat only."""
    if not feedback:
        return None
    examples = []
    for row in feedback.get('examples', []):
        frame = row.get('frame')
        if row.get('beat_id') == beat['beat_id'] or (isinstance(frame, int) and
            beat['start_frame'] <= frame < beat['start_frame']+beat['duration_frames']):
            examples.append({**row, 'beat_id':beat['beat_id'],
                **({'local_frame':frame-beat['start_frame']} if isinstance(frame,int) else {})})
    if not examples:
        return None
    return {'instruction':feedback['instruction'], 'examples':examples,
            'receipt_sha256':feedback.get('receipt_sha256')}


def _objective(requirement):
    # Chapter-context contains research archives; it is deliberately not repeated
    # in every small motion request. Preserve the scope and qualified grounding.
    result = {key:copy.deepcopy(requirement[key]) for key in
        ('id','subject','learning_objective','visual_message','scene_requirement','audience','constraints','facts')
        if key in requirement}
    chapter = requirement.get('chapter_context',{}).get('chapter',{})
    result['chapter_scope'] = {key:copy.deepcopy(chapter[key]) for key in
        ('scope','objective','exclude','exclusions','entry_context','exit_handoff','exit_context') if key in chapter}
    return result


def _incoming(previous, selected):
    if not previous or selected['transition']['kind'] == 'explained_reset':
        return None
    ids = {obj['id'] for obj in selected['objects']} if isinstance(selected.get('objects'),list) else set(selected['object_ids'])
    objects = {identity:copy.deepcopy(row) for identity,row in previous['objects'].items() if identity in ids}
    cycles = {identity:copy.deepcopy(row) for identity,row in previous.get('cycles',{}).items()
              if set(row['object_ids']) & ids}
    return {'schema_version':previous['schema_version'], 'beat_id':previous['beat_id'],
        'width':previous['width'], 'height':previous['height'], 'objects':objects, 'cycles':cycles}


def _motion_schema():
    schema = copy.deepcopy(MOTION_SCHEMA); schema.pop('background',None)
    schema['objects'] = [{'id':'selected catalogue instance ID','tracks':schema['objects'][0]['tracks']}]
    schema.update(cycles=[{'id':'shared cycle ID','start_anchor':ANCHOR_SCHEMA,
        'end_anchor':ANCHOR_SCHEMA,'period_seconds':4}],
        animation_phases=[{'id':'phase ID','kind':'guided|loop|replay|hold','beat_ids':['current beat ID'],
            'start_anchor':ANCHOR_SCHEMA,'end_anchor':ANCHOR_SCHEMA,'cycle_id':None,
            'transition':'continuous|explained_reset','purpose':'teaching purpose'}],
        motion_requirements=[{'behavior':'capability key','object_ids':['selected ID'],
            'importance':'essential|optional','reason':'why needed'}],
        capability_gaps=[{'behavior':'unavailable behavior','object_ids':['selected ID'],
            'importance':'essential|optional','reason':'what cannot be implemented','lost_explanation':'what would be lost'}])
    return schema


def execute_beats(args, store, models, requirement, teaching, script, timing,
                  narration, canvases, components, word_spans, review_policy,
                  model_routes, placement_validator, motion_validator):
    """Return scoped compiled chapter plans or the existing explicit pause codes."""
    manifests = {orientation:build_manifest(teaching,timing,narration,canvas,components,
        fps=canvas['viewport']['fps'],word_spans=word_spans)
        for orientation,canvas in canvases.items()}
    # Existing creative inputs remain frozen. This namespace is operational.
    roots = {orientation:store.root/'executions'/'beat-v1'/manifest['source_hash'][:20]/orientation
             for orientation,manifest in manifests.items()}
    index = {'schema_version':EXECUTION_SCHEMA, 'execution_mode':'beats',
        'execution_roots':{key:str(path.resolve()) for key,path in roots.items()},
        'manifest_paths':{key:str((path/'manifest.json').resolve()) for key,path in roots.items()}}
    write(store.root/'beat-execution.json',index)
    for orientation,manifest in manifests.items():
        write(roots[orientation]/'manifest.json',manifest)
    all_assets = {}; chapters = {}
    feedback = read(store.root/'render-feedback.json') if (store.root/'render-feedback.json').exists() else {}
    bindings = read(args.bindings) if args.bindings else None
    if bindings is not None and (not isinstance(bindings,dict) or not set(bindings) <= {obj['id'] for obj in components['objects']}):
        raise ValueError('Bindings must map existing catalogue instance IDs')

    for orientation,canvas in canvases.items():
        manifest = manifests[orientation]; execution = roots[orientation]
        previous = None; compiled_beats = {}
        # Copy only an approved geometry checkpoint, never a rejected candidate.
        hint_path = execution/'approved-placement-hints.json'
        if not hint_path.exists():
            original = store.root/('placement-'+orientation+'-plan.json')
            write(hint_path,read(original) if original.exists() else {})
        hints = read(hint_path)
        for beat in manifest['beats']:
            local = Store(execution/(f"{beat['position']:02d}-"+beat['beat_id']))
            local_clock = beat_narration(manifest,beat['beat_id'],narration)
            selected_count = None; current_phase = 'selection'
            def progress(name,state,**extra):
                nonlocal current_phase
                current_phase = name
                details = {'orientation':orientation,'beat_id':beat['beat_id'],
                    'position':beat['position'],'total':beat['total'],'phase':name,
                    'state':state,**({'selected_objects':selected_count} if selected_count is not None else {}),**extra}
                write(execution/'progress.json',details); write(local.root/'progress.json',details)
                write(store.root/'state.json',{'state':'beat_'+name+'_'+state,'workflow':'infofilm.animation-workflow.v2',
                    'execution_mode':'beats','execution_root':str(execution.resolve()),'beat_progress':details,
                    'next':name,'accepted_outputs_retained':True})
                store.event('animation:beat',state,details)
                callback = getattr(args,'on_beat',None)
                if callback:callback(details)
            def plan(name,task,context,validator,tokens):
                progress(name,'running')
                value = phase(models,local,'beat-'+beat['beat_id']+'-'+name,task,
                    {**context,'workflow_contract':'infofilm.animation-workflow.v2',
                     'workflow_review_policy':review_policy,'model_routes':model_routes},
                    validator,tokens,args.max_attempts,reviewed=True)
                write(local.root/(name+'-plan.json'),value)
                progress(name,'succeeded')
                return value
            try:
                neighbors = [{'id':row['beat_id'],'message':row['teaching']['message'],
                    'demonstration':row['teaching']['demonstration']}
                    for row in manifest['beats'] if abs(row['position']-beat['position']) == 1]
                scoped_canvas = copy.deepcopy(canvas); scoped_canvas['teaching_sequence'] = [beat['teaching']]
                clock = {key:local_clock[key] for key in ('duration_seconds','words','transcript','clock')}
                context = {'learning_objective':_objective(requirement),'current_beat':beat['teaching'],
                    'neighbors':neighbors,'canvas_plan':scoped_canvas,'narration':clock,
                    'timing_cue':beat['timing']}
                catalogue = [{key:copy.deepcopy(obj[key]) for key in
                    ('id','kind','role','conveys','physics_role','required_capabilities','dependency_ids') if key in obj}
                    for obj in components['objects']]
                # Selection considers preceding instance identity, not mutable
                # terminal geometry. Geometry invalidates only dependent placement.
                preceding = {'beat_id':previous['beat_id'],'object_ids':list(previous['objects']),
                    'cycles':{key:{'period_seconds':row['period_seconds'],'object_ids':row['object_ids']}
                              for key,row in previous.get('cycles',{}).items()}} if previous else None
                selection = plan('selection','Select only the objects actively needed for this ONE teaching beat. '
                    'The catalogue includes alternatives across the chapter: do not put them all on screen. '
                    'Prefer one clear diagram, a few explanatory annotations and concise label cards. '
                    'Choose existing instance IDs; immutable SVGs will be resolved separately. '
                    'physics_role projectile forces a ballistic gravity-equation track: use it only for actual '
                    'free-flight under gravity in this beat. For constrained circular motion, schematic straight '
                    'inertia paths, setup or hold choose static; static objects may still move with keyed or linear '
                    'tracks. Never inherit a ballistic role merely because the drawing is a ball. '
                    'Declare a continuous transition only when shared visible objects genuinely retain artwork, '
                    'size, style and computed pose; otherwise use an explained_reset with a teaching reason. '
                    'A repeating mechanism needs cycle_carries to preserve its clock and complete dependency group. '
                    'component_requests must be empty: this migration uses the accepted catalogue.',
                    {**context,'object_catalogue':catalogue,'preceding_instances':preceding,'output_schema':SELECTION_SHAPE},
                    lambda value:selection_errors(value,beat,components,previous),3500)
                subset = component_subset(components,selection)
                selected_count = len(subset['objects'])
                write(local.root/'selected-components-plan.json',subset)
                binding_path = None
                if bindings is not None:
                    ids = {obj['id'] for obj in subset['objects']}
                    binding_path = write(local.root/'selected-bindings.json',{key:value for key,value in bindings.items() if key in ids})
                effective,assets,resolution = resolve_objects(local,subset,args.library,binding_path)
                # The compiler's resolved copy can contain both requested ID
                # and resolved asset ID. Cache readiness needs the resolver's
                # exclusive asset-or-request contract, with bindings preserved.
                readiness = copy.deepcopy(effective)
                for obj in readiness['objects']:
                    if obj.get('request_id'):obj['asset_id'] = None
                effective_path = write(local.root/'effective-components-plan.json',readiness)
                if not resolution['ready']:
                    request_path = write(store.root/'object-requests.json',read(local.root/'object-requests.json'))
                    progress('selection','waiting_for_components')
                    state = read(store.root/'state.json')
                    write(store.root/'state.json',{**state,'state':'waiting_for_components','requests':str(request_path),
                        'component_plan':str(effective_path.resolve()),'resume_from':'beat_selection'})
                    return {'status':3,'compiled_plans':chapters,'assets':all_assets}
                for identity,asset in assets.items():
                    if identity in all_assets and all_assets[identity]['sha256'] != asset['sha256']:
                        raise ValueError('Immutable asset changed between beats: '+identity)
                    all_assets[identity] = asset
                incoming = _incoming(previous,{**selection,'objects':effective['objects']})
                write(local.root/'entry-state.json',incoming)
                current_feedback = scoped_feedback(feedback.get(orientation),manifest,beat)
                geometry_hints = [obj for obj in hints.get('objects',[]) if obj['id'] in {o['id'] for o in effective['objects']}]
                schema = copy.deepcopy(PLACEMENT_SCHEMA)
                schema['objects'][0]['label_card'] = 'true for labels; false for headings or body text'
                placement_context = {**context,'accepted_components':effective,'resolved_assets':resolution['assets'],
                    'incoming_state':incoming,'transition':selection['transition'],
                    'approved_geometry_hints':geometry_hints,'output_schema':schema,
                    **({'render_feedback':current_feedback} if current_feedback else {})}
                placement = plan('placement','Place and style only the selected objects for this ONE beat. '
                    'Use the shared canvas zones as conventions, not simultaneous chapter-wide scenes. '
                    'x/y is the pivot; respect true SVG aspect ratio and landmarks. Keep objects in upper80%. '
                    'Set label_card true for concise labels; budget six-pixel padding inside text boxes. '
                    'For continuous objects use the incoming computed pose, size, artwork and styles exactly. '
                    'New/hidden objects may enter independently. Use approved geometry hints only when they '
                    'fit this beat and its incoming state. No tracks or inactive catalogue objects.',
                    placement_context,
                    lambda value:placement_validator(value,effective,assets,local_clock,scoped_canvas)+
                        placement_continuity_errors(previous,value,effective,assets,selection['transition'],scoped_canvas),6500)
                if args.until == 'placement':
                    return {'status':0,'stopped_at':'placement','compiled_plans':chapters,'assets':all_assets}
                scoped_teaching = {**teaching,'teaching_sequence':[beat['teaching']]}
                def validate_motion(value):
                    if not isinstance(value,dict):return ['Animation plan must be an object']
                    carried,carry_errors = apply_cycle_carries(value,previous,selection['transition'])
                    if carry_errors:return carry_errors
                    errors = motion_validator(carried,placement,scoped_canvas,effective,assets,local_clock,scoped_teaching)
                    # Never reference a catalogue object not actually selected.
                    if not errors:
                        _,bounds = compile_beat(value,placement,scoped_canvas,effective,assets,local_clock,
                            beat,previous,selection['transition'],manifest['fps'])
                        errors.extend(bounds)
                    return errors
                motion = plan('animation','Animate only this ONE accepted teaching beat with its local narration clock. '
                    'Original MFA word IDs are preserved but all supplied word times are already local; never subtract '
                    'a chapter offset. audio_start/end denote this beat window, including pauses. '
                    'Keep 3 to 8 useful checkpoints. Motion entries permit only id/tracks; placement is frozen. '
                    'Every selected object needs tracks, including static cards. Implement the beat required_behaviors '
                    'and its guided/loop/replay/hold mode. Use the repeat cue where supplied. End the demonstration '
                    'at the accepted spoken end cue, then hold its final picture through audio_end; continue a '
                    'mechanism through silence only when the teaching explicitly calls for it. Declare unavailable capabilities '
                    'explicitly. For cycle_carries code supplies phase_offset; use incoming pose as frozen placement. '
                    'Avoid gratuitous animation of labels. '+RULES,
                    {**context,'placement_plan':placement,'accepted_components':effective,'resolved_assets':resolution['assets'],
                        'incoming_state':incoming,'transition':selection['transition'],'renderer_capabilities':CAPABILITIES,
                        'renderer_rules':GENERAL_MOTION_RULES+'\n'+RULES,'output_schema':_motion_schema()},validate_motion,9000)
                gaps = [row for row in motion['capability_gaps'] if row['importance'] == 'essential']
                if gaps:
                    requests = write(store.root/'motion-capability-requests.json',{'requests':gaps,'orientation':orientation,'beat_id':beat['beat_id']})
                    progress('animation','waiting_for_motion_capabilities')
                    write(store.root/'state.json',{**read(store.root/'state.json'),'state':'waiting_for_motion_capabilities',
                        'requests':str(requests),'resume_from':'beat_animation'})
                    return {'status':4,'compiled_plans':chapters,'assets':all_assets}
                compiled,errors = compile_beat(motion,placement,scoped_canvas,effective,assets,local_clock,
                    beat,previous,selection['transition'],manifest['fps'])
                if errors:raise ValueError(str(errors))
                write(local.root/'compiled-plan.json',compiled)
                receipt = dependency_receipt(beat,canvas=scoped_canvas,components=effective,assets=assets,narration=local_clock,
                    selection=selection,placement=placement,motion=motion,incoming_state=previous)
                write(local.root/'continuity-receipt.json',{'transition':selection['transition'],'errors':[],
                    'dependency_hashes':receipt,'entry_state_sha256':digest(incoming)})
                previous = terminal_state(compiled,assets,beat['beat_id'])
                write(local.root/'terminal-state.json',previous)
                write(local.root/'state.json',{'state':'animation_ready','beat_id':beat['beat_id'],
                    'compiled_sha256':digest(compiled),'dependency_hashes':receipt})
                compiled_beats[beat['beat_id']] = compiled
                progress('animation','succeeded',compiled_sha256=digest(compiled))
            except Exception as exc:
                local_state = read(local.root/'state.json') if (local.root/'state.json').exists() else {}
                state = read(store.root/'state.json')
                write(store.root/'state.json',{**state,'state':local_state.get('state','planning_needs_review'),
                    'error':str(exc),'resume_from':'beat_'+current_phase,'phase_checkpoint':str(local.root.resolve()),
                    'accepted_outputs_retained':True})
                raise
            finally:
                local.db.close()
        chapter = assemble_chapter(manifest,compiled_beats,narration)
        chapters[orientation] = chapter
        write(store.root/'renders'/orientation/'compiled-plan.json',chapter)
    return {'status':0,'compiled_plans':chapters,'assets':all_assets}
