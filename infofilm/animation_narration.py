"""Luna narration from a frozen teaching sequence, with resumable speech/MFA preparation."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess
import sys

from .animation_experiment import phase,load_narration
from .animation_frontier import publish_report,usage_summary
from .animation_openai import OpenAIAnimationModels
from .core import ROOT,Store,read,write,digest

SCHEMA='infofilm.animation-narration-script.v1'
OUTPUT_SCHEMA={'schema_version':SCHEMA,'requirement_id':'copy input id',
    'visual_message':'copy input visual_message verbatim','title':'Readable title',
    'points':[{'id':'N01','beat_ids':['actual teaching sequence beat ID'],
               'narration':'Only the words the narrator speaks; complete natural sentences'}]}


def script_errors(value,requirement,canvas):
    import re
    if not isinstance(value,dict):return ['Narration script must be a JSON object']
    errors=[]
    for key,wanted in [('schema_version',SCHEMA),('requirement_id',requirement['id']),
                       ('visual_message',requirement['visual_message'])]:
        if value.get(key)!=wanted:errors.append(key+' must match the input')
    points=value.get('points')
    if not isinstance(points,list) or not points:return errors+['Need nonempty narration points']
    beats={b['id'] for b in canvas['teaching_sequence']};covered=set();ids=set()
    for point in points:
        if not isinstance(point,dict):errors.append('Narration point must be an object');continue
        identity=point.get('id')
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',identity) or identity in ids:
            errors.append('Narration point IDs must be unique safe strings')
        else:ids.add(identity)
        refs=point.get('beat_ids')
        if not isinstance(refs,list) or not refs or any(not isinstance(b,str) or b not in beats for b in refs):
            errors.append('Each narration point needs known teaching beat_ids')
        elif len(refs)!=len(set(refs)):errors.append('Duplicate teaching beat_ids')
        else:covered.update(refs)
        if not isinstance(point.get('narration'),str) or not point['narration'].strip():
            errors.append('Each point needs spoken narration text')
    if beats-covered:errors.append('Uncovered teaching beats: '+', '.join(sorted(beats-covered)))
    return errors


def paragraph_errors(value,requirement,teaching):
    """Narration flow stays natural while each paragraph has one visual owner."""
    errors=script_errors(value,requirement,teaching)
    if errors:return errors
    order=[]
    for point in value['points']:
        if len(point['beat_ids'])!=1:
            errors.append('Beat execution needs exactly one teaching beat per narration paragraph')
            continue
        identity=point['beat_ids'][0]
        if not order or order[-1]!=identity:order.append(identity)
    if order!=[beat['id'] for beat in teaching['teaching_sequence']]:
        errors.append('Narration paragraphs must follow accepted teaching order without interleaving beats')
    return errors


def generate_script(store, *, models=None, teaching=None, max_attempts=1, reviewed=False, single_beat_paragraphs=False):
    frozen=read(store.root/'inputs.json');requirement=frozen['requirement']
    orientation=frozen['orientations'][0]
    canvas=teaching or read(store.root/('canvas-'+orientation+'-plan.json'))
    models=models or OpenAIAnimationModels(frozen['model'],store)
    value=phase(models,store,'narration-script',
        'Write the complete spoken explainer for this chapter and its accepted teaching sequence. '
        'You own this narration. Explain the concept clearly for the specified audience, connect causes to effects, '
        'and end with a satisfying takeaway. Follow the chapter scope, exclusions and entry/exit continuity. '
        'Respect supplied facts and assumptions; research gaps are not established facts. '
        'Use natural spoken English, writing numbers as spoken words. Narration fields contain voiceover only: '
        'no stage directions, SSML, timestamps, headings or labels to read mechanically. '
        'Each point should be a coherent spoken paragraph linked to one or more teaching beat IDs. '
        'Cover every accepted beat; combine related beats if that improves flow. Avoid repeatedly introducing the chapter. '
        'The requested runtime is advisory; let the explanation breathe. Actual speech and MFA will own animation timing. '
        'Do not plan object coordinates or animation. Return the supplied JSON shape with exact identities.'+
        (' Beat execution requires exactly one beat ID per paragraph, in accepted teaching order. '
         'Several consecutive paragraphs may explain one beat. Keep transitions natural, without repeating '
         'introductions; paragraph boundaries are visual ownership boundaries, not narration breaks.'
         if single_beat_paragraphs else ''),
        {'learning_objective':requirement,'teaching_sequence':canvas['teaching_sequence'],
         **({'explanation_strategy':canvas.get('explanation_strategy',canvas.get('layout_strategy'))}
            if reviewed else {'layout_strategy':canvas['layout_strategy']}),
         **({'workflow_review_policy':frozen.get('review_policy'),'model_routes':frozen['model']}
            if reviewed else {}),'output_schema':OUTPUT_SCHEMA},
        lambda v:(paragraph_errors if single_beat_paragraphs else script_errors)(v,requirement,canvas),
        5000,max_attempts,reviewed=reviewed)
    transcript='\n\n'.join(p['narration'].strip() for p in value['points'])
    text=store.root/'narration.txt'
    if text.exists() and text.read_text()!=transcript+'\n':raise ValueError('Frozen narration.txt changed')
    text.write_text(transcript+'\n')
    write(store.root/'narration-script-provenance.json',{'script_source':'Luna',
        'model':frozen['model']['planner_model'],'script_json_sha256':digest(value),
        'transcript_sha256':digest(transcript),'semantic_review':'accepted' if reviewed else 'not_run','human_review':'pending'})
    return value


def prepare_audio(store, config, *, owner_project=None):
    output=store.root/'input-audio'
    write(store.root/'narration-state.json',{'state':'speech_and_mfa_running','human_review':'pending'})
    command=[sys.executable,str(ROOT/'scripts/prepare-animation-narration.py'),
        '--script-plan',str(store.root/'narration-script-plan.json'),
        '--config',str(config),'--output',str(output)]
    if owner_project:command+=['--owner-project',str(owner_project)]
    subprocess.run(command,cwd=ROOT,check=True)
    bundle=load_narration(output/'narration-input.json');attached=store.root/'attached-narration.json'
    if attached.exists() and read(attached)!=bundle:raise ValueError('An immutable narration is already attached')
    write(attached,bundle);review_audio=Path('/tmp')/(store.root.name+'-narration.wav')
    shutil.copyfile(bundle['audio'],review_audio)
    write(store.root/'narration-state.json',{'state':'audio_ready','next':'detailed_visual_planning',
        'narration_input':str(output/'narration-input.json'),'duration_seconds':bundle['duration_seconds'],
        'review_audio':str(review_audio),'human_review':'pending'})
    store.event('animation:narration','ready',{'duration_seconds':bundle['duration_seconds'],
        'words':len(bundle['words']),'review_audio':str(review_audio),'alignment':'MFA'})
    return bundle


def run(args):
    store=Store(args.experiment)
    try:
        frozen=read(store.root/'inputs.json')
        generate_script(store)
        write(store.root/'narration-state.json',{'state':'script_ready','next':'speech_and_mfa','human_review':'pending'})
        if args.until=='script':return 0
        prepare_audio(store,args.config)
        return 0
    except Exception as exc:
        write(store.root/'narration-state.json',{'state':'failed','error':str(exc),
            'saved_script_retained':(store.root/'narration-script-plan.json').exists(),'human_review':'pending'})
        store.event('animation:narration','failed',{'error':str(exc)})
        raise
    finally:
        usage_summary(store)
        if (store.root/'inputs.json').exists():
            frozen=read(store.root/'inputs.json')
            bundle=read(store.root/'attached-narration.json') if (store.root/'attached-narration.json').exists() else None
            publish_report(store,frozen['requirement'],bundle,frozen['orientations'])
        store.db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment',required=True,type=Path)
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--until',choices=['script','audio'],default='audio')
    return run(parser.parse_args())
