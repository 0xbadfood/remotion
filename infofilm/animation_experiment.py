"""Resumable local-model animation planning, isolated from production films."""
from __future__ import annotations

import argparse
import base64
import copy
import html
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

from .core import ROOT, Store, configuration, digest, read, write
from .llm import Models, ModelOutputError

CONTRACT = 'infofilm.animation-experiment.v1'
COMPONENT_SCHEMA = {
    'schema_version': 'infofilm.animation-components.v1', 'requirement_id': 'copy input id',
    'scene_id': 'copy input id', 'visual_message': 'copy input visual_message verbatim',
    'teaching_strategy': 'explain your sequence and why it teaches the message',
    'assumptions': ['explicit simplifications'],
    'objects': [{'id': 'unique safe instance ID', 'kind': 'svg|text|rect|line',
        'role': 'semantic role', 'physics_role': 'static|projectile|annotation',
        'conveys': 'why this object is necessary', 'asset_id': 'available SVG ID or null',
        'request_id': 'component_requests ID or null', 'required_capabilities': ['needed capabilities']}],
    'component_requests': [{'id': 'unique request ID', 'requested_asset_id': 'new versioned asset ID',
        'description': 'desired visible object', 'required_capabilities': ['capability'],
        'svg_requirements': {'view': 'view direction', 'pivot': [0.5, 0.5], 'semantic_landmarks': ['visible parts']}}]
}
ANCHOR_SCHEMA = {'word_id': 'actual MFA word ID, audio_start, or audio_end', 'edge': 'start|end', 'offset_seconds': 0}
MOTION_SCHEMA = {
    'schema_version': 'infofilm.animation-motion-draft.v1', 'scene_id': 'copy input id',
    'visual_message': 'copy input visual_message verbatim', 'background': '#071b29',
    'explanation': 'why the arrangement and action sequence communicate the message',
    'objects': [{'id': 'accepted component instance ID', 'width': 'frame fraction 0..1',
        'height': 'frame fraction 0..1', 'color': '#hex when applicable', 'font_size': 'pixels for text',
        'text': 'short visual label for text objects', 'z_index': 0,
        'tracks': [{'kind': 'hold|linear|projectile', 'start_anchor': ANCHOR_SCHEMA,
            'end_anchor': ANCHOR_SCHEMA, 'opacity': 1, 'rotation': 0,
            'origin': [0.2, 0.3], 'fraction_per_meter': [0.01, 0.02],
            'gravity': 9.81, 'vx': 5, 'vy': 0, 'physical_seconds': 2}]}],
    'checkpoints': [{'id': 'safe ID', 'anchor': ANCHOR_SCHEMA, 'reason': 'what should be visible and why',
                     'object_ids': ['relevant instance IDs']}]
}
MOTION_RULES = (
    'Every accepted component gets exactly one motion object entry; do not add unrequested objects. '
    'Put hold x/y, linear from/to, or projectile origin/fraction_per_meter/gravity/vx/vy/physical_seconds '
    'DIRECTLY on its track. The schema example shows a projectile track; use only the appropriate fields for each kind. '
    'All object sizes and x/y coordinates are fractions of the output frame. Fonts and strokes use pixels; rotations use degrees. '
    'A circular SVG must remain circular in pixels: width*viewport_width equals height*viewport_height. '
    'The position refers to the asset pivot, or the center [0.5,0.5] for native objects. '
    'The first track starts when the object should appear; it is hidden earlier. The last-started track owns the object and holds '
    'its final state after its end. Opacity is constant per track. To hide an object, add a later hold with opacity=0. '
    'Tracks must have increasing start times and non-overlapping intervals. Zero-length intervals are not allowed. '
    'hold: x,y; linear: from,to; projectile: origin,fraction_per_meter,gravity,vx,vy,physical_seconds. '
    'Projectile x=origin_x+sx*vx*t, y=origin_y-sy*(vy*t-0.5*gravity*t*t), with physical t progressing linearly '
    'from zero to physical_seconds over the narration interval. Positive vy means initially upward; gravity is positive downward. '
    'Choose physical duration and spatial scale so landing coincides with the planned ground; there is no automatic ground collision or clamping. '
    'A new track can reset a demonstration at an explicitly explained replay. Use equal release anchors, physical duration and vertical '
    'scales for the two balls in the comparison. Preserve the initial zero vertical velocities. '
    'Native line objects are horizontal within their width/height box; rotation changes their direction. arrow_end=true adds an arrowhead. '
    'Use exact word IDs supplied by MFA, with edge start/end and small offset_seconds if needed. audio_start/audio_end are also available. '
    'The actual audio duration owns the film clock. Do not invent timestamps or alter the narration. '
    'Reserve the bottom 20% for transcript captions. Keep visual objects, labels and their complete paths in the upper 80%. '
    'Show useful static context from audio_start. Provide 3..8 checkpoint anchors for setup, action, comparison and conclusion. '
    'Return one complete JSON object. Do not generate JavaScript or JSX.'
)


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load_narration(path):
    value = read(path)
    if value.get('schema_version') != 'infofilm.animation-narration.v1':
        raise ValueError('Expected an animation narration input bundle')
    if digest(value['transcript']) != value['transcript_sha256']:
        raise ValueError('Narration transcript hash changed')
    for field in ('audio', 'timing'):
        if digest(Path(value[field]).read_bytes()) != value[field+'_sha256']:
            raise ValueError('Frozen narration '+field+' changed')
    timing = read(value['timing'])
    if timing['audio_sha256'] != value['audio_sha256']:
        raise ValueError('MFA map belongs to different audio')
    expected = [{k:w[k] for k in ('id','word','start','end')} for w in timing['words']]
    if expected != value['words'] or [w['word'] for w in expected] != value['transcript'].split():
        raise ValueError('MFA map no longer matches the supplied transcript')
    if not finite(value['duration_seconds']) or value['duration_seconds'] <= 0:
        raise ValueError('Invalid narration duration')
    prior, ids = 0, set()
    for word in expected:
        if word['id'] in ids or not finite(word['start']) or not finite(word['end']) or not prior <= word['start'] <= word['end'] <= value['duration_seconds']+.001:
            raise ValueError('Invalid MFA word identity or span')
        ids.add(word['id']); prior=word['end']
    return value


def anchor_time(anchor, narration):
    if not isinstance(anchor, dict) or anchor.get('edge') not in {'start','end'}:
        raise ValueError('Timing needs {word_id, edge:start|end, offset_seconds}')
    offset=anchor.get('offset_seconds', 0)
    if not finite(offset) or abs(offset)>5:
        raise ValueError('Anchor offset must be finite and within five seconds')
    identity=anchor.get('word_id')
    if identity=='audio_start': base=0
    elif identity=='audio_end': base=narration['duration_seconds']
    else:
        word=next((w for w in narration['words'] if w['id']==identity),None)
        if word is None: raise ValueError('Unknown MFA word ID: '+str(identity))
        base=word[anchor['edge']]
    value=base+offset
    if not 0<=value<=narration['duration_seconds']:
        raise ValueError('Anchor leaves the narration interval')
    return value


def captions(words):
    result=[]; group=[]
    for index,word in enumerate(words):
        group.append(word)
        if len(group)>=9 or re.search(r'[.!?]$',word['word']) or index==len(words)-1:
            result.append({'start':group[0]['start'],'end':group[-1]['end'],
                'text':' '.join(w['word'] for w in group)})
            group=[]
    return result


def review_errors(value):
    if not isinstance(value,dict):return ['Review must be a JSON object']
    errors=[]
    if not isinstance(value.get('pass'),bool):errors.append('Review pass must be boolean')
    if not finite(value.get('score')) or not 0<=value['score']<=100:errors.append('Review score must be 0..100')
    issues=value.get('issues')
    if not isinstance(issues,list):errors.append('Review issues must be a list')
    else:
        for issue in issues:
            if not isinstance(issue,dict) or not isinstance(issue.get('severity'),str) or issue['severity'] not in {'major','minor'} or not all(isinstance(issue.get(k),str) and issue[k].strip() for k in ('field','problem','correction')):
                errors.append('Each review issue needs severity, field, problem and correction')
    return errors


def passing_review(value):
    return not review_errors(value) and value['pass'] and value['score']>=82 and not any(i['severity']=='major' for i in value['issues'])


def phase(models,store,name,task,context,validator,max_tokens,max_attempts=3,review_invalid=False,reviewed=True):
    identity=digest({'contract':CONTRACT,'task':task,'context':context,
                     'planner':models.config['planner_url'],'reviewer':models.config['reviewer_url'],
                     **({'review_mode':'code_only','model':models.config.get('planner_model'),
                         'reasoning_effort':models.config.get('reasoning_effort')} if not reviewed else {})})
    root=store.root/'planning'/name/identity[:16]
    selected=root/'selected.json'
    if selected.exists():
        value=read(selected); errors=validator(value)
        if errors: raise ValueError('Saved '+name+' no longer validates: '+str(errors))
        store.event('animation:'+name,'reused',{'selected':str(selected)})
        return value
    repairer=getattr(models,'schema_repairer',None)
    prior_evaluation=root/'evaluation.json'
    if repairer is not None and prior_evaluation.exists():
        previous=read(prior_evaluation)
        archive=root/('evaluation-before-local-schema-'+digest(previous)[:12]+'.json')
        if not archive.exists():write(archive,previous)
    feedback=''; history=[]
    prompt=task+'\nINPUT:\n'+json.dumps(context,ensure_ascii=False)
    if context.get('output_schema',{}).get('schema_version')=='infofilm.animation-placement.v1':
        prompt=(task+'\nRENDERER_GEOMETRY_CONVENTIONS:\n'
            'x/y locate the pivot. Convert width fractions with canvas WIDTH and height fractions with canvas HEIGHT '
            'before rotating in pixel space. A circular SVG needs width*canvas_width == height*canvas_height. '
            'Native lines are HORIZONTAL across their unrotated WIDTH; tall height does not make a vertical line. '
            'For a vertical line set a real width/length, thin height and rotation 90 or -90. The renderer trims '
            'stroke_width (default 3 pixels) at each end of a plain line; add those insets when specifying a shaft length. '
            'A width shorter than twice the stroke width produces no visible plain-line shaft. '
            'Use actual SVG landmark coordinates for contacts.\nINPUT:\n'+json.dumps(context,ensure_ascii=False))
    for attempt in range(max_attempts):
        budget_exhausted=False
        try:
            value=models.call('planner',f'animation-{name}-{identity[:8]}-{attempt}',prompt+feedback,max_tokens=max_tokens)
            errors=validator(value)
        except ModelOutputError as error:
            value=None; errors=[str(error)]
            budget_exhausted=getattr(error,'output_budget_exhausted',False)
        review=None
        if value is not None:
            write(root/f'candidate-{attempt}.json',value)
        repair=None;original_errors=list(errors)
        if errors and value is not None and repairer is not None:
            previous_review=history[-1].get('review') if history else None
            repaired,repair=repairer.repair(name,value,context,errors,validator,root,previous_review)
            if repair and repair['accepted']:
                value=repaired;errors=validator(value)
                write(root/f'repaired-candidate-{attempt}.json',value)
        if reviewed and value is not None and (not errors or review_invalid):
            review_prompt=(
                'Independently review this animation plan for the visual message, source facts, useful teaching sequence, '
                'explicit requirement adherence and narration synchronization. Code owns identity, timing bounds and supported '
                'primitive validation; supplied code errors are authoritative. When code errors are present, explain their '
                'cause and practical correction instead of claiming the candidate is valid. Do not invent renderer capabilities. Missing assets are valid requests during component '
                'planning and should pause resolution, not force unrelated replacements. Reject concrete semantic defects, '
                'wrong physics, essential missing components, or motion coinciding with the wrong spoken explanation. '
                'Different reasonable teaching arrangements are acceptable. Do not demand the hand-authored baseline or any '
                'unprovided example. Return {pass:boolean,score:0..100,issues:[{severity:major|minor,field,problem,correction}],'
                'strengths:[string]}. Pass needs score>=82 and no major issue.\nTASK:\n'+task+'\nINPUT:\n'+
                json.dumps(context,ensure_ascii=False)+'\nCANDIDATE:\n'+json.dumps(value,ensure_ascii=False)+'\nCODE_ERRORS:\n'+json.dumps(errors))
            if not errors:
                from .animation_review_geometry import placement_geometry
                geometry=placement_geometry(value,context)
                if geometry is not None:
                    review_prompt+=('\nRENDERER_DERIVED_PLACEMENT_GEOMETRY:\n'+json.dumps(geometry)+
                        '\nUse these renderer-derived pixel measurements when judging sizes, line endpoints, '
                        'contacts and rotation. Do not mix width fractions with height fractions. These measurements '
                        'do not approve semantic relationships: evaluate the teaching meaning independently.')
            for format_attempt in range(2):
                try:
                    review=models.call_json('reviewer',f'animation-{name}-review-{identity[:8]}-{attempt}'+('-schema-repair' if format_attempt else ''),
                        review_prompt,max_tokens=3500)
                    shape_errors=review_errors(review)
                except ModelOutputError as exc:
                    shape_errors=[str(exc)]
                    budget_exhausted=getattr(exc,'output_budget_exhausted',False)
                if budget_exhausted:break
                if not shape_errors:break
                review_prompt+='\nReturn the exact review schema. Format errors: '+json.dumps(shape_errors)
            if shape_errors:errors.extend(shape_errors)
            write(root/f'review-{attempt}.json',review)
        accepted=not errors and (not reviewed or passing_review(review))
        row={'attempt':attempt+1,'code_errors':errors,'review':review,'accepted':accepted}
        if budget_exhausted:row['output_budget_exhausted']=True
        if repair is not None:
            row.update(original_code_errors=original_errors,local_schema_repair=repair)
        history.append(row);write(root/'evaluation.json',{'phase':name,'input_sha256':identity,'attempts':history,'accepted':accepted,
            **({'review_mode':'code_only','semantic_review':'not_run'} if not reviewed else {})})
        store.event('animation:'+name,'accepted' if accepted else 'needs_repair',
                    {'attempt':attempt+1,'code_errors':errors,
                     **({'review_score':review.get('score') if isinstance(review,dict) else None} if reviewed else {'validation':'code_only'})})
        if accepted:
            write(selected,value);write(store.root/(name+'-plan.json'),value)
            return value
        if budget_exhausted:
            store.event('animation:'+name,'output_budget_stalled',{'attempt':attempt+1,
                'reason':'Recovery ceiling reached; do not spend semantic retries on another truncated request'})
            break
        feedback='\nRepair the candidate using actual defects. Keep correct decisions. Return a complete compact JSON object.\n'+json.dumps({'candidate':value,'errors':errors,'review':review},ensure_ascii=False)
    message=(name+(' reached the output-budget recovery ceiling' if budget_exhausted else
        (' needs review' if reviewed else ' failed code validation')+' after bounded attempts')+
        '; inspect '+str(root/'evaluation.json'))
    write(store.root/'state.json',{'state':'planning_needs_review','resume_from':name,'evaluation':str(root/'evaluation.json'),'error':message})
    raise RuntimeError(message)


def component_errors(value,requirement):
    from .animation_objects import validate_component_plan
    errors=validate_component_plan(value)
    if not isinstance(value,dict):return errors
    if value.get('schema_version')!='infofilm.animation-components.v1':errors.append('Wrong component schema')
    for key,wanted in [('requirement_id',requirement['id']),('scene_id',requirement['id']),('visual_message',requirement['visual_message'])]:
        if value.get(key)!=wanted: errors.append(key+' must equal the supplied requirement verbatim')
    objects=value.get('objects',[])
    for obj in objects if isinstance(objects,list) else []:
        if isinstance(obj,dict) and (not isinstance(obj.get('physics_role'),str) or obj['physics_role'] not in {'static','projectile','annotation'}):
            errors.append(str(obj.get('id'))+' needs physics_role static|projectile|annotation')
    return errors


def compile_motion(draft,components,assets,narration,width,height,fps=30):
    errors=[]; compiled=[]
    if not isinstance(draft,dict):return None,['Motion draft must be one JSON object']
    if draft.get('schema_version')!='infofilm.animation-motion-draft.v1': errors.append('Wrong motion schema')
    if draft.get('scene_id')!=components['scene_id'] or draft.get('visual_message')!=components['visual_message']:
        errors.append('Motion scene_id and visual_message must match the accepted components')
    defs={o['id']:o for o in components['objects']}
    objects=draft.get('objects',[])
    if not isinstance(objects,list): return None,['objects must be a list']
    if len(objects)!=len(defs) or {o.get('id') for o in objects if isinstance(o,dict) and isinstance(o.get('id'),str)}!=set(defs):
        errors.append('Motion must include exactly the accepted component instance IDs')
    for obj in objects:
        if not isinstance(obj,dict) or not isinstance(obj.get('id'),str) or obj['id'] not in defs:
            errors.append('Unknown motion object');continue
        source=defs[obj['id']]
        if 'kind' in obj and obj['kind']!=source['kind']:errors.append(obj['id']+' cannot change the accepted component kind')
        if 'asset_id' in obj and obj['asset_id']!=source.get('asset_id'):errors.append(obj['id']+' cannot change the accepted asset')
        size=(obj.get('width'),obj.get('height'))
        if not all(finite(x) and 0<x<=1 for x in size):
            errors.append(obj['id']+' width/height must be positive frame fractions');continue
        if source['kind']=='text' and (not isinstance(obj.get('text'),str) or not obj['text'].strip() or len(obj['text'])>150 or not finite(obj.get('font_size')) or not 18<=obj['font_size']<=100):
            errors.append(obj['id']+' needs a short text and font_size 18..100 pixels')
        asset=assets.get(source.get('asset_id'),{})
        if 'circular' in asset.get('capabilities',[]) and abs(size[0]*width/(size[1]*height)-1)>.02:
            errors.append(obj['id']+' circular SVG must have equal pixel width and height')
        for field in ('color',):
            if field in obj and (not isinstance(obj[field],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',obj[field])):errors.append(obj['id']+' '+field+' must be a hex color')
        if 'z_index' in obj and (not isinstance(obj['z_index'],int) or isinstance(obj['z_index'],bool)):errors.append(obj['id']+' z_index must be an integer')
        if 'stroke_width' in obj and (not finite(obj['stroke_width']) or not 0<obj['stroke_width']<=30):errors.append(obj['id']+' invalid stroke_width')
        if 'arrow_end' in obj and not isinstance(obj['arrow_end'],bool):errors.append(obj['id']+' arrow_end must be boolean')
        pivot=obj.get('pivot',asset.get('pivot',[.5,.5]))
        if not isinstance(pivot,list) or len(pivot)!=2 or not all(finite(x) and 0<=x<=1 for x in pivot):
            errors.append(obj['id']+' has invalid pivot');continue
        tracks=[]; prior_end=-1
        raw_tracks=obj.get('tracks')
        if not isinstance(raw_tracks,list):
            errors.append(obj['id']+' tracks must be a list');raw_tracks=[]
        for index,track in enumerate(raw_tracks):
            try:
                if not isinstance(track,dict):raise ValueError('Track must be an object')
                start=anchor_time(track['start_anchor'],narration);end=anchor_time(track['end_anchor'],narration)
                if not start<end or start<prior_end-1e-8: raise ValueError('Tracks reverse, overlap or have zero duration')
                prior_end=end
                kind=track.get('kind')
                if not isinstance(kind,str) or kind not in {'hold','linear','projectile'}: raise ValueError('Unsupported motion primitive')
                built={k:copy.deepcopy(v) for k,v in track.items() if k not in {'start_anchor','end_anchor'}}
                built.update(start=start,end=end)
                opacity=built.get('opacity',1);rotation=built.get('rotation',0)
                if not finite(opacity) or not 0<=opacity<=1 or not finite(rotation): raise ValueError('Invalid opacity/rotation')
                def pair(key):
                    value=built[key]
                    if not isinstance(value,list) or len(value)!=2 or not all(finite(x) for x in value): raise ValueError('Invalid '+key)
                    return value
                fractions=[0,.25,.5,.75,1]
                if kind=='hold':
                    if not all(finite(built.get(k)) for k in ('x','y')):raise ValueError('hold needs finite x,y')
                    positions=[(built['x'],built['y'])]
                elif kind=='linear':
                    positions=[pair('from'),pair('to')]
                else:
                    origin=pair('origin');scale=pair('fraction_per_meter')
                    if not all(x>0 for x in scale) or not all(finite(built.get(k)) for k in ('gravity','vx','vy','physical_seconds')) or built['gravity']<=0 or built['physical_seconds']<=0:
                        raise ValueError('Invalid projectile units/parameters')
                    peak=built['vy']/built['gravity']/built['physical_seconds']
                    if 0<peak<1:fractions.append(peak)
                    positions=[]
                    for f in fractions:
                        t=f*built['physical_seconds']
                        positions.append((origin[0]+scale[0]*built['vx']*t,origin[1]-scale[1]*(built['vy']*t-.5*built['gravity']*t*t)))
                tracks.append(built)
                if opacity>0:
                    theta=math.radians(rotation);c=math.cos(theta);s=math.sin(theta)
                    for x,y in positions:
                        for dx in (-pivot[0]*size[0]*width,(1-pivot[0])*size[0]*width):
                            for dy in (-pivot[1]*size[1]*height,(1-pivot[1])*size[1]*height):
                                px=x*width+dx*c-dy*s;py=y*height+dx*s+dy*c
                                if not -.1<=px<=width+.1 or not -.1<=py<=height*.8+.1:
                                    raise ValueError(f'Object or its motion leaves the upper 80% visual area: corner_px=({px:.2f},{py:.2f}), '
                                        f'allowed_x=[0,{width}], allowed_y=[0,{height*.8:.2f}], pivot={pivot}, '
                                        f'size_px=({size[0]*width:.2f},{size[1]*height:.2f}). x/y place the pivot, not the center; '
                                        'change placement, scale or physical duration to keep the entire path inside these bounds.')
            except (ValueError,KeyError,TypeError) as exc:
                errors.append(f"{obj['id']}.tracks[{index}]: {exc}")
        if not tracks:errors.append(obj['id']+' has no executable tracks')
        if source['physics_role']=='projectile' and not any(t['kind']=='projectile' for t in tracks):
            errors.append(obj['id']+' is a physical projectile but has no projectile track')
        output={k:copy.deepcopy(v) for k,v in obj.items() if k!='tracks'}
        output.update(kind=source['kind'],asset_id=source.get('asset_id'),tracks=tracks)
        compiled.append(output)
    checkpoints=[]
    raw=draft.get('checkpoints',[])
    if not isinstance(raw,list) or not 3<=len(raw)<=8:errors.append('Provide 3..8 review checkpoints')
    else:
        seen=set()
        for row in raw:
            try:
                if not isinstance(row,dict):raise ValueError('Checkpoint must be an object')
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',row['id']) or row['id'] in seen:raise ValueError('Invalid checkpoint ID')
                if not isinstance(row.get('reason'),str) or not row['reason'].strip():raise ValueError('Checkpoint needs a reason')
                if not isinstance(row.get('object_ids'),list) or not all(isinstance(i,str) for i in row['object_ids']):raise ValueError('Checkpoint needs an object ID list')
                if not set(row.get('object_ids',[]))<=set(defs):raise ValueError('Unknown checkpoint object')
                seen.add(row['id']);checkpoints.append({'id':row['id'],'time':anchor_time(row['anchor'],narration),'reason':row['reason']})
            except (ValueError,KeyError,TypeError) as exc:errors.append('Checkpoint: '+str(exc))
    background=draft.get('background')
    if not isinstance(background,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',background):errors.append('background needs six-digit hex color')
    plan={'schema_version':'infofilm.animation-motion.v1','scene_id':components['scene_id'],
          'visual_message':components['visual_message'],'width':width,'height':height,'fps':fps,
          'duration_seconds':math.ceil(narration['duration_seconds']*fps-1e-8)/fps,'background':background,
          'objects':compiled,'captions':captions(narration['words']),'checkpoints':checkpoints}
    return plan,errors


def render_reusable(folder,props_path):
    results=folder/'render-results.json'
    if not results.exists():return False
    value=read(results)
    if not value.get('complete') or value.get('props_sha256')!=hashlib.sha256(props_path.read_bytes()).hexdigest():return False
    video=value.get('video') or {}
    if video.get('file')!='animation.mp4' or not (folder/'animation.mp4').is_file():return False
    sources=['src/animation-experiment/index.tsx','src/animation-experiment/AnimationExperiment.tsx','src/animation-experiment/motion.ts','src/animation-experiment/assets.ts','src/animation-experiment/chapters.ts','src/animation-experiment/LayoutMeasurement.tsx','animation-experiment.cjs']
    expected={name:hashlib.sha256((ROOT/'renderer'/name).read_bytes()).hexdigest() for name in sources}
    return value.get('source_sha256')==expected and all((folder/sample['file']).is_file() for sample in value.get('checkpoints',[]))


def review_render(models,store,folder,requirement,narration):
    result=read(folder/'render-results.json');plan=read(folder/'compiled-plan.json')
    paths=[folder/sample['file'] for sample in result['checkpoints']]
    identity=digest({'contract':'animation-checkpoint-review.v1','requirement':requirement,'plan':plan,
        'images':[digest(path.read_bytes()) for path in paths],'reviewer':models.config['reviewer_url']})
    saved=folder/'frame-review.json'
    if saved.exists() and read(saved).get('input_sha256')==identity:
        store.event('animation:frame-review','reused',{'orientation':folder.name})
        return read(saved)
    prompt=(
        'Review the actual rendered explanatory animation checkpoints, in supplied image order. '
        'Use the scene requirement, narration and compiled motion plan. Check whether visible objects, placement, '
        'physics and teaching sequence convey the message. Code has checked frame bounds, font overflow, identities '
        'and word-anchor timing, but those checks do not prove semantic correctness. Specifically inspect the launch '
        'at the cannon muzzle, meaningful support/height, path and landing relative to the visible ground, a distinct '
        'simultaneous comparison, and whether a replay or frozen example confuses the narration. '
        'Read the projectile equations to verify vertical behavior; still images alone cannot prove continuous motion. '
        'Reject concrete factual or visual errors; do not demand decorative polish or the hidden baseline. '
        'Return {pass:boolean,score:0..100,issues:[{severity:major|minor,field,problem,correction}],'
        'strengths:[string],limitations:[string]}. Pass needs score>=82 and no major issue. '
        'State that this is checkpoint/plan review, not an audio listening review.\n'+
        json.dumps({'requirement':requirement,'narration':{'transcript':narration['transcript'],
            'words':narration['words']},'motion':plan,'image_order':[
            {k:sample[k] for k in ('id','time','reason')} for sample in result['checkpoints']]},ensure_ascii=False))
    review=None;errors=[]
    for attempt in range(2):
        try:
            review=models.call('reviewer',f'animation-frames-{folder.name}-{identity[:8]}-{attempt}',
                prompt,reference_image_paths=paths,max_tokens=3500)
            errors=review_errors(review)
        except ModelOutputError as exc:errors=[str(exc)]
        if not errors:break
        prompt+='\nReturn the exact schema, correcting these format errors: '+json.dumps(errors)
    value={'schema_version':'infofilm.animation-frame-review.v1','input_sha256':identity,
        'accepted':not errors and passing_review(review),'review':review,'code_errors':errors,
        'human_review':'pending','limitations':['Checkpoint images and motion plan reviewed; continuous video and audio still need human review.']}
    write(saved,value)
    store.event('animation:frame-review','accepted' if value['accepted'] else 'needs_review',
        {'orientation':folder.name,'score':review.get('score') if isinstance(review,dict) else None})
    return value


def preview_draft(path,store,models,orientation):
    """Render an actual saved model draft for evaluation without approving it."""
    path=Path(path).resolve()
    if not path.is_relative_to((store.root/'planning'/('motion-'+orientation)).resolve()):
        raise ValueError('Preview candidate must belong to this experiment and orientation')
    value=read(path)
    for result_path in (store.root/'requests').glob('animation-motion-'+orientation+'-*.result.json'):
        if digest(read(result_path))!=digest(value):continue
        request_path=result_path.with_name(result_path.name.replace('.result.json','.request.json'))
        if not request_path.exists():continue
        request=read(request_path)
        if request.get('role')=='planner' and request.get('url')==models.config['planner_url']:
            write(store.root/'preview-source.json',{'schema_version':'infofilm.animation-unapproved-preview.v1',
                'candidate':str(path),'candidate_sha256':digest(value),'model_request':str(request_path),
                'model':request.get('model'),'approved':False,'notice':'Model-written draft preview; semantic approval remains pending.'})
            return value
    raise ValueError('Preview candidate does not match a saved planner response from this model route')


def publish(store,requirement,orientations,preview=False):
    sections=[];files=['report.html','components-plan.json','object-resolution.json']
    for orientation in orientations:
        folder=store.root/'renders'/orientation
        result=read(folder/'render-results.json');clip=folder/'animation.mp4'
        data='data:video/mp4;base64,'+base64.b64encode(clip.read_bytes()).decode()
        images=''
        for sample in result['checkpoints']:
            name='renders/'+orientation+'/'+sample['file'];files.append(name)
            images+='<figure><img src="'+name+'"><figcaption>'+html.escape(sample['reason'])+'</figcaption></figure>'
        files.extend(['motion-'+orientation+'-plan.json','renders/'+orientation+'/render-results.json'])
        review_path=folder/'frame-review.json';review_summary='Human visual and listening review pending.'
        if review_path.exists():
            assessment=read(review_path);name='renders/'+orientation+'/frame-review.json';files.append(name)
            review_summary+=' Checkpoint reviewer: '+('passed' if assessment['accepted'] else 'needs attention')+'. '
            review_summary+='<a href="'+name+'">Reviewer findings</a>'
        sections.append('<h2>'+orientation.title()+'</h2><p>'+review_summary+'</p><video controls playsinline src="'+data+'"></video><div class="frames">'+images+'</div>')
    notice=''
    if preview:
        files.append('preview-source.json')
        notice='<p style="border:2px solid #efbd56;padding:14px"><strong>Unapproved model draft — needs review.</strong> This preview preserves the saved model objects and motion. Its geometry passed code checks; the semantic planning loop did not accept it. <a href="preview-source.json">Source/provenance</a></p>'
    for name in ('comparison-inputs.json','role-comparison-results.json'):
        if (store.root/name).exists():
            files.append(name);sections.append('<p><a href="'+name+'">'+html.escape(name)+'</a></p>')
    (store.root/'report.html').write_text('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Local-model animation experiment</title><style>body{max-width:1100px;margin:auto;padding:28px;background:#071b29;color:#eff8fd;font:17px/1.5 system-ui}video{max-width:100%;max-height:80vh}.frames{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}figure{margin:0}img{max-width:100%}a{color:#83e5f5}</style></head><body><h1>'+html.escape(requirement['subject'])+'</h1>'+notice+'<p>Objects, arrangement and motion were planned and reviewed by the configured local-network LLMs. Narration was hand-written, rendered by VibeVoice and aligned by MFA. The baseline scene and its timing were excluded from model inputs.</p><p>'+html.escape(requirement['visual_message'])+'</p><p><a href="components-plan.json">Component plan</a> · <a href="object-resolution.json">Resolution</a></p>'+''.join(sections)+'</body></html>')
    write(store.root/'public-report.json',{'schema_version':'infofilm.public-experiment-report.v1','files':files})


def publish_failure(store,requirement,narration,error):
    """A failed model trial remains inspectable without presenting a bad clip as accepted."""
    if (store.root/'report.html').exists():return
    files=['report.html','state.json'];links=[];evaluations=[]
    for name in ('components-plan.json','object-resolution.json','initial-motion-trial.json'):
        if (store.root/name).exists():files.append(name);links.append('<a href="'+name+'">'+html.escape(name)+'</a>')
    for path in sorted((store.root/'planning').glob('*/*/evaluation.json')):
        name=path.relative_to(store.root).as_posix();files.append(name)
        evaluations.append('<details open><summary>'+html.escape(path.parent.parent.name)+'</summary><pre>'+html.escape(json.dumps(read(path),indent=2))+'</pre></details>')
    audio='data:audio/wav;base64,'+base64.b64encode(Path(narration['audio']).read_bytes()).decode()
    (store.root/'report.html').write_text('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Animation planning trial</title><style>body{max-width:1100px;margin:auto;padding:28px;background:#071b29;color:#eff8fd;font:17px/1.5 system-ui}a{color:#83e5f5}pre{white-space:pre-wrap;font-size:14px}audio{width:100%}</style></head><body><h1>'+html.escape(requirement['subject'])+'</h1><p>The local-model planning trial needs attention. No animation was accepted or substituted from the hand-authored baseline.</p><p>'+html.escape(str(error))+'</p><h2>Fixed narration</h2><audio controls src="'+audio+'"></audio><p>Hand-written script, VibeVoice audio and machine MFA. Human listening/timing review pending.</p><p>'+' · '.join(links)+'</p>'+''.join(evaluations)+'</body></html>')
    write(store.root/'public-report.json',{'schema_version':'infofilm.public-experiment-report.v1','files':files})


def run(args):
    from .animation_objects import catalog,registry_snapshot,resolve_components
    store=Store(args.output)
    try:
        requirement=read(args.requirement);narration=load_narration(args.narration_input)
        if requirement.get('schema_version')!='infofilm.animation-requirement.v1' or not requirement.get('visual_message'):
            raise ValueError('Invalid animation scene requirement')
        config=configuration(args.config)
        if getattr(args,'swap_models',False):
            config={**config,'planner_url':config['reviewer_url'],'reviewer_url':config['planner_url']}
        frozen={'contract':CONTRACT,'requirement':requirement,'narration':narration,
                'models':{k:config[k] for k in ['planner_url','reviewer_url']}}
        inputs=store.root/'inputs.json'
        if inputs.exists() and digest(read(inputs))!=digest(frozen):
            raise ValueError('Frozen requirement/audio/model routes changed; use a new --output directory')
        if args.resume and not inputs.exists():raise ValueError('--resume needs an existing experiment')
        write(inputs,frozen)
        cat_path=store.root/'initial-library-catalog.json'
        if not cat_path.exists():write(cat_path,catalog(args.library))
        initial_catalog=read(cat_path)
        models=Models(config,store)
        narration_context={'transcript':narration['transcript'],'duration_seconds':narration['duration_seconds']}
        components=phase(models,store,'components',
            'Plan the components for an explanatory animation from the supplied requirement and narration. '
            'Choose a teaching strategy and all necessary instances. Reuse available SVG assets when their actual '
            'capabilities fit. Native text, rectangle and line are built-in components; they do not need SVG requests. '
            'For an unavailable SVG, use request_id and a structured component_request with a proposed new asset identity. '
            'A requested asset is allowed to be missing; compilation will pause and an operator can replenish the library. '
            'SVG objects have exactly one of asset_id or request_id. Native objects have neither. '
            'This phase selects objects and teaching roles only. Do not plan positions, motion or timestamps yet. '
            'Return the specified JSON schema. Preserve the requirement id and visual_message verbatim.',
            {'requirement':requirement,'narration':narration_context,'component_library':initial_catalog,'output_schema':COMPONENT_SCHEMA},
            lambda value:component_errors(value,requirement),6500,args.max_attempts)
        effective=copy.deepcopy(components)
        if args.bindings:
            bindings=read(args.bindings)
            if not isinstance(bindings,dict) or not set(bindings)<={o['id'] for o in components['objects']}:
                raise ValueError('Bindings map component instance IDs to explicit registered asset IDs')
            for obj in effective['objects']:
                if obj['id'] in bindings:
                    if obj['kind']!='svg' or not isinstance(bindings[obj['id']],str):raise ValueError('Only SVG objects can be rebound')
                    if obj.get('request_id'):
                        original_request=next(r for r in effective['component_requests'] if r['id']==obj['request_id'])
                        obj['required_capabilities']=sorted(set(obj['required_capabilities'])|set(original_request['required_capabilities']))
                    obj['asset_id']=bindings[obj['id']];obj['request_id']=None
            still_referenced={obj.get('request_id') for obj in effective['objects']}
            effective['component_requests']=[r for r in effective['component_requests'] if r['id'] in still_referenced]
            write(store.root/'object-bindings.json',bindings)
        elif (store.root/'object-bindings.json').exists():
            raise ValueError('This experiment has explicit bindings; pass the same --bindings file when resuming')
        registry=registry_snapshot(args.library)
        assets,faults=resolve_components(effective,args.library,registry)
        requests={r['id']:r for r in effective['component_requests']}
        for obj in effective['objects']:
            if obj.get('request_id'):obj['asset_id']=requests[obj['request_id']]['requested_asset_id']
        resolution={'schema_version':'infofilm.animation-resolution.v1','scene_id':requirement['id'],
                    'visual_message':requirement['visual_message'],'component_plan_sha256':digest(components),
                    'assets':{k:{field:value for field,value in v.items() if field!='data_uri'} for k,v in assets.items()},
                    'faults':faults,'ready':not faults}
        prior_path=store.root/'object-resolution.json'
        if prior_path.exists():
            prior=read(prior_path)
            for identity,asset in prior.get('assets',{}).items():
                if identity in resolution['assets'] and asset['sha256']!=resolution['assets'][identity]['sha256']:
                    raise ValueError('Resolved shared asset changed; register a new version and bind explicitly')
        write(prior_path,resolution)
        if faults:
            write(store.root/'object-requests.json',{'schema_version':'infofilm.animation-object-requests.v1',
                'scene_id':requirement['id'],'visual_message':requirement['visual_message'],'requests':faults})
            write(store.root/'state.json',{'state':'waiting_for_components','resume_from':'object_resolution','requests':str(store.root/'object-requests.json')})
            store.event('animation:objects','waiting_for_components',{'count':len(faults),'component_plan_preserved':True})
            return 3
        write(store.root/'resolved-components.json',effective)
        if args.until=='components':
            write(store.root/'state.json',{'state':'components_ready','resume_from':'motion'})
            return 0
        orientations=['landscape','portrait'] if args.orientation=='both' else [args.orientation]
        reviews=[]
        for orientation in orientations:
            width,height=(1280,720) if orientation=='landscape' else (720,1280)
            context={'requirement':requirement,'accepted_components':effective,'resolved_assets':resolution['assets'],
                'narration':{**narration_context,'words':narration['words']},'viewport':{'width':width,'height':height,'fps':30,'visual_area_y':[0,.8]},
                'output_schema':MOTION_SCHEMA,'renderer_rules':MOTION_RULES}
            validator=lambda value:compile_motion(value,effective,assets,narration,width,height)[1]
            if getattr(args,'preview_candidate',None):
                draft=preview_draft(args.preview_candidate,store,models,orientation)
                errors=validator(draft)
                if errors:raise ValueError('Preview still needs valid geometry: '+str(errors))
                write(store.root/('motion-'+orientation+'-plan.json'),draft)
                store.event('animation:motion-preview','needs_review',{'orientation':orientation,'candidate':str(args.preview_candidate)})
            else:
                draft=phase(models,store,'motion-'+orientation,
                'Plan an executable explanatory scene. Decide placement, scale, layering, text and all movement for the '
                'accepted components. Sync each action to the frozen narration via MFA word anchors. Follow the supplied '
                'generic renderer contract and source facts. The actual narration duration is authoritative. '
                'Return only the complete requested motion-draft JSON.',context,validator,9500,args.max_attempts,review_invalid=True)
            compiled,errors=compile_motion(draft,effective,assets,narration,width,height)
            if errors:raise ValueError(str(errors))
            folder=store.root/'renders'/orientation;folder.mkdir(parents=True,exist_ok=True)
            write(folder/'compiled-plan.json',compiled)
            if args.until=='motion':continue
            props={'plan':compiled,'assets':assets,'audio_src':'data:audio/wav;base64,'+base64.b64encode(Path(narration['audio']).read_bytes()).decode()}
            props_path=write(folder/'props.json',props)
            results=folder/'render-results.json'
            if not render_reusable(folder,props_path):
                store.event('animation:render','running',{'orientation':orientation,'objects':len(compiled['objects'])})
                with (folder/'render.log').open('w') as log:
                    subprocess.run(['node',str(ROOT/'renderer/animation-experiment.cjs'),str(folder),config['browser'],str(props_path)],
                        cwd=ROOT/'renderer',stdout=log,stderr=subprocess.STDOUT,check=True)
            store.event('animation:render','succeeded',{'orientation':orientation,'result':str(results)})
            if args.until=='review':reviews.append(review_render(models,store,folder,requirement,narration))
        preview=bool(getattr(args,'preview_candidate',None))
        needs_review=preview or any(not value['accepted'] for value in reviews)
        write(store.root/'state.json',{'state':'preview_needs_review' if preview else 'motion_ready' if args.until=='motion' else 'rendered_needs_review' if needs_review else 'rendered',
            'orientations':orientations,'next':'human_visual_and_listening_review'})
        if args.until in {'render','review'}:publish(store,requirement,orientations,preview=preview)
        return 2 if needs_review else 0
    except Exception as exc:
        store.event('animation-experiment','failed',{'error':str(exc)})
        state_path=store.root/'state.json'
        state=read(state_path) if state_path.exists() else {}
        if state.get('state')!='planning_needs_review':write(state_path,{'state':'failed','error':str(exc),'previous_state':state})
        if 'requirement' in locals() and 'narration' in locals():publish_failure(store,requirement,narration,exc)
        raise
    finally:store.db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requirement',type=Path,default=ROOT/'animation-experiments/gravity/requirement.json')
    parser.add_argument('--narration-input',type=Path,default=ROOT/'artifacts/experiments/gravity-animation-llm/input-audio/narration-input.json')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/experiments/gravity-animation-llm')
    parser.add_argument('--library',type=Path,default=ROOT/'animation-library')
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--swap-models',action='store_true',help='Reverse planner/reviewer routes for this experiment only; use a separate output directory')
    parser.add_argument('--orientation',choices=['landscape','portrait','both'],default='landscape')
    parser.add_argument('--until',choices=['components','motion','render','review'],default='review')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--bindings',type=Path,help='Explicit object-instance to registered-asset replacements')
    parser.add_argument('--preview-candidate',type=Path,help='Render a saved model motion draft as an unapproved preview; does not freeze or accept it')
    parser.add_argument('--max-attempts',type=int,default=3)
    args=parser.parse_args()
    if not 1<=args.max_attempts<=5:parser.error('--max-attempts must be between one and five')
    if args.preview_candidate and (args.until not in {'render','review'} or args.orientation=='both'):
        parser.error('--preview-candidate needs one orientation and --until render or review')
    return run(args)
