"""Chapter handoff → teaching → narration → speech/MFA → visuals → Remotion.

Accepted phases are immutable. Missing SVGs stop after audio, and --resume
resolves them against the updated library without paying to replan earlier stages.
"""
import base64
import copy
import json
import math
import subprocess
from pathlib import Path

from .animation_experiment import (ANCHOR_SCHEMA, COMPONENT_SCHEMA, MOTION_SCHEMA, MOTION_RULES,
    anchor_time, component_errors, phase, render_reusable, load_narration, finite)
from .animation_frontier import (input_requirement, canvas_errors, placement_errors,
    merge_animation, resolve_objects, publish_report, usage_summary, CANVAS_SCHEMA, PLACEMENT_SCHEMA)
from .animation_frontier import GENERAL_MOTION_RULES
from .animation_motion import CAPABILITIES, RULES, compile_motion_v2, transforms, dependencies, capability_key
from .animation_narration import generate_script, prepare_audio
from .animation_objects import catalog
from .animation_openai import OpenAIAnimationModels
from .core import ROOT, Store, configuration, digest, read, write
from .llm import Models

CONTRACT='infofilm.animation-workflow.v2'
STAGES=['teaching','narration','audio','timing','canvas','objects','placement','animation','render']
REVIEW_POLICY=(
    'Review concrete semantic defects, factual/causal meaning, useful visual demonstrations, scope and chapter continuity. '
    'Runtime is advisory. Missing SVGs are legitimate requests. A capability gap must be explicit, not disguised as '
    'a schematic fallback. Guided demonstrations should accompany their explanation; repeating mechanisms may enter '
    'a synchronized loop during recap. One-time events need a justified replay or hold, not automatic looping. '
    'Do not require cosmetic rearrangement or demand unprovided research. Review only the current phase; do not '
    'demand coordinates before canvas/placement or timestamps before speech/MFA.')
TEACHING_SCHEMA={'schema_version':'infofilm.animation-teaching.v2','requirement_id':'copy input id',
    'visual_message':'copy visual_message','explanation_strategy':'how the explanation develops; no canvas coordinates',
    'assumptions':['explicit teaching simplifications'],
    'teaching_sequence':[{'id':'safe beat ID','message':'learning point','demonstration':'visible cause and effect',
        'demonstration_mode':'guided|guided_then_loop|guided_then_replay|hold',
        'required_behaviors':['behavior from renderer capabilities, or explicit unavailable behavior'],
        'repeat_purpose':'why repetition helps, or null'}]}
TIMING_SCHEMA={'schema_version':'infofilm.animation-timing-cues.v2',
    'beats':[{'beat_id':'accepted beat ID','start_anchor':ANCHOR_SCHEMA,'end_anchor':ANCHOR_SCHEMA,
        'repeat_anchor':'null or an actual MFA anchor','repeat_reason':'null or words explaining this transition'}]}


class WorkflowModels:
    """Luna plans and independently reviews; an optional local mapper only finds word anchors."""
    def __init__(self,config,store,*,schema_repair=True,max_output_recovery_tokens=64000,animation_output_tokens=48000):
        recovery={'max_output_recovery_tokens':max_output_recovery_tokens,'animation_output_tokens':animation_output_tokens}
        self.config=config;self.planner=OpenAIAnimationModels(config,store,**recovery)
        self.reviewer=OpenAIAnimationModels({**config,'planner_model':config['reviewer_model'],
            'planner_url':config['reviewer_url'],'role':'reviewer'},store,**recovery)
        self.local=Models({'reviewer_url':config['continuity_url']},store)
        from .animation_schema_repair import LocalSchemaRepair
        self.schema_repairer=LocalSchemaRepair(self.local,store) if schema_repair else None

    def call(self,role,*args,**kwargs):
        return (self.planner if role=='planner' else self.reviewer).call(role,*args,**kwargs)

    call_json=Models.call_json

    def timing_mapper(self):
        parent=self
        class Mapper:
            config=parent.config
            def call(self,role,*args,**kwargs):
                if role=='planner' and parent.config['continuity_model']=='local':
                    return parent.local.call('reviewer',*args,**kwargs)
                return parent.call(role,*args,**kwargs)
            call_json=Models.call_json
        return Mapper()


def teaching_errors(value,requirement):
    if not isinstance(value,dict):return ['Teaching plan must be an object']
    errors=[]
    for key,want in [('schema_version',TEACHING_SCHEMA['schema_version']),('requirement_id',requirement['id']),
                     ('visual_message',requirement['visual_message'])]:
        if value.get(key)!=want:errors.append(key+' must match the chapter input')
    if not isinstance(value.get('explanation_strategy'),str) or not value['explanation_strategy'].strip():errors.append('Teaching needs an explanation strategy')
    rows=value.get('teaching_sequence');ids=set()
    if not isinstance(rows,list) or not rows:return errors+['Teaching needs nonempty beats']
    import re
    for row in rows:
        if not isinstance(row,dict):errors.append('Each beat must be an object');continue
        identity=row.get('id')
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',identity) or identity in ids:errors.append('Beats need unique safe IDs')
        else:ids.add(identity)
        for key in ('message','demonstration'):
            if not isinstance(row.get(key),str) or not row[key].strip():errors.append('Beat needs '+key)
        mode=row.get('demonstration_mode')
        if mode not in {'guided','guided_then_loop','guided_then_replay','hold'}:errors.append('Unknown demonstration mode')
        behaviors=row.get('required_behaviors')
        if not isinstance(behaviors,list) or any(not isinstance(b,str) or not b.strip() for b in behaviors) or len(set(behaviors))!=len(behaviors):errors.append('required_behaviors must be unique strings')
        if mode in {'guided_then_loop','guided_then_replay'} and (not isinstance(row.get('repeat_purpose'),str) or not row['repeat_purpose'].strip()):errors.append('Repetition needs a teaching purpose')
    return errors


def beat_word_spans(script,narration):
    """Map accepted paragraph boundaries into exact MFA words; no new transcription."""
    spans={};cursor=0
    for point in script['points']:
        count=len(point['narration'].split());words=narration['words'][cursor:cursor+count]
        if [w['word'] for w in words]!=point['narration'].split():raise ValueError('Speech bundle differs from accepted narration')
        for beat in point['beat_ids']:
            spans.setdefault(beat,[]).extend(words)
        cursor+=count
    if cursor!=len(narration['words']):raise ValueError('Narration paragraph boundaries do not cover MFA')
    return spans


def timing_errors(value,teaching,spans,narration):
    if not isinstance(value,dict) or value.get('schema_version')!=TIMING_SCHEMA['schema_version'] or not isinstance(value.get('beats'),list):return ['Invalid timing-cues contract']
    expected={b['id']:b for b in teaching['teaching_sequence']};seen=set();errors=[]
    for row in value['beats']:
        try:
            identity=row['beat_id']
            if identity not in expected or identity in seen:raise ValueError('Timing needs each accepted beat exactly once')
            seen.add(identity);words=spans[identity];lo,hi=words[0]['start'],words[-1]['end']
            start,end=anchor_time(row['start_anchor'],narration),anchor_time(row['end_anchor'],narration)
            if not lo-1e-6<=start<end<=hi+1e-6:raise ValueError('Beat anchors must remain inside its narrated paragraph span')
            repeat=row.get('repeat_anchor')
            mode=expected[identity]['demonstration_mode']
            if repeat:
                when=anchor_time(repeat,narration)
                if mode not in {'guided_then_loop','guided_then_replay'} or not start<when<end:raise ValueError('Repeat needs an approved teaching mode and an interior narration anchor')
                if not isinstance(row.get('repeat_reason'),str) or not row['repeat_reason'].strip():raise ValueError('Repeat anchor needs an explanation')
            elif mode in {'guided_then_loop','guided_then_replay'}:raise ValueError('Approved repetition needs a repeat anchor')
        except (KeyError,TypeError,ValueError) as exc:errors.append(str(exc))
    if seen!=set(expected):errors.append('Timing must cover every teaching beat')
    return errors


def mapper_word_spans(spans):
    # The mapper selects IDs, not numerical timestamps. Exact times remain in
    # the verified MFA bundle for code validation and detailed motion planning.
    return {beat:{'first_word_id':words[0]['id'],'last_word_id':words[-1]['id'],
                  'words':[[w['id'],w['word']] for w in words]} for beat,words in spans.items()}


def motion_errors(value,placement,canvas,components,assets,narration,teaching):
    if not isinstance(value,dict):return ['Animation plan must be an object']
    objects=value.get('objects')
    if not isinstance(objects,list):return ['Animation objects must be a list']
    errors=[]
    for obj in objects:
        if not isinstance(obj,dict) or set(obj)-{'id','tracks'}:errors.append('Motion entries permit only id and tracks; placement is frozen')
    for key in ('cycles','animation_phases','motion_requirements','capability_gaps'):
        if not isinstance(value.get(key),list):errors.append(key+' must be a list (empty when unused)')
    if errors:return errors
    draft=merge_animation(value,placement,canvas)
    compiled,bounds=compile_motion_v2(draft,components,assets,narration,canvas['viewport']['width'],canvas['viewport']['height'])
    errors.extend(bounds)
    ids={o['id'] for o in components['objects']};beats={b['id'] for b in teaching['teaching_sequence']}
    phases=value['animation_phases'];seen=set();prior=0;covered=set()
    try:
        cycles={c['id']:c for c in compiled.get('cycles',[])} if compiled else {}
        for row in phases:
            if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id'] or row['id'] in seen:raise ValueError('Animation phases need unique IDs')
            seen.add(row['id']);kind=row.get('kind');refs=row.get('beat_ids')
            if kind not in {'guided','loop','replay','hold'} or not isinstance(refs,list) or not refs or not set(refs)<=beats:raise ValueError('Phase needs known teaching beats and kind')
            covered.update(refs)
            start,end=anchor_time(row['start_anchor'],narration),anchor_time(row['end_anchor'],narration)
            if not start<end or start<prior-1e-6:raise ValueError('Teaching phases reverse or overlap')
            prior=end
            if row.get('transition') not in {'continuous','explained_reset'} or not isinstance(row.get('purpose'),str) or not row['purpose'].strip():raise ValueError('Phase needs transition policy and teaching purpose')
            if kind=='loop':
                c=cycles.get(row.get('cycle_id'))
                if not c or abs(c['start']-start)>1e-6 or abs(c['end']-end)>1e-6:raise ValueError('Loop phase must match its shared cycle window')
            elif row.get('cycle_id') is not None:raise ValueError('Only loop phases reference a cycle')
            if not bounds and start>0 and row['transition']=='continuous':
                before=transforms(compiled,max(0,start-1e-7),assets);after=transforms(compiled,start,assets)
                for identity in ids:
                    a,b=before[identity],after[identity]
                    if a['opacity']>.001 and b['opacity']>.001 and (math.hypot((a['x']-b['x'])*compiled['width'],(a['y']-b['y'])*compiled['height'])>1 or abs((a['rotation']-b['rotation']+180)%360-180)>.1):raise ValueError(identity+' jumps at a continuous phase transition')
        if covered!=beats:errors.append('Animation phases must cover every teaching beat')
        for beat in teaching['teaching_sequence']:
            kinds={p.get('kind') for p in phases if beat['id'] in p.get('beat_ids',[])}
            if beat['demonstration_mode']=='guided_then_loop' and not {'guided','loop'}<=kinds:errors.append(beat['id']+' needs its accepted guided demonstration followed by a loop')
            if beat['demonstration_mode']=='guided_then_replay' and not {'guided','replay'}<=kinds:errors.append(beat['id']+' needs its accepted guided demonstration followed by replay')
        for gap in value['capability_gaps']:
            if not isinstance(gap,dict) or gap.get('importance') not in {'essential','optional'} or not isinstance(gap.get('behavior'),str) or not isinstance(gap.get('object_ids'),list) or not set(gap['object_ids'])<=ids or not all(isinstance(gap.get(k),str) and gap[k].strip() for k in ('reason','lost_explanation')):errors.append('Invalid structured capability gap')
        tracks={o['id']:o['tracks'] for o in objects}
        linked_ids={identity for identity,ts in tracks.items() if any(t.get('kind') in {'pin_link','slider_crank'} for t in ts)}
        linked_ids.update(ref for ts in tracks.values() for t in ts for ref in dependencies(t))
        required={capability_key(b) for beat in teaching['teaching_sequence'] for b in beat['required_behaviors']}
        declared={capability_key(r.get('behavior')) for r in value['motion_requirements'] if isinstance(r,dict) and r.get('importance')=='essential'}
        declared.update(capability_key(g.get('behavior')) for g in value['capability_gaps'] if isinstance(g,dict) and g.get('importance')=='essential')
        if required-declared:errors.append('Accepted teaching behaviors must be implemented or explicitly faulted: '+', '.join(sorted(required-declared)))
        for req in value['motion_requirements']:
            if not isinstance(req,dict) or not isinstance(req.get('behavior'),str) or req.get('importance') not in {'essential','optional'} or not isinstance(req.get('object_ids'),list) or not req['object_ids'] or not set(req['object_ids'])<=ids or not isinstance(req.get('reason'),str) or not req['reason'].strip():errors.append('Invalid motion requirement');continue
            behavior=capability_key(req['behavior'])
            if behavior not in CAPABILITIES:
                if not any(capability_key(g.get('behavior'))==behavior and g.get('importance')==req['importance'] for g in value['capability_gaps']):errors.append('Unsupported behavior needs an explicit capability gap: '+behavior)
                continue
            if any(capability_key(g.get('behavior'))==behavior and g.get('importance')=='essential' for g in value['capability_gaps']):continue
            def implemented(ts):
                if behavior=='continuous_rotation':return any(('rotation_from' in t and t['rotation_from']!=t.get('rotation_to')) or (t['kind']=='keyframes' and len({f['rotation'] for f in t['keyframes']})>1) or t['kind']=='pin_link' for t in ts)
                if behavior=='synchronized_cycle':return any(t.get('cycle_id') for t in ts)
                if behavior=='linked_joints':return any(t['kind'] in {'pin_link','slider_crank'} for t in ts)
                if behavior in {'slider_crank','pin_link'}:return any(t['kind']==behavior for t in ts)
                if behavior=='projectile_motion':return any(t['kind']=='projectile' for t in ts)
                if behavior=='translation':return any(t['kind'] in {'linear','projectile','keyframes','slider_crank','pin_link'} for t in ts)
                if behavior in {'keyframes','linear'}:return any(t['kind']==behavior for t in ts)
                return True
            present=(all(i in linked_ids for i in req['object_ids']) if behavior=='linked_joints'
                     else all(implemented(tracks[i]) for i in req['object_ids']))
            if req['importance']=='essential' and not present:errors.append('Essential declared behavior has no implementation: '+behavior)
            if behavior=='synchronized_cycle':
                shared=set.intersection(*[{t.get('cycle_id') for t in tracks[i] if t.get('cycle_id')} for i in req['object_ids']])
                if not shared:errors.append('Cycle participants must share a cycle clock')
        if not bounds:
            placed={o['id']:o for o in placement['objects']}
            for obj in compiled['objects']:
                pose=transforms(compiled,obj['tracks'][0]['start'],assets)[obj['id']];p=placed[obj['id']]
                if math.hypot((pose['x']-p['x'])*compiled['width'],(pose['y']-p['y'])*compiled['height'])>1 or abs((pose['rotation']-p.get('rotation',0)+180)%360-180)>.1:errors.append(obj['id']+' must begin at its frozen placement')
    except (KeyError,TypeError,ValueError) as exc:errors.append(str(exc))
    return errors


def placement_plan_errors(value,components,assets,narration,canvas):
    errors=placement_errors(value,components,assets,narration,canvas)
    text_ids={o['id'] for o in components['objects'] if o['kind']=='text'}
    for obj in value.get('objects',[]) if isinstance(value,dict) and isinstance(value.get('objects'),list) else []:
        if not isinstance(obj,dict):continue
        if obj.get('id') in text_ids and not isinstance(obj.get('label_card'),bool):errors.append(str(obj.get('id'))+' text needs explicit label_card boolean (false only for heading/body text)')
        elif 'label_card' in obj and not isinstance(obj['label_card'],bool):errors.append(str(obj.get('id'))+' label_card must be boolean')
    return errors


class RendererLayoutError(RuntimeError):
    def __init__(self,orientation,feedback):
        self.orientation,self.feedback=orientation,feedback
        super().__init__('Rendered layout needs repair: '+orientation)


def resume_speech_configuration(root, candidate, previous):
    """Retain the verified speech config when only execution mode changed."""
    if previous is None:
        return copy.deepcopy(candidate)
    path=Path(root)/'speech-config.json'
    if not path.exists():raise ValueError('Frozen speech configuration checkpoint is missing')
    saved=read(path)
    if digest(saved)!=previous.get('speech_config_sha256'):
        raise ValueError('Frozen speech configuration checkpoint changed')
    def content(config):
        value=copy.deepcopy(config)
        provider=value.get('animation_provider')
        if isinstance(provider,dict):provider.pop('execution_mode',None)
        return value
    return copy.deepcopy(saved if content(saved)==content(candidate) else candidate)


def run_once(args):
    store=Store(args.output);narration=None;orientations=['landscape','portrait'] if args.orientation=='both' else [args.orientation]
    try:
        requirement=input_requirement(args)
        path=store.root/'inputs.json';previous=read(path) if path.exists() else None
        if args.resume and previous is None:raise ValueError('--resume requires an existing workflow')
        speech_config=resume_speech_configuration(store.root,configuration(args.config),previous)
        if requirement.get('schema_version')!='infofilm.animation-requirement.v1' or any(not isinstance(requirement.get(k),str) or not requirement[k].strip() for k in ('id','subject','visual_message','scene_requirement')):raise ValueError('Expected a valid animation chapter learning objective')
        models_config={'planner_url':'https://api.openai.com/v1','reviewer_url':'https://api.openai.com/v1',
            'planner_model':args.model,'reviewer_model':args.reviewer_model or args.model,
            'reasoning_effort':args.reasoning_effort,'max_output_tokens':args.max_output_tokens,
            'continuity_model':args.continuity_model,'continuity_url':speech_config['reviewer_url']}
        frozen={'contract':CONTRACT,'requirement':requirement,'model':models_config,'orientations':orientations,
            'review_mode':'semantic','review_policy':REVIEW_POLICY,'speech_config_sha256':digest(speech_config),
            'library':str(args.library.resolve())}
        if previous is not None and previous!=frozen:raise ValueError('Frozen inputs changed; use a new output directory')
        if previous is None:write(path,frozen)
        write(store.root/'requirement.json',requirement)
        if requirement.get('chapter_context'):write(store.root/'chapter-context.json',requirement['chapter_context'])
        config_path=store.root/'speech-config.json'
        if previous is None:write(config_path,speech_config)
        capabilities_path=store.root/'teaching-capabilities.json'
        if not capabilities_path.exists():write(capabilities_path,CAPABILITIES)
        # Operational recovery does not alter frozen creative/model inputs.
        models=WorkflowModels(models_config,store,schema_repair=getattr(args,'local_schema_repair',True),
            max_output_recovery_tokens=getattr(args,'max_output_recovery_tokens',64000),
            animation_output_tokens=getattr(args,'animation_output_tokens',48000))
        def checkpoint(stage):
            previous=read(store.root/'state.json') if (store.root/'state.json').exists() else {}
            execution={key:previous[key] for key in ('execution_mode','execution_root','beat_progress') if key in previous}
            write(store.root/'state.json',{**execution,'state':stage+'_ready','workflow':CONTRACT,'review_mode':'semantic',
                'human_review':'pending','next':STAGES[STAGES.index(stage)+1] if stage!='render' else 'human_visual_and_listening_review'})
            store.event('animation:workflow','stage_complete',{'stage':stage,'position':STAGES.index(stage)+1,'total':len(STAGES)})
            callback=getattr(args,'on_stage',None)
            if callback:callback(stage)
            return args.until==stage
        def plan(name,task,ctx,validate,tokens=6500,mapper=None):
            return phase(mapper or models,store,name,task,{**ctx,'workflow_contract':CONTRACT,
                'workflow_review_policy':REVIEW_POLICY,'model_routes':models_config},validate,tokens,args.max_attempts,reviewed=True)
        teaching=plan('teaching','Plan the teaching sequence and visual demonstrations for the supplied chapter. '
            'Keep objective, scope, grounding, exclusions and entry/exit context. Describe learning points and visible '
            'cause/effect. Choose guided, guided-then-loop, guided-then-replay or hold per beat. Plan a repeat only when '
            'it teaches something and include the narration transition it will need. Do not plan a canvas, coordinates, '
            'asset instances, timestamps or narration yet. Declare required motion behaviors; expose unavailable capabilities.',
            {'learning_objective':requirement,'renderer_capabilities':read(capabilities_path),'output_schema':TEACHING_SCHEMA},
            lambda v:teaching_errors(v,requirement),5000)
        if checkpoint('teaching'):return 0
        paragraph_policy=store.root/'narration-paragraph-policy.json'
        if not paragraph_policy.exists():
            write(paragraph_policy,{'single_beat_paragraphs':getattr(args,'execution_mode','chapter')=='beats'
                and not (store.root/'narration-script-plan.json').exists()})
        script=generate_script(store,models=models,teaching=teaching,max_attempts=args.max_attempts,reviewed=True,
            single_beat_paragraphs=read(paragraph_policy)['single_beat_paragraphs'])
        if checkpoint('narration'):return 0
        owner=getattr(args,'queue_owner_project',None)
        narration=prepare_audio(store,config_path,**({'owner_project':owner} if owner else {}))
        if checkpoint('audio'):return 0
        spans=beat_word_spans(script,narration)
        timing=plan('timing-cues','Your narrow job is to locate explanation and repeat transitions in the supplied '
            'MFA word spans. Return exact beat anchors inside each beat\'s narrated paragraphs. For an accepted '
            'guided_then_loop/replay beat, choose the word that announces the repeated/continuous demonstration. '
            'Words are compact [word_id,spoken_token] pairs; exact numerical timing is owned by the verified MFA map. '
            'Do not rewrite speech, change the teaching sequence, infer new mechanics or create motion. Code owns '
            'span validation; semantic review checks that anchors correspond to what is actually being explained.',
            {'teaching_sequence':teaching['teaching_sequence'],'script':script,'beat_words':mapper_word_spans(spans),'output_schema':TIMING_SCHEMA},
            lambda v:timing_errors(v,teaching,spans,narration),3500,models.timing_mapper())
        if checkpoint('timing'):return 0
        clock={'transcript':narration['transcript'],'duration_seconds':narration['duration_seconds'],'words':narration['words']}
        context={'learning_objective':requirement,'teaching_plan':teaching,'narration_script':script,'narration':clock,'timing_cues':timing}
        canvases={}
        for orientation in orientations:
            viewport={'width':1280 if orientation=='landscape' else 720,'height':720 if orientation=='landscape' else 1280,'fps':30}
            schema=copy.deepcopy(CANVAS_SCHEMA)
            canvases[orientation]=plan('canvas-'+orientation,'Plan the detailed canvas for the accepted teaching plan '
                'and real narration clock. Preserve teaching_sequence exactly; do not replan accepted explanations or speech. '
                'Choose zones, palette and layout that support the guided demonstrations and planned repetitions. '
                'Reserve bottom 20% for captions. Do not select object instances or their coordinates yet.',
                {**context,'viewport':viewport,'output_schema':schema},
                lambda v:canvas_errors(v,requirement,viewport)+([] if isinstance(v,dict) and v.get('teaching_sequence')==teaching['teaching_sequence'] else ['Canvas must preserve accepted teaching_sequence']),5000)
        if checkpoint('canvas'):return 0
        catalog_path=store.root/'initial-library-catalog.json'
        if not catalog_path.exists():write(catalog_path,catalog(args.library))
        components=plan('components','Select all object instances for the accepted canvases and teaching demonstrations. '
            'Reuse available SVGs when capabilities fit. Text, rect and line are native. Text labels can be rendered '
            'as code-drawn cards, so do not request separate label-card SVGs/background objects. Request missing SVGs '
            'explicitly with immutable versioned identity, pivot, landmarks and required capabilities. All objects, '
            'including native ones, need required_capabilities (empty is valid). Do not place instances or plan tracks.',
            {**context,'canvas_plans':canvases,'component_library':read(catalog_path),'output_schema':COMPONENT_SCHEMA},
            lambda v:component_errors(v,requirement))
        execution_mode=getattr(args,'execution_mode','chapter')
        if execution_mode not in {'beats','chapter'}:raise ValueError('Unknown animation execution mode')
        if execution_mode=='beats' and args.until!='objects':
            from .animation_beat_workflow import execute_beats
            result=execute_beats(args,store,models,requirement,teaching,script,timing,narration,
                canvases,components,spans,REVIEW_POLICY,models_config,placement_plan_errors,motion_errors)
            if result['status']:return result['status']
            if result.get('stopped_at'):
                checkpoint(result['stopped_at']);return 0
            compiled_plans=result['compiled_plans'];assets=result['assets']
        else:
            effective,assets,resolution=resolve_objects(store,components,args.library,args.bindings)
            if not resolution['ready']:return 3
            if checkpoint('objects'):return 0
            # Phase cache includes immutable resolved asset metadata so replenishing the library cannot rewrite teaching/audio.
            placements={}
            render_feedback=read(store.root/'render-feedback.json') if (store.root/'render-feedback.json').exists() else {}
            for orientation in orientations:
                canvas=canvases[orientation];viewport=canvas['viewport'];schema=copy.deepcopy(PLACEMENT_SCHEMA)
                schema['objects'][0]['label_card']='true for text labels, false for headings or plain body text'
                placement=plan('placement-'+orientation,'Place and style every accepted object, using its true SVG proportions, '
                    'pivot and landmarks. x/y is the pivot position. Plan readable labels with label_card:true: code will draw '
                    'a background, border, rounded corners and six-pixel padding inside the box. Budget for padding when sizing '
                    'text boxes. Use label_card:false for headings/body text. Native text card backgrounds need no extra object. '
                    'Make mechanical connection points and support relationships explicit. Keep complete objects in the upper '
                    '80%. Establish an initial state suitable for entering guided motion smoothly; no motion tracks yet.',
                    {**context,'canvas_plan':canvas,'accepted_components':effective,'resolved_assets':resolution['assets'],
                        'render_feedback':render_feedback.get(orientation),'output_schema':schema},
                    lambda v:placement_plan_errors(v,effective,assets,narration,canvas))
                placements[orientation]=placement
            if checkpoint('placement'):return 0
            compiled_plans={}
            for orientation in orientations:
                canvas=canvases[orientation];viewport=canvas['viewport'];placement=placements[orientation]
                schema=copy.deepcopy(MOTION_SCHEMA);schema.pop('background',None)
                schema['objects']=[{'id':'accepted instance ID','tracks':schema['objects'][0]['tracks']}]
                schema.update(cycles=[{'id':'shared cycle ID','start_anchor':ANCHOR_SCHEMA,'end_anchor':ANCHOR_SCHEMA,'period_seconds':4}],
                    animation_phases=[{'id':'phase ID','kind':'guided|loop|replay|hold','beat_ids':['beat ID'],
                        'start_anchor':ANCHOR_SCHEMA,'end_anchor':ANCHOR_SCHEMA,'cycle_id':None,
                        'transition':'continuous|explained_reset','purpose':'teaching purpose'}],
                    motion_requirements=[{'behavior':'capability key','object_ids':['instance ID'],'importance':'essential|optional','reason':'why needed'}],
                    capability_gaps=[{'behavior':'unavailable behavior','object_ids':['instance ID'],'importance':'essential|optional',
                        'reason':'what cannot be implemented','lost_explanation':'what would be lost'}])
                animation=plan('animation-'+orientation,'Plan guided demonstrations anchored to the actual explanation words, '
                    'then synchronized loops/replays where the accepted sequence calls for them. Every accepted beat must '
                    'appear in animation_phases. Motion entries have only id and tracks; placement sizes, labels and assets '
                    'are frozen. Use continuous_rotation and linked_joints where essential; do not freeze a rod/crank while '
                    'claiming the mechanism is coordinated. Declare and implement motion_requirements, and expose capability '
                    'gaps. Native cards and headings can remain static. ' + RULES,
                    {**context,'canvas_plan':canvas,'placement_plan':placement,'accepted_components':effective,
                        'resolved_assets':resolution['assets'],'renderer_capabilities':CAPABILITIES,
                        'renderer_rules':GENERAL_MOTION_RULES+'\n'+RULES,'output_schema':schema},
                    lambda v:motion_errors(v,placement,canvas,effective,assets,narration,teaching),12000)
                gaps=[g for g in animation['capability_gaps'] if g['importance']=='essential']
                if gaps:
                    write(store.root/'motion-capability-requests.json',{'requests':gaps,'orientation':orientation})
                    write(store.root/'state.json',{'state':'waiting_for_motion_capabilities','resume_from':'animation-'+orientation,
                        'requests':'motion-capability-requests.json','review_mode':'semantic'})
                    store.event('animation:motion','waiting_for_capabilities',{'count':len(gaps)});return 4
                draft=merge_animation(animation,placement,canvas);write(store.root/('motion-'+orientation+'-plan.json'),draft)
                compiled,errors=compile_motion_v2(draft,effective,assets,narration,viewport['width'],viewport['height'])
                if errors:raise ValueError(str(errors))
                folder=store.root/'renders'/orientation;write(folder/'compiled-plan.json',compiled)
                compiled_plans[orientation]=compiled
        if checkpoint('animation'):return 0
        for orientation in orientations:
            compiled=compiled_plans[orientation];folder=store.root/'renders'/orientation
            props={'plan':compiled,'assets':assets,'audio_src':'data:audio/wav;base64,'+base64.b64encode(Path(narration['audio']).read_bytes()).decode()}
            props_path=write(folder/'props.json',props)
            if not render_reusable(folder,props_path):
                store.event('animation:render','running',{'orientation':orientation,'frames':round(compiled['duration_seconds']*30)})
                with (folder/'render.log').open('w') as log:
                    command=['node',str(ROOT/'renderer/animation-experiment.cjs'),str(folder),speech_config['browser'],str(props_path),str(getattr(args,'render_concurrency',6))]
                    with subprocess.Popen(command,cwd=ROOT/'renderer',stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True) as child:
                        for line in child.stdout:
                            log.write(line)
                            if line.startswith('{"state"'):print('[animation:render:'+orientation+'] '+line.strip(),flush=True)
                        if child.wait():
                            receipt=folder/'render-results.json'
                            if receipt.exists():
                                result=read(receipt)
                                failures=(result.get('video') or {}).get('layout_failures',[])
                                if failures and 'Animation layout needs review' in str(result.get('error')):
                                    examples=[];seen=set()
                                    for row in failures:
                                        beat_id=row.get('beat_id')
                                        if not beat_id:
                                            beat_id=next((beat['beat_id'] for beat in compiled.get('beats',[])
                                                if beat['start_frame']<=row['frame']<beat['start_frame']+beat['duration_frames']),None)
                                        for reason in row.get('failures',[]):
                                            key=(beat_id,reason)
                                            if key not in seen:
                                                examples.append({'frame':row['frame'],'problem':reason,**({'beat_id':beat_id} if beat_id else {})})
                                                seen.add(key)
                                    raise RendererLayoutError(orientation,{'instruction':'Repair these actual rendered layout defects. Preserve teaching, narration and correct visual decisions.',
                                        'examples':examples[:30],'affected_frames':len(failures),'receipt_sha256':digest(result)})
                            raise subprocess.CalledProcessError(child.returncode,command)
            store.event('animation:render','succeeded',{'orientation':orientation})
        checkpoint('render');return 0
    except Exception as exc:
        previous=read(store.root/'state.json') if (store.root/'state.json').exists() else {}
        if previous.get('state')!='planning_needs_review':
            write(store.root/'state.json',{**previous,'state':'failed','error':str(exc),'previous_state':previous,
                'resume_from':previous.get('next'),'accepted_outputs_retained':True})
        store.event('animation:workflow','failed',{'error':str(exc)});raise
    finally:
        usage_summary(store)
        if (store.root/'requirement.json').exists():publish_report(store,read(store.root/'requirement.json'),narration,orientations)
        store.db.close()


def run(args):
    """Retry actual text/layout failures by replanning only placement and its dependent motion."""
    for attempt in range(getattr(args,'max_render_repairs',2)+1):
        try:return run_once(args)
        except RendererLayoutError as error:
            root=Path(args.output);history_path=root/'render-repairs.json'
            history=read(history_path) if history_path.exists() else []
            history.append({'orientation':error.orientation,'feedback':error.feedback,'attempt':attempt+1})
            write(history_path,history)
            if attempt>=getattr(args,'max_render_repairs',2):raise
            path=root/'render-feedback.json';feedback=read(path) if path.exists() else {}
            feedback[error.orientation]=error.feedback;write(path,feedback)
            print('[animation:render] layout_repair: '+json.dumps({'attempt':attempt+1,'orientation':error.orientation,'upstream_plans_and_audio_retained':True}),flush=True)
