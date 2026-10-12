"""Adapt an animation chapter into the existing checked chapter delivery contract."""
from pathlib import Path
from types import SimpleNamespace
import copy
import shutil
import sys

from .core import ROOT, Store, configuration, digest, read, write
from .production_state import ProductionPaused


def provider_settings(config):
    settings=copy.deepcopy(config.get('animation_provider') or read(ROOT/'animation-provider.json'))
    if settings.get('schema_version')!='infofilm.animation-provider.v1':raise ValueError('Unknown animation provider schema')
    for key in ('model','reviewer_model','library'):
        if not isinstance(settings.get(key),str) or not settings[key].strip():raise ValueError('Animation provider needs '+key)
    for key,low,high in [('max_output_tokens',1000,128000),('max_attempts',1,5),('max_render_repairs',0,3),('render_concurrency',1,32)]:
        if type(settings.get(key)) is not int or not low<=settings[key]<=high:raise ValueError('Invalid animation provider '+key)
    if settings.get('continuity_model') not in {'local','luna'}:raise ValueError('Invalid animation continuity mapper')
    # Execution is operational: old snapshots keep their approved teaching,
    # narration and assets while resuming through the bounded beat workflow.
    settings.setdefault('execution_mode','beats')
    if not isinstance(settings['execution_mode'],str) or settings['execution_mode'] not in {'chapter','beats'}:
        raise ValueError('Invalid animation execution mode')
    if 'local_schema_repair' in settings and type(settings['local_schema_repair']) is not bool:raise ValueError('Invalid local_schema_repair switch')
    for key in ('animation_output_tokens','max_output_recovery_tokens'):
        if key in settings and (type(settings[key]) is not int or not 1000<=settings[key]<=128000):raise ValueError('Invalid animation provider '+key)
    if settings.get('reasoning_effort') not in {'none','low','medium','high','xhigh','max'}:raise ValueError('Invalid animation reasoning effort')
    return settings


def library_path(settings):
    path=Path(settings['library']).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT/path).resolve()


def ground_chapter(config,store,context,subject):
    """Run the existing local chapter research/evidence path before handing context to Luna."""
    from .pipeline import research
    from .planning import make_evidence
    from .llm import Models
    record=store.root/'animation-grounding.json';path=store.root/'animation-context.json'
    if record.exists():
        saved=read(record)
        if saved['parent_context_sha256']!=digest(context):raise ValueError('Animation chapter handoff changed after grounding')
        for name,key in [('sources.json','sources_sha256'),('evidence-final.json','evidence_sha256'),('animation-context.json','context_sha256')]:
            if digest(read(store.root/name))!=saved[key]:raise ValueError('Frozen animation grounding changed: '+name)
        for stage in ('research','evidence'):store.event('pipeline:'+stage,'succeeded',{'reused':True})
        return read(path),path
    store.event('pipeline:research','running',{'production_kind':'animation'})
    sources=research(config,store,context['research_subject'])
    write(store.root/'sources.json',sources)
    store.event('pipeline:research','succeeded',{'sources':len(sources['results'])})
    store.event('pipeline:evidence','running')
    evidence=make_evidence(Models(config,store),sources,subject,experimental=True,chapter_context=context)
    write(store.root/'evidence-final.json',evidence)
    grounded=copy.deepcopy(context);grounded['chapter_evidence']=evidence
    # Full fetched pages remain in the chapter archive. Facts, qualifications and source URLs are the handoff.
    grounded['chapter_sources']=[{'id':f'S{i}','url':s['url'],'title':s.get('title') or s['url']}
                                for i,s in enumerate(sources['results'],1)]
    write(path,grounded)
    write(record,{'parent_context_sha256':digest(context),'sources_sha256':digest(sources),
        'evidence_sha256':digest(evidence),'context_sha256':digest(grounded)})
    store.event('pipeline:evidence','succeeded',{'facts':len(evidence['facts'])})
    return grounded,path


def asset_waits(project):
    """Read and resolve cached demands without model calls or artifact mutations."""
    from .animation_objects import resolve_components, registry_snapshot
    project=Path(project);roots=[]
    status=project/'longform-status.json'
    if status.exists():
        for row in read(status)['chapters']:
            if row.get('status') in {'waiting_for_assets','waiting_for_capabilities'}:
                roots.append((row['chapter']['id'],Path(row['project'])/'animation'))
    elif (project/'animation/state.json').exists():roots=[('C01',project/'animation')]
    result=[]
    for chapter,root in roots:
        state=read(root/'state.json');kind=state.get('state')
        if kind=='waiting_for_components':
            frozen=read(root/'inputs.json')
            selected=Path(state.get('component_plan') or 'components-plan.json')
            selected=selected if selected.is_absolute() else root/selected
            selected=selected.resolve()
            if not selected.is_relative_to(root.resolve()):raise ValueError('Animation component checkpoint leaves its chapter')
            components=read(selected)
            library=Path(frozen['library'])
            try:_,faults=resolve_components(components,library,registry_snapshot(library))
            except (OSError,ValueError) as exc:faults=[{'reason':str(exc),'description':'Asset cache failed its integrity check'}]
            result.append({'chapter':chapter,'kind':'assets','ready':not faults,
                'requests':read(root/'object-requests.json')['requests'], 'unresolved':faults})
        elif kind=='waiting_for_motion_capabilities':
            result.append({'chapter':chapter,'kind':'capabilities','ready':False,
                'requests':read(root/'motion-capability-requests.json')['requests']})
    return result


def export_audio(project,animation,context,target):
    """Expose the already verified script/MFA in the normal chapter checkpoint format."""
    from .animation_experiment import load_narration
    from .media import audio_info
    bundle=load_narration(animation/'attached-narration.json')
    original=read(animation/'narration-script-plan.json')
    beats={b['id']:b for b in read(animation/'teaching-plan.json')['teaching_sequence']}
    facts=context.get('chapter_evidence',{}).get('facts') or [
        {'id':a['id'],'claim':a['claim'],'source_ids':a['source_ids']} for a in context.get('orientation_anchors',[])]
    script={'title':context['chapter']['title'],'subject':context['film']['subject'],
        'points':[{**p,'heading':beats[p['beat_ids'][0]]['message'],'fact_ids':[f['id'] for f in facts]}
                  for p in original['points']]}
    timing=read(bundle['timing']);folder=project/f'film-{target}'
    write(folder/'script.json',script);(folder/'script.txt').write_text(bundle['transcript']+'\n')
    write(folder/'timing.json',timing)
    info=audio_info(bundle['audio'])
    film={'requested_seconds':target,'actual_seconds':bundle['duration_seconds'],'point_count':len(script['points']),
        'script':script,'audio':bundle['audio'],'audio_info':info,'timing':timing,
        'audit':{'status':'passed','method':'Reused native point speech audits and verified MFA',
                 'receipt':str(animation/'input-audio/audio-info.json')},'human_listening_review':'pending'}
    write(folder/'audio-selection.json',{k:v for k,v in film.items() if k!='timing'})
    write(project/'project.json',{'subject':context['film']['subject'],'durations':[target], 'generation_mode':'chapter'})
    if not (project/'evidence-final.json').exists():write(project/'evidence-final.json',{'title':script['title'],'facts':facts})
    if not (project/'sources.json').exists():write(project/'sources.json',{'results':[{'url':s['url'],'title':s.get('title'),
        'raw_content':'\n\n'.join(s.get('excerpts',[]))} for s in context.get('orientation_sources',[])]})
    return film


def package_video(config,store,animation,film,orientation,music):
    """Scale to chapter delivery size and mux the existing audited master, without Remotion replanning."""
    from .longform_video import ffmpeg
    from .media import audio_info
    folder=animation/'renders'/orientation;receipt=read(folder/'render-results.json')
    if not receipt.get('complete') or receipt['video'].get('layout_failures'):raise ValueError('Animation renderer has not passed its layout gate')
    picture=folder/'animation.mp4';props=folder/'props.json'
    from .animation_experiment import render_reusable
    if not render_reusable(folder,props):raise ValueError('Animation render has changed since verification')
    width,height=(1080,1920) if orientation=='portrait' else (1920,1080)
    width,height=config.get('assembly_width',width),config.get('assembly_height',height)
    fps=config['fps'];frames=round(film['actual_seconds']*fps)
    identity=digest({'picture':digest(picture.read_bytes()),'master':music['master_audio_info']['audio_sha256'],
        'dimensions':[width,height],'fps':fps,'policy':'animation-chapter-delivery-v1'})
    root=store.root/'videos'/(str(film['requested_seconds'])+'s-'+orientation)/identity[:16]
    root.mkdir(parents=True,exist_ok=True);video=root/'video.mp4';qa_path=root/'qa.json'
    if qa_path.exists() and read(qa_path).get('passed') and video.exists():
        if digest(video.read_bytes())!=read(qa_path)['video_sha256']:raise ValueError('Animation delivery changed after QA')
        return read(root/'output.json')
    ffmpeg(['-i',picture,'-i',music['master_audio'],'-map','0:v:0','-map','1:a:0',
        '-vf',f'scale={width}:{height},setsar=1','-r',fps,'-frames:v',frames,
        '-c:v','libx264','-preset','fast','-crf','19','-pix_fmt','yuv420p',
        '-c:a','aac','-b:a','192k','-video_track_timescale','90000',video])
    master=root/'master.wav';shutil.copyfile(music['master_audio'],master)
    source={**audio_info(master),'audio_file':'master.wav'}
    write(root/'source.json',source)
    sys.path.insert(0,config['filmora_root'])
    from backend.qa import run_qa
    package=write(root/'qa-package.json',{'source':source})
    qa_timeline=write(root/'qa-timeline.json',{'width':width,'height':height,'fps':fps,'durationInFrames':frames})
    qa_path.unlink(missing_ok=True)
    try:qa=run_qa(video,package,qa_timeline,root,qa_path)
    except SystemExit as exc:raise RuntimeError('Animation chapter delivery failed media QA') from exc
    if not qa.get('passed'):raise RuntimeError('Animation chapter delivery failed media QA')
    compiled=read(folder/'compiled-plan.json')
    timeline=write(root/'timeline.json',{'data':{'title':film['script']['title'],'subject':film['script']['subject'],
        'width':width,'height':height,'fps':fps,'durationInFrames':frames,
        'scenes':[{'id':compiled['scene_id'],'kind':'animation','from':0,'duration':frames,'source_ids':[]}],
        'captions':compiled['captions']}})
    layout=root/'layout';layout.mkdir(exist_ok=True)
    sample=receipt['checkpoints'][0];poster=layout/'poster.png'
    ffmpeg(['-i',folder/sample['file'],'-vf',f'scale={width}:{height}','-frames:v',1,poster])
    write(layout/'layout-results.json',{'passed':True,'samples':[{'file':'poster.png'}],
        'method':'All native animation frames measured, then proportional scaling',
        'source_receipt_sha256':digest(receipt),'all_frames_measured':True})
    output={'requested_seconds':film['requested_seconds'],'actual_seconds':film['actual_seconds'],
        'point_count':film['point_count'],'orientation':orientation,'video':str(video),'timeline':str(timeline),
        'qa':str(qa_path),'music_engine':music['engine'],'production_kind':'animation'}
    write(root/'output.json',output);return output


def create_animation(args):
    from .animation_workflow import run as run_workflow
    from .longform import chapter_creative
    from .music import prepare_music
    from .narrators import select_reference
    config=configuration(args.config);settings=provider_settings(config);project=Path(args.project).resolve()
    context=read(args.chapter_context);target=args.durations[0];animation=project/'animation'
    config=select_reference(config,reference_audio=args.reference_audio,reference_transcript=getattr(args,'reference_transcript',None))
    # The current animation speech runner uses the native VibeVoice path.
    if getattr(args,'tts_engine','vibevoice')!='vibevoice':raise ValueError('Animation chapters currently require VibeVoice narration')
    config_path=write(project/'animation-config.json',config)
    store=Store(project)
    try:
        context,context_path=ground_chapter(config,store,context,args.subject)
        orientations=['landscape','portrait'] if args.orientation=='both' else [args.orientation]
        brief=chapter_creative(context['shared_creative'],context['chapter'],getattr(args,'mood','auto'))
        write(project/'creative-brief.json',brief)
        def on_stage(stage):
            if stage=='audio':
                export_audio(project,animation,context,target)
            store.event('pipeline:'+stage,'succeeded',{'production_kind':'animation'})
            stages=['teaching','narration','audio','timing','canvas','objects','placement','animation','render']
            if stage!='render':store.event('pipeline:'+stages[stages.index(stage)+1],'running')
        def on_beat(details):
            # Keep the existing stage/event envelope so parent logs and web
            # progress receive the narrower beat scope without a new protocol.
            phase=details.get('phase','animation')
            stage='objects' if phase=='selection' else phase
            state=details.get('state','running')
            store.event('pipeline:'+stage,state,{'production_kind':'animation','execution_mode':'beats',**details})
        store.event('pipeline:teaching','running',{'production_kind':'animation','model':settings['model']})
        workflow=SimpleNamespace(**{k:v for k,v in settings.items() if k not in {'schema_version','library'}},
            output=animation,chapter_plan=context_path,chapter_id=None,situation=None,requirement=None,
            orientation=args.orientation,config=config_path,library=library_path(settings),bindings=None,
            resume=(animation/'inputs.json').exists(),until='audio' if args.until=='audio' else 'render',
            queue_owner_project=getattr(args,'queue_owner_project',None),on_stage=on_stage,on_beat=on_beat)
        code=run_workflow(workflow)
        if code in {3,4}:
            state=read(animation/'state.json');status='waiting_for_assets' if code==3 else 'waiting_for_capabilities'
            store.event('pipeline:objects' if code==3 else 'pipeline:animation',status,{'state':state})
            raise ProductionPaused(status,'Animation chapter is saved and waiting for '+('SVG assets' if code==3 else 'a motion capability'),state)
        if code:raise RuntimeError('Animation workflow exited '+str(code))
        film=export_audio(project,animation,context,target)
        if args.until=='audio':return
        store.event('pipeline:music','running',{'engine':args.music_engine})
        music=prepare_music(config,store,film,brief,args.music_engine)
        store.event('pipeline:music','succeeded')
        store.event('pipeline:video','running')
        outputs=[package_video(config,store,animation,film,o,music) for o in orientations]
        write(project/'delivery.json',{'subject':args.subject,'generation_mode':'chapter','production_kind':'animation',
            'outputs':outputs,'human_acceptance':'pending'})
        store.event('pipeline:video','succeeded',{'orientations':orientations})
    finally:store.db.close()
