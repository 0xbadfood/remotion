import type {AnimationPlan} from './AnimationExperiment';

export type AnimationBeat={
  beat_id:string;source_beat_id?:string;start_frame:number;duration_frames:number;plan:AnimationPlan;
};
export type AnimationChapterPlan={
  schema_version:'infofilm.animation-beat-chapter.v1';scene_id:string;visual_message:string;
  fps:number;duration_seconds:number;duration_frames:number;width:number;height:number;background:string;
  captions:AnimationPlan['captions'];checkpoints:AnimationPlan['checkpoints'];beats:AnimationBeat[];
};
export type RenderAnimationPlan=AnimationPlan|AnimationChapterPlan;

/** IDs are scoped at rendering, leaving frozen plan references and catalogue IDs intact. */
export function scopedAnimationId(beatId:string,sourceId:string):string {
  const encode=(value:string)=>Array.from(value).map(character=>character.codePointAt(0)!.toString(16)).join('-');
  return `beat-${encode(beatId)}--object-${encode(sourceId)}`;
}

/** Clock ownership is exact in frames, including pauses and the narration tail. */
export function validateChapterPlan(plan:AnimationChapterPlan):void {
  for(const key of ['width','height','fps','duration_seconds'] as const){
    if(!Number.isFinite(plan[key])||plan[key]<=0)throw new Error('Invalid animation metadata: '+key);
  }
  if(!Array.isArray(plan.beats)||!plan.beats.length)throw new Error('An animation chapter needs beats');
  if(!Number.isInteger(plan.duration_frames)||plan.duration_frames<1||Math.abs(plan.duration_seconds*plan.fps-plan.duration_frames)>1e-6)throw new Error('Chapter duration must equal its explicit frame count');
  const ids=new Set<string>();let nextFrame=0;
  for(const beat of plan.beats){
    if(typeof beat.beat_id!=='string'||!beat.beat_id||ids.has(beat.beat_id))throw new Error('Beat IDs must be unique and nonempty');
    ids.add(beat.beat_id);
    if(!Number.isInteger(beat.start_frame)||beat.start_frame!==nextFrame)throw new Error('Beat windows must be contiguous: '+beat.beat_id);
    if(!Number.isInteger(beat.duration_frames)||beat.duration_frames<1)throw new Error('Invalid beat duration: '+beat.beat_id);
    const local=beat.plan;
    if(local.schema_version!=='infofilm.animation-motion.v1')throw new Error('Unsupported nested animation plan: '+beat.beat_id);
    if(local.width!==plan.width||local.height!==plan.height||local.fps!==plan.fps)throw new Error('Beat canvas must match chapter: '+beat.beat_id);
    if(!Number.isFinite(local.duration_seconds)||Math.abs(local.duration_seconds*plan.fps-beat.duration_frames)>1e-6)throw new Error('Beat local duration must equal its frame window: '+beat.beat_id);
    nextFrame+=beat.duration_frames;
  }
  if(nextFrame!==plan.duration_frames)throw new Error('Beat windows must cover the entire chapter');
}

/** Chapter frame ownership is authoritative; legacy rounding tolerates FP noise. */
export function animationDurationFrames(plan:RenderAnimationPlan):number {
  if(plan.schema_version==='infofilm.animation-beat-chapter.v1'){
    validateChapterPlan(plan);return plan.duration_frames;
  }
  return Math.ceil(plan.duration_seconds*plan.fps-1e-8);
}

export function beatAtFrame(plan:AnimationChapterPlan,frame:number):{beat:AnimationBeat;local_frame:number}|null {
  const beat=plan.beats.find(value=>frame>=value.start_frame&&frame<value.start_frame+value.duration_frames);
  return beat?{beat,local_frame:frame-beat.start_frame}:null;
}

/** QA samples both pictures at a cut rather than only one rounded timestamp. */
export function chapterCheckpoints(plan:RenderAnimationPlan):Array<AnimationPlan['checkpoints'][number]&{frame?:number;beat_id?:string;source_id?:string}> {
  if(plan.schema_version==='infofilm.animation-motion.v1')return plan.checkpoints;
  return [...plan.checkpoints,...plan.beats.slice(1).flatMap((beat,index)=>[
    {id:scopedAnimationId(beat.beat_id,'boundary-before'),source_id:'boundary-before',beat_id:plan.beats[index].beat_id,
      time:(beat.start_frame-1)/plan.fps,frame:beat.start_frame-1,reason:'Last visible frame before beat transition'},
    {id:scopedAnimationId(beat.beat_id,'boundary-after'),source_id:'boundary-after',beat_id:beat.beat_id,
      time:beat.start_frame/plan.fps,frame:beat.start_frame,reason:'First visible frame after beat transition'},
  ])];
}
