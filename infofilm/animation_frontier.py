"""Chapter objective → teaching → narration → speech/MFA → visual plans → Remotion."""
from __future__ import annotations

import argparse
import base64
import copy
import html
import json
import re
import subprocess
import sys
from pathlib import Path

from .animation_experiment import (COMPONENT_SCHEMA, MOTION_SCHEMA, MOTION_RULES,
    compile_motion, component_errors, finite, load_narration, phase, render_reusable)
from .animation_objects import catalog, registry_snapshot, resolve_components
from .animation_openai import OpenAIAnimationModels
from .core import ROOT, Store, configuration, digest, read, write

CONTRACT = 'infofilm.animation-frontier.v1'
GENERAL_MOTION_RULES = MOTION_RULES.replace(
    'Use equal release anchors, physical duration and vertical scales for the two balls in the comparison. '
    'Preserve the initial zero vertical velocities. ',
    'Choose the motion primitives appropriate to this concept; use projectile motion only for freely falling objects. ')
CANVAS_SCHEMA = {
    'schema_version':'infofilm.animation-canvas.v1', 'requirement_id':'copy requirement id',
    'visual_message':'copy requirement visual_message verbatim',
    'learning_objective':'what viewers should understand',
    'viewport':{'width':'copy supplied pixels','height':'copy supplied pixels','fps':30},
    'visual_area':[0,0,1,0.8], 'caption_area':[0,0.8,1,0.2],
    'palette':{'background':'#hex','foreground':'#hex','accent':'#hex'},
    'layout_strategy':'how the canvas supports the explanation',
    'teaching_sequence':[{'id':'safe unique ID','message':'what this beat teaches',
                         'demonstration':'what changes on screen'}],
    'zones':[{'id':'safe unique ID','role':'purpose','box':[0.1,0.1,0.8,0.6]}]
}
PLACEMENT_SCHEMA = {
    'schema_version':'infofilm.animation-placement.v1','scene_id':'copy scene id',
    'visual_message':'copy visual_message verbatim','explanation':'why these positions support the concept',
    'objects':[{'id':'accepted instance ID','x':0.3,'y':0.3,'width':0.1,'height':0.1,
                'rotation':0,'z_index':1,'color':'#hex when applicable',
                'text':'label text only for text objects','font_size':'18..100 pixels only for text objects; size the box for all wrapped lines'}],
    'relationships':[{'subject':'object instance ID','relation':'supported_by|aligned_with|adjacent_to|other',
                      'object':'other instance ID','reason':'why this relationship matters'}]
}


def situation_handoff(situation):
    situation=situation.strip()
    if not situation:raise ValueError('Situation must be nonempty')
    # Match longform.chapter_context's envelope, without inventing research or audio.
    return {'film':{'subject':situation,'title':situation,'language':'en'},
        'chapter':{'id':'C01','title':situation,'objective':situation,
            'scope':{'start_year':None,'end_year':None,'description':situation},
            'topic_ids':[],'source_ids':[],'research_queries':[],'exclude':[],
            'entry_context':'A standalone explanation; no previous chapter.',
            'exit_handoff':'Conclude the explanation.','opening_role':'film_intro','ending_role':'film_conclusion',
            'visual_direction':'Develop a clear visual explanation. Choose the teaching sequence and required components.'},
        'owned_topics':[],'orientation_anchors':[],'previous_chapter':None,'next_chapter':None,
        'research_gaps':[],'shared_creative':{},'research_subject':situation}


def chapter_handoff(value, chapter_id=None):
    if not isinstance(value,dict):raise ValueError('Chapter input must be a JSON object')
    if value.get('schema_version')=='infofilm.chapter-plan.v0':
        chapters=value.get('chapters',[])
        selected=[c for c in chapters if isinstance(c,dict) and (chapter_id is None or c.get('id')==chapter_id)]
        if len(selected)!=1:raise ValueError('Select one chapter from the parent plan with --chapter-id C01')
        chapter=selected[0];index=chapters.index(chapter)
        value={'film':value.get('film'), 'chapter':chapter,
            'owned_topics':[t for t in value.get('coverage',[]) if t.get('owner_chapter_id')==chapter.get('id')],
            'orientation_anchors':[],
            'previous_chapter':chapters[index-1]['title'] if index else None,
            'next_chapter':chapters[index+1]['title'] if index+1<len(chapters) else None,
            'research_gaps':[g for g in value.get('research_gaps',[]) if g.get('chapter_id')==chapter.get('id')]}
    film,chapter=value.get('film'),value.get('chapter')
    if not isinstance(film,dict) or not isinstance(chapter,dict):
        raise ValueError('Expected the existing chapter-context envelope {film, chapter, owned_topics, orientation_anchors, ...}')
    for label,item,keys in [('film',film,('subject',)),('chapter',chapter,('id','title','objective'))]:
        if any(not isinstance(item.get(k),str) or not item[k].strip() for k in keys):raise ValueError(label+' is missing '+', '.join(keys))
    if chapter_id and chapter['id']!=chapter_id:raise ValueError('Chapter context does not match --chapter-id')
    scope=chapter.get('scope')
    if not isinstance(scope,dict) or not isinstance(scope.get('description'),str) or not scope['description'].strip():
        raise ValueError('Chapter needs scope.description')
    return copy.deepcopy(value)


def chapter_requirement(handoff):
    chapter=handoff['chapter'];subject=handoff['film']['subject']
    if chapter['title']!=subject:subject+=': '+chapter['title']
    slug=re.sub(r'[^a-z0-9]+','-',subject.lower()).strip('-')[:48] or 'concept'
    message=chapter['objective']
    animation_brief=chapter.get('production',{}).get('animation_brief')
    direction=chapter['scope']['description']+' '+chapter.get('visual_direction','')
    if animation_brief:
        direction+=' Demonstrations: '+'; '.join(animation_brief['demonstrations'])
    return {'schema_version':'infofilm.animation-requirement.v1',
        'id':slug+'-'+digest(handoff)[:10], 'subject':subject,
        'learning_objective':message,'visual_message':message,
        'scene_requirement':direction,
        'audience':handoff['film'].get('audience','Casual viewers and junior students'),
        'facts':[a['claim'] for a in (handoff.get('chapter_evidence',{}).get('facts') or handoff.get('orientation_anchors',[]))
                 if isinstance(a,dict) and a.get('claim')],
        'constraints':['Use short readable labels.',
            'Request missing components explicitly; do not substitute unrelated assets.',
            'Respect chapter scope, exclusions and continuity; runtime is advisory.'],
        'chapter_context':handoff}


def situation_requirement(situation):
    return chapter_requirement(situation_handoff(situation))


def input_requirement(args):
    if getattr(args,'situation',None):return situation_requirement(args.situation)
    if getattr(args,'chapter_plan',None):
        return chapter_requirement(chapter_handoff(read(args.chapter_plan),getattr(args,'chapter_id',None)))
    return read(args.requirement)


def canvas_errors(value, requirement, viewport):
    if not isinstance(value,dict):return ['Canvas must be one JSON object']
    errors=[]
    expected={'schema_version':'infofilm.animation-canvas.v1','requirement_id':requirement['id'],
        'visual_message':requirement['visual_message'],'viewport':viewport,
        'visual_area':[0,0,1,.8],'caption_area':[0,.8,1,.2]}
    for key,wanted in expected.items():
        if value.get(key)!=wanted:errors.append(key+' must match the supplied canvas contract')
    for key in ('learning_objective','layout_strategy'):
        if not isinstance(value.get(key),str) or not value[key].strip():errors.append(key+' must be nonempty')
    palette=value.get('palette',{})
    if not isinstance(palette,dict) or any(not re.fullmatch(r'#[0-9a-fA-F]{6}',str(palette.get(k,''))) for k in ('background','foreground','accent')):
        errors.append('palette needs background, foreground and accent six-digit hex colors')
    for field in ('teaching_sequence','zones'):
        rows=value.get(field)
        if not isinstance(rows,list) or not rows:errors.append(field+' must be nonempty');continue
        ids=set()
        for row in rows:
            if not isinstance(row,dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',str(row.get('id',''))) or row['id'] in ids:
                errors.append(field+' requires unique safe IDs');continue
            ids.add(row['id'])
            for key in (('message','demonstration') if field=='teaching_sequence' else ('role',)):
                if not isinstance(row.get(key),str) or not row[key].strip():errors.append(field+' needs '+key)
            if field=='zones':
                box=row.get('box')
                if not isinstance(box,list) or len(box)!=4 or not all(finite(v) for v in box):
                    errors.append('zone box needs [x,y,width,height] fractions')
                elif min(box[:2])<0 or min(box[2:])<=0 or box[0]+box[2]>1.000001 or box[1]+box[3]>.800001:
                    errors.append('zone leaves the upper 80% visual area')
    return errors


def placement_motion(value,canvas):
    objects=[]
    raw=value.get('objects',[])
    for item in raw if isinstance(raw,list) else []:
        if not isinstance(item,dict):continue
        obj={k:copy.deepcopy(v) for k,v in item.items() if k not in {'x','y','rotation'}}
        obj['tracks']=[{'kind':'hold','x':item.get('x'),'y':item.get('y'),
            'rotation':item.get('rotation',0),'start_anchor':{'word_id':'audio_start','edge':'start'},
            'end_anchor':{'word_id':'audio_end','edge':'end'}}]
        objects.append(obj)
    return {'schema_version':'infofilm.animation-motion-draft.v1', 'scene_id':value.get('scene_id'),
        'visual_message':value.get('visual_message'),'background':canvas['palette']['background'],
        'explanation':value.get('explanation'),'objects':objects,
        'checkpoints':[{'id':identity,'anchor':{'word_id':word,'edge':edge},
                       'reason':'Static placement validation','object_ids':[]}
            for identity,word,edge in [('setup','audio_start','start'),('end','audio_end','start'),('last','audio_end','end')]]}


def placement_errors(value,components,assets,narration,canvas):
    if not isinstance(value,dict):return ['Placement must be one JSON object']
    errors=[]
    if value.get('schema_version')!='infofilm.animation-placement.v1':errors.append('Wrong placement schema')
    if not isinstance(value.get('explanation'),str) or not value['explanation'].strip():errors.append('Placement needs explanation')
    static=copy.deepcopy(components)
    for obj in static['objects']:obj['physics_role']='static'
    draft=placement_motion(value,canvas)
    # Static geometry needs a validation interval, not speech or fabricated word timing.
    validation_clock=narration or {'duration_seconds':1.0,'words':[]}
    _,bounds=compile_motion(draft,static,assets,validation_clock,canvas['viewport']['width'],canvas['viewport']['height'])
    errors.extend(bounds)
    ids={o['id'] for o in components['objects']}
    if not isinstance(value.get('relationships'),list):errors.append('Placement relationships must be a list')
    else:
        for relationship in value['relationships']:
            if not isinstance(relationship,dict) or relationship.get('subject') not in ids or relationship.get('object') not in ids:
                errors.append('Placement relationships need existing object IDs')
            elif any(not isinstance(relationship.get(k),str) or not relationship[k].strip() for k in ('relation','reason')):
                errors.append('Placement relationships need relation and reason')
    return errors


def merge_animation(value,placement,canvas):
    draft={k:copy.deepcopy(v) for k,v in value.items() if k!='objects'}
    draft['background']=canvas['palette']['background']
    placed={obj['id']:obj for obj in placement['objects']}
    draft['objects']=[]
    for item in value.get('objects',[]):
        if not isinstance(item,dict) or item.get('id') not in placed:
            draft['objects'].append(item);continue
        obj={k:copy.deepcopy(v) for k,v in placed[item['id']].items() if k not in {'x','y','rotation'}}
        obj['tracks']=copy.deepcopy(item.get('tracks'))
        draft['objects'].append(obj)
    return draft


def animation_errors(value,placement,canvas,components,assets,narration):
    if not isinstance(value,dict):return ['Animation must be one JSON object']
    errors=[]
    objects=value.get('objects')
    if not isinstance(objects,list):return ['Animation objects must be a list']
    placed={obj['id']:obj for obj in placement['objects']}
    for obj in objects:
        if not isinstance(obj,dict):continue
        if set(obj)-{'id','tracks'}:errors.append(str(obj.get('id'))+' animation entry only permits id and tracks; placement is frozen')
        tracks=obj.get('tracks')
        if obj.get('id') not in placed or not isinstance(tracks,list) or not tracks:continue
        first=tracks[0]
        if not isinstance(first,dict):continue
        initial=([first.get('x'),first.get('y')] if first.get('kind')=='hold' else
                 first.get('from') if first.get('kind')=='linear' else first.get('origin'))
        wanted=[placed[obj['id']]['x'],placed[obj['id']]['y']]
        if not isinstance(initial,list) or len(initial)!=2 or any(not finite(a) or abs(a-b)>1e-6 for a,b in zip(initial,wanted)):
            errors.append(obj['id']+' must start at its frozen placement coordinates')
        if not finite(first.get('rotation',0)) or abs(first.get('rotation',0)-placed[obj['id']].get('rotation',0))>1e-6:
            errors.append(obj['id']+' must start at its frozen placement rotation')
    draft=merge_animation(value,placement,canvas)
    _,bounds=compile_motion(draft,components,assets,narration,canvas['viewport']['width'],canvas['viewport']['height'])
    return errors+bounds


def resolve_objects(store,components,library,bindings_path=None):
    effective=copy.deepcopy(components)
    if bindings_path:
        bindings=read(bindings_path)
        ids={obj['id'] for obj in effective['objects']}
        if not isinstance(bindings,dict) or not set(bindings)<=ids:raise ValueError('Bindings must map existing instance IDs to asset IDs')
        saved=store.root/'object-bindings.json'
        if saved.exists() and read(saved)!=bindings:raise ValueError('Frozen object bindings changed; use a new output directory')
        for obj in effective['objects']:
            if obj['id'] not in bindings:continue
            if obj['kind']!='svg' or not isinstance(bindings[obj['id']],str):raise ValueError('Only SVG objects can be rebound')
            if obj.get('request_id'):
                request=next(r for r in effective['component_requests'] if r['id']==obj['request_id'])
                obj['required_capabilities']=sorted(set(obj['required_capabilities'])|set(request['required_capabilities']))
            obj.update(asset_id=bindings[obj['id']],request_id=None)
        used={obj.get('request_id') for obj in effective['objects']}
        effective['component_requests']=[r for r in effective['component_requests'] if r['id'] in used]
        write(store.root/'object-bindings.json',bindings)
    elif (store.root/'object-bindings.json').exists():raise ValueError('Resume with the same explicit --bindings file')
    assets,faults=resolve_components(effective,library,registry_snapshot(library))
    requests={row['id']:row for row in effective['component_requests']}
    for obj in effective['objects']:
        if obj.get('request_id'):obj['asset_id']=requests[obj['request_id']]['requested_asset_id']
    resolution={'schema_version':'infofilm.animation-resolution.v1','scene_id':components['scene_id'],
        'visual_message':components['visual_message'],'component_plan_sha256':digest(components),
        'assets':{key:{k:v for k,v in asset.items() if k!='data_uri'} for key,asset in assets.items()},
        'faults':faults,'ready':not faults}
    previous=store.root/'object-resolution.json'
    if previous.exists():
        for key,asset in read(previous).get('assets',{}).items():
            if key in resolution['assets'] and asset['sha256']!=resolution['assets'][key]['sha256']:
                raise ValueError('Resolved immutable asset changed: '+key)
    write(previous,resolution)
    if faults:
        write(store.root/'object-requests.json',{'schema_version':'infofilm.animation-object-requests.v1',
            'scene_id':components['scene_id'],'visual_message':components['visual_message'],'requests':faults})
        write(store.root/'state.json',{'state':'waiting_for_components','resume_from':'object_resolution',
            'requests':str(store.root/'object-requests.json'),'human_review':'pending'})
        store.event('animation:objects','waiting_for_components',{'count':len(faults),'component_plan_preserved':True})
    else:write(store.root/'resolved-components.json',effective)
    return effective,assets,resolution


def beat_report_sections(root):
    """Publish accepted scoped plans, never provider requests or asset payloads."""
    root=Path(root).resolve();marker=root/'beat-execution.json'
    if not marker.exists():return [],[]
    index=read(marker);files=['beat-execution.json'];sections=[
        '<h2>Beat execution</h2><p>Each teaching beat owns its placement and motion. '
        'Narration and captions remain one chapter track.</p>']
    state=read(root/'state.json') if (root/'state.json').exists() else {}
    progress=state.get('beat_progress') or {}
    if progress:
        label=f"{progress.get('orientation','')} · beat {progress.get('position','?')}/{progress.get('total','?')} · {progress.get('beat_id','')} · {progress.get('phase','')}"
        sections.append('<p><strong>Current checkpoint:</strong> '+html.escape(label)+'</p>')
    def local_path(value):
        if not isinstance(value,(str,Path)):return None
        path=Path(value);path=path if path.is_absolute() else root/path
        path=path.resolve()
        return path if path.is_relative_to(root) and path.is_file() else None
    def link_file(path,label):
        name=str(path.relative_to(root));files.append(name)
        sections.append('<p><a href="'+html.escape(name,quote=True)+'">'+html.escape(label)+'</a></p>')
    manifests=index.get('manifest_paths',{})
    if isinstance(manifests,dict):manifests=list(manifests.values())
    if not isinstance(manifests,(list,tuple)):manifests=[]
    for value in manifests:
        path=local_path(value)
        if not path:continue
        manifest=read(path);link_file(path,'Beat timing manifest')
        rows=[];fps=manifest.get('fps',30)
        for beat in manifest.get('beats',[]):
            identity=beat.get('beat_id',beat.get('id',''))
            start=beat.get('start_frame',0)/fps;duration=beat.get('duration_frames',0)/fps
            rows.append('<tr><td>'+html.escape(str(identity))+'</td><td>'+f'{start:.2f}s'+'</td><td>'+f'{duration:.2f}s'+'</td></tr>')
        if rows:sections.append('<table><thead><tr><th>Beat</th><th>Starts</th><th>Duration</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>')
    roots=index.get('execution_roots',{})
    if not isinstance(roots,dict):roots={}
    for orientation,value in roots.items():
        path=Path(value) if isinstance(value,str) else None
        if path is None:continue
        path=(path if path.is_absolute() else root/path).resolve()
        if not path.is_relative_to(root) or not path.is_dir():continue
        sections.append('<h3>'+html.escape(str(orientation).title())+' beat plans</h3>')
        for artifact in sorted(path.rglob('*-plan.json')):
            safe=local_path(artifact)
            if safe:link_file(safe,artifact.name)
        for artifact in sorted(path.glob('**/planning/*/*/evaluation.json')):
            safe=local_path(artifact)
            if safe:link_file(safe,artifact.parent.parent.name+' validation and semantic review')
        for name in ('progress.json','terminal-state.json','entry-state.json','continuity-receipt.json'):
            for artifact in sorted(path.rglob(name)):
                safe=local_path(artifact)
                if safe:link_file(safe,artifact.name+' execution receipt')
    return list(dict.fromkeys(files)),sections


def publish_report(store,requirement,narration,orientations):
    files=['report.html','state.json'];sections=[]
    repair_receipts=list(store.root.glob('planning/components/*/schema-repair-adoption.json'))
    workflow=read(store.root/'inputs.json').get('contract') if (store.root/'inputs.json').exists() else None
    review_note=('Independent semantic reviews and bounded repairs are recorded for each planning phase.'
                 if workflow=='infofilm.animation-workflow.v2' else
                 'Local schema repair is recorded separately; no semantic LLM review.'
                 if repair_receipts else 'No LLM reviewer calls.')
    for receipt in repair_receipts:
        name=str(receipt.relative_to(store.root));files.append(name)
        sections.append('<p><a href="'+name+'">Schema repair provenance</a></p>')
    for receipt in sorted(store.root.glob('planning/*/*/local-schema-repair/*/evaluation.json')):
        name=str(receipt.relative_to(store.root));files.append(name)
        sections.append('<p><a href="'+name+'">Failure-only local repair provenance</a></p>')
        for verdict in sorted(receipt.parent.glob('verdict-*.json')):
            name=str(verdict.relative_to(store.root));files.append(name)
            sections.append('<p><a href="'+name+'">'+html.escape(verdict.name)+' (copy instructions and equivalence verdicts)</a></p>')
    for path in sorted(store.root.glob('*-plan.json')):
        files.append(path.name);sections.append('<p><a href="'+path.name+'">'+html.escape(path.name)+'</a></p>')
    beat_files,beat_sections=beat_report_sections(store.root)
    files.extend(beat_files);sections.extend(beat_sections)
    for name in ['requirement.json','chapter-context.json','object-resolution.json','object-requests.json','usage-summary.json',
                 'narration.txt','narration-state.json','narration-script-provenance.json','input-audio/narration-input.json',
                 'motion-capability-requests.json']:
        if (store.root/name).exists():files.append(name);sections.append('<p><a href="'+name+'">'+html.escape(name)+'</a></p>')
    for name in ['manual-components/gallery.html','manual-components/registration-receipts.json']:
        if (store.root/name).exists():
            files.append(name);sections.append('<p><a href="'+name+'">'+html.escape(name)+'</a></p>')
    for orientation in orientations:
        folder=store.root/'renders'/orientation
        animation_path=store.root/('animation-'+orientation+'-plan.json')
        if animation_path.exists():
            explanation=read(animation_path).get('explanation','')
            if explanation:sections.append('<p><strong>'+html.escape(orientation.title())+' animation plan:</strong> '+html.escape(explanation)+'</p>')
        if not (folder/'render-results.json').exists():continue
        result=read(folder/'render-results.json')
        if not result.get('complete'):continue
        video='data:video/mp4;base64,'+base64.b64encode((folder/'animation.mp4').read_bytes()).decode()
        sections.append('<h2>'+orientation.title()+'</h2><video controls playsinline src="'+video+'"></video>')
        video_path='renders/'+orientation+'/animation.mp4';files.append(video_path)
        sections.append('<p><a href="'+video_path+'" download>Download '+html.escape(orientation)+' preview</a></p>')
        files.append('renders/'+orientation+'/render-results.json')
        for sample in result['checkpoints']:
            name='renders/'+orientation+'/'+sample['file'];files.append(name)
            sections.append('<figure><img src="'+name+'"><figcaption>'+html.escape(sample['reason'])+'</figcaption></figure>')
    if narration and not any('video controls' in section for section in sections):
        audio='data:audio/wav;base64,'+base64.b64encode(Path(narration['audio']).read_bytes()).decode()
        sections.append('<audio controls src="'+audio+'"></audio>')
    status=read(store.root/'state.json')
    audio_note='Supplied narration and MFA are preserved.' if narration else 'Pre-audio planning: no narration or timing has been supplied.'
    order_note=('Ordered workflow: chapter → teaching → narration → speech/MFA → detailed visuals → Remotion.'
                if workflow=='infofilm.animation-workflow.v2' else 'Single OpenAI model: canvas, objects, placement, animation. Code validation only.')
    page='<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Animation chapter preview</title><style>body{max-width:1100px;margin:auto;padding:28px;background:#071b29;color:#eff8fd;font:17px/1.5 system-ui}video,img,audio{max-width:100%}video{max-height:80vh}a{color:#83e5f5}figure{margin:20px 0}pre{white-space:pre-wrap}</style></head><body><h1>'+html.escape(requirement['subject'])+'</h1><p>'+order_note+' '+review_note+' Human review pending. '+audio_note+' Baseline layout and motion were excluded from model inputs.</p><pre>'+html.escape(json.dumps(status,indent=2))+'</pre>'+''.join(sections)+'</body></html>'
    (store.root/'report.html').write_text(page)
    write(store.root/'public-report.json',{'schema_version':'infofilm.public-experiment-report.v1','files':files})


def usage_summary(store):
    responses=[read(path) for path in sorted((store.root/'requests').glob('*.response.json'))]
    requests=[read(path) for path in sorted((store.root/'requests').glob('*.request.json'))]
    summary={'schema_version':'infofilm.animation-usage.v1','calls':len(responses),
        'review_calls':sum(row.get('role')=='reviewer' for row in requests),
        'input_tokens':sum(row.get('usage',{}).get('input_tokens',row.get('usage',{}).get('prompt_tokens',0)) for row in responses),
        'cached_input_tokens':sum(row.get('usage',{}).get('input_tokens_details',{}).get('cached_tokens',0) for row in responses),
        'cache_write_tokens':sum(row.get('usage',{}).get('input_tokens_details',{}).get('cache_write_tokens',0) for row in responses),
        'output_tokens':sum(row.get('usage',{}).get('output_tokens',row.get('usage',{}).get('completion_tokens',0)) for row in responses),
        'total_tokens':sum(row.get('usage',{}).get('total_tokens',0) for row in responses),
        'reasoning_tokens':sum(row.get('usage',{}).get('output_tokens_details',{}).get('reasoning_tokens',0) for row in responses),
        'latency_seconds':round(sum(row.get('measured_latency_seconds',0) for row in responses),3)}
    write(store.root/'usage-summary.json',summary)


def run_legacy(args):
    store=Store(args.output);orientations=['landscape','portrait'] if args.orientation=='both' else [args.orientation]
    try:
        requirement=input_requirement(args)
        narration=load_narration(args.narration_input) if args.narration_input else None
        if requirement.get('schema_version')!='infofilm.animation-requirement.v1':raise ValueError('Invalid learning objective input')
        if any(not isinstance(requirement.get(k),str) or not requirement[k].strip()
               for k in ('id','subject','visual_message','scene_requirement')):
            raise ValueError('Requirement needs nonempty id, subject, visual_message and scene_requirement')
        config={'planner_url':'https://api.openai.com/v1','reviewer_url':'https://api.openai.com/v1',
                'planner_model':args.model,'reasoning_effort':args.reasoning_effort,'max_output_tokens':args.max_output_tokens}
        frozen={'contract':CONTRACT,'requirement':requirement,'narration':narration,
                'model':config,'orientations':orientations,'review_mode':'none'}
        inputs=store.root/'inputs.json'
        # A pre-audio plan can acquire narration later without replanning its canvas or objects.
        early_narration=narration
        if inputs.exists() and read(inputs).get('narration') is None:
            frozen['narration']=None;early_narration=None
        if inputs.exists() and digest(read(inputs))!=digest(frozen):raise ValueError('Frozen inputs changed; use a new output directory')
        if args.resume and not inputs.exists():raise ValueError('--resume needs an existing experiment')
        attached=store.root/'attached-narration.json'
        if frozen['narration'] is None:
            if attached.exists():
                saved=read(attached)
                if narration and digest(narration)!=digest(saved):raise ValueError('Attached narration changed; use a new output directory')
                narration=load_narration(attached)
            elif narration:write(attached,narration)
        if not narration and args.until not in ('canvas','objects','placement'):
            raise ValueError('Animation needs --narration-input with matching audio and MFA; use --until placement first')
        write(inputs,frozen)
        write(store.root/'requirement.json',requirement)
        if requirement.get('chapter_context'):write(store.root/'chapter-context.json',requirement['chapter_context'])
        contracts_path=store.root/'phase-contracts.json'
        if not contracts_path.exists():
            write(contracts_path,{'schema_version':'infofilm.animation-phase-contracts.v1','canvas':CANVAS_SCHEMA,
                'components':COMPONENT_SCHEMA,'placement':PLACEMENT_SCHEMA,'motion':MOTION_SCHEMA,
                'renderer_rules':GENERAL_MOTION_RULES,'generic_instructions':True})
        contracts=read(contracts_path)
        if not (store.root/'initial-library-catalog.json').exists():write(store.root/'initial-library-catalog.json',catalog(args.library))
        library=read(store.root/'initial-library-catalog.json');models=OpenAIAnimationModels(config,store)
        context={'learning_objective':requirement,'narration':{'transcript':early_narration['transcript'],
                 'duration_seconds':early_narration['duration_seconds']} if early_narration else None}
        sequence_instruction=('Use the frozen narration as the source of the explanation sequence. ' if early_narration else
            'There is no narration yet. Derive the explanation sequence from the learning objective and chapter scope. '
            'Propose understandable teaching beats, not timestamps or an invented audio transcript. ')
        canvases={}
        for orientation in orientations:
            width,height=(1280,720) if orientation=='landscape' else (720,1280)
            viewport={'width':width,'height':height,'fps':30}
            canvases[orientation]=phase(models,store,'canvas-'+orientation,
                'Design the canvas and teaching sequence for this learning objective. You own the entire visual explanation. '
                +sequence_instruction+'Plan visual zones, palette, demonstrations '
                'and any explicit replay. Reserve the bottom 20% for captions. Do not select instances, place individual objects '
                'or plan motion yet. Return the supplied JSON shape, with exact viewport and requirement identity.',
                {**context,'viewport':viewport,'output_schema':contracts['canvas']},
                lambda value:canvas_errors(value,requirement,viewport),5000,args.max_attempts,reviewed=False)
        if args.until=='canvas':
            write(store.root/'state.json',{'state':'canvas_ready','resume_from':'objects','human_review':'pending'});return 0
        components=phase(models,store,'components',
            'Select every object required to execute your accepted canvas and teaching sequence. Reuse the supplied SVG catalog '
            'where actual capabilities fit. Native text, rect and line objects are available and need no SVG request. '
            'For each missing SVG return a structured component request; an operator will hand-create it and resume this exact plan. '
            'Choose objects and their teaching roles only; placement and motion are later phases. '
            'Return the supplied JSON shape. SVG objects have exactly one asset_id or request_id; native objects have neither. '
            'Preserve requirement_id, scene_id and visual_message verbatim.',
            {**context,'canvas_plans':canvases,'component_library':library,'output_schema':contracts['components']},
            lambda value:component_errors(value,requirement),6500,args.max_attempts,reviewed=False)
        effective,assets,resolution=resolve_objects(store,components,args.library,args.bindings)
        if not resolution['ready']:return 3
        if args.until=='objects':
            write(store.root/'state.json',{'state':'objects_ready','resume_from':'placement','human_review':'pending'});return 0
        placement_instruction=('Make support relationships, the launch point, ground, meaningful height and label readability explicit. '
            'Sizes and positions are frame fractions; maintain circular balls in pixel dimensions. ')
        motion_instruction=('Choose the physical parameters and release anchors yourself. Follow the deterministic projectile equations. '
            'The physical simulation progresses across the full specified narration interval; physical_seconds is not '
            'wall-clock film time. Align the bottom of each ball to the visible ground at landing. '
            'Supply useful checkpoint anchors, including mid-flight action. Return the requested animation JSON.')
        if contracts.get('generic_instructions'):
            placement_instruction=('Make meaningful object relationships, scale and label readability explicit for this concept. '
                'Sizes and positions are frame fractions; preserve SVG proportions in pixel dimensions. ')
            motion_instruction=('Select supported motion primitives appropriate to this concept and narration. '
                'Use projectile equations only for freely falling objects, when applicable. '
                'If the required behavior cannot be expressed by the supplied primitives, explain the limitation rather than '
                'silently inventing an unsupported primitive or changing the learning objective. '
                'Supply useful checkpoint anchors covering the teaching beats. Return the requested animation JSON.')
        for orientation in orientations:
            canvas=canvases[orientation];viewport=canvas['viewport']
            placement=phase(models,store,'placement-'+orientation,
                'Place and style every accepted object on your canvas. Establish the initial arrangement before motion. '
                'Use the resolved SVG intrinsic dimensions and pivot: x/y refer to the pivot, not the top-left corner. '
                +placement_instruction+
                'All visible objects must stay in the upper 80% of the canvas. Objects may appear later during animation. '
                'Return the specified placement JSON. Native lines are horizontal in their box and can be rotated. '
                'Define label text and styles here. Do not plan trajectories or timestamps yet.',
                {**context,'canvas_plan':canvas,'accepted_components':effective,'resolved_assets':resolution['assets'],
                 'output_schema':contracts['placement'],'renderer_rules':contracts['renderer_rules']},
                lambda value:placement_errors(value,effective,assets,narration,canvas),6500,args.max_attempts,reviewed=False)
            if args.until=='placement':continue
            animation_schema=copy.deepcopy(contracts['motion'])
            animation_schema.pop('background',None)
            animation_schema['objects']=[{'id':'accepted instance ID','tracks':contracts['motion']['objects'][0]['tracks']}]
            animation=phase(models,store,'animation-'+orientation,
                'Plan all animation and narration synchronization for your accepted canvas, objects and frozen placement. '
                'Each objects entry contains ONLY id and tracks. Object sizes, styles, text and asset identities are frozen '
                'by placement. Every first track starts at that object\'s placement x/y and rotation. Later tracks can move '
                'objects, hide them or explicitly reset demonstrations according to the narration. '
                +motion_instruction,
                {**context,'narration':{'transcript':narration['transcript'],'duration_seconds':narration['duration_seconds'],
                                      'words':narration['words']},'canvas_plan':canvas,
                 'accepted_components':effective,'resolved_assets':resolution['assets'],'placement_plan':placement,
                 'output_schema':animation_schema,'renderer_rules':contracts['renderer_rules']},
                lambda value:animation_errors(value,placement,canvas,effective,assets,narration),10000,args.max_attempts,reviewed=False)
            draft=merge_animation(animation,placement,canvas);write(store.root/('motion-'+orientation+'-plan.json'),draft)
            compiled,errors=compile_motion(draft,effective,assets,narration,viewport['width'],viewport['height'])
            if errors:raise ValueError(str(errors))
            folder=store.root/'renders'/orientation;write(folder/'compiled-plan.json',compiled)
            if args.until=='animation':continue
            props={'plan':compiled,'assets':assets,'audio_src':'data:audio/wav;base64,'+base64.b64encode(Path(narration['audio']).read_bytes()).decode()}
            props_path=write(folder/'props.json',props)
            if not render_reusable(folder,props_path):
                store.event('animation:render','running',{'orientation':orientation,'objects':len(compiled['objects'])})
                browser=configuration(args.config)['browser']
                with (folder/'render.log').open('w') as log:
                    subprocess.run(['node',str(ROOT/'renderer/animation-experiment.cjs'),str(folder),browser,str(props_path)],
                        cwd=ROOT/'renderer',stdout=log,stderr=subprocess.STDOUT,check=True)
            store.event('animation:render','succeeded',{'orientation':orientation})
        write(store.root/'state.json',{'state':args.until+'_ready','review_mode':'none','human_review':'pending',
              'next':'human_visual_and_listening_review' if args.until=='render' else 'resume_next_phase'})
        return 0
    except Exception as exc:
        state_path=store.root/'state.json';previous=read(state_path) if state_path.exists() else {}
        write(state_path,{'state':'failed','error':str(exc),'previous_state':previous,'human_review':'pending'})
        store.event('animation-frontier','failed',{'error':str(exc)})
        raise
    finally:
        usage_summary(store)
        if 'requirement' in locals() and 'narration' in locals():publish_report(store,requirement,narration,orientations)
        store.db.close()


def run(args):
    if getattr(args,'workflow','legacy')=='legacy':return run_legacy(args)
    from .animation_workflow import run as run_workflow
    return run_workflow(args)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',default='gpt-6-luna')
    parser.add_argument('--reasoning-effort',choices=['none','low','medium','high','xhigh','max'],default='high')
    parser.add_argument('--max-output-tokens',type=int,default=24000)
    parser.add_argument('--animation-output-tokens',type=int,default=48000,help='Initial output budget for detailed motion planning; operational, retained plans are reused')
    parser.add_argument('--max-output-recovery-tokens',type=int,default=64000,help='Bounded output-budget recovery ceiling; includes reasoning tokens')
    parser.add_argument('--execution-mode',choices=['chapter','beats'],default='beats',
        help='Plan placement and animation per teaching beat, or keep the original whole-chapter path; operational on resume')
    parser.add_argument('--max-attempts',type=int,default=3,help='Bounded planning/semantic-review attempts per phase')
    parser.add_argument('--reviewer-model',help='Independent semantic review model; defaults to the same Luna model')
    parser.add_argument('--continuity-model',choices=['local','luna'],default='local',help='Narrow MFA anchor mapper; local uses the configured reviewer endpoint')
    parser.add_argument('--local-schema-repair',action=argparse.BooleanOptionalAction,default=True,
        help='After a code failure, try scoped local copy/equivalence checks before another frontier draft')
    parser.add_argument('--legacy',action='store_true',help='Preserve the original pre-audio canvas pilot workflow')
    parser.add_argument('--render-concurrency',type=int,default=6,help='Remotion rendering workers, 1..32; does not change the accepted plans')
    parser.add_argument('--max-render-repairs',type=int,default=2,help='0..3 repairs from measured text/layout failures; upstream plans/audio are retained')
    source=parser.add_mutually_exclusive_group()
    source.add_argument('--situation',help='Plain learning objective, wrapped into a chapter handoff')
    source.add_argument('--chapter-plan','--chapter-context',dest='chapter_plan',type=Path,
        help='Existing chapter-context JSON, or parent chapter-plan JSON with --chapter-id')
    source.add_argument('--requirement',type=Path,help='Structured global-planner handoff JSON')
    parser.add_argument('--chapter-id',help='Chapter to select from a parent plan, e.g. C01')
    parser.add_argument('--narration-input',type=Path,help='Legacy-only matching narration/MFA bundle')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--library',type=Path,default=ROOT/'animation-library')
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--orientation',choices=['landscape','portrait','both'],help='Defaults to chapter film orientation, otherwise landscape')
    parser.add_argument('--until',choices=['teaching','narration','audio','timing','canvas','objects','placement','animation','render'])
    parser.add_argument('--bindings',type=Path)
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    args.workflow='legacy' if args.legacy else 'v2'
    if not 1<=args.max_attempts<=5:parser.error('--max-attempts must be 1..5')
    if not 1<=args.render_concurrency<=32:parser.error('--render-concurrency must be 1..32')
    if not 0<=args.max_render_repairs<=3:parser.error('--max-render-repairs must be 0..3')
    if not 1000<=args.max_output_tokens<=128000:parser.error('--max-output-tokens must be 1000..128000')
    if not 1000<=args.animation_output_tokens<=128000:parser.error('--animation-output-tokens must be 1000..128000')
    if not 1000<=args.max_output_recovery_tokens<=128000:parser.error('--max-output-recovery-tokens must be 1000..128000')
    custom=bool(args.situation is not None or args.requirement or args.chapter_plan)
    # Existing experiments keep their original contract and results. New runs use v2.
    if args.output and (args.output/'inputs.json').exists():
        previous=read(args.output/'inputs.json')
        if previous.get('contract')==CONTRACT:args.workflow='legacy'
        if args.resume and not custom:
            args.requirement=args.output/'requirement.json';custom=True
            if args.orientation is None:
                orientations=previous['orientations'];args.orientation='both' if len(orientations)==2 else orientations[0]
        if args.resume and args.workflow=='v2':
            supplied=lambda flag:any(token==flag or token.startswith(flag+'=') for token in sys.argv[1:])
            model=previous.get('model',{})
            for name,key in [('model','planner_model'),('reviewer_model','reviewer_model'),
                             ('reasoning_effort','reasoning_effort'),('max_output_tokens','max_output_tokens'),
                             ('continuity_model','continuity_model')]:
                if not supplied('--'+name.replace('_','-')) and key in model:setattr(args,name,model[key])
            if not supplied('--library') and previous.get('library'):args.library=Path(previous['library'])
            if not supplied('--config') and (args.output/'speech-config.json').exists():args.config=args.output/'speech-config.json'
    if args.chapter_id and not args.chapter_plan:parser.error('--chapter-id requires --chapter-plan')
    if args.situation is not None and not args.situation.strip():parser.error('--situation must be nonempty')
    if args.until is None:args.until='render'
    if args.workflow=='legacy' and args.until not in ('canvas','objects','placement','animation','render'):parser.error('Legacy phases are canvas, objects, placement, animation, render')
    if args.workflow!='legacy' and args.narration_input:parser.error('The ordered workflow generates its own narration; --narration-input is legacy-only')
    if not custom:
        args.requirement=ROOT/'animation-experiments/gravity/requirement.json'
        if args.workflow=='legacy' and args.narration_input is None:args.narration_input=ROOT/'artifacts/experiments/gravity-animation-llm/input-audio/narration-input.json'
    req=input_requirement(args)
    if args.orientation is None:
        args.orientation=req.get('chapter_context',{}).get('film',{}).get('orientation','landscape')
        if args.orientation not in ('landscape','portrait','both'):parser.error('Invalid film orientation in chapter handoff')
    if args.output is None:
        if custom:
            slug=re.sub(r'[^a-z0-9]+','-',req['subject'].lower()).strip('-')[:48] or 'concept'
            args.output=ROOT/'artifacts/experiments'/('animation-v2-'+slug+'-'+digest(req)[:10])
        else:args.output=ROOT/'artifacts/experiments'/('gravity-animation-luna' if args.workflow=='legacy' else 'gravity-animation-luna-v2')
    return run(args)
