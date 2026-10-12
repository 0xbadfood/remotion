import React, {useEffect, useState} from 'react';
import {AbsoluteFill, Audio, Sequence, cancelRender, continueRender, delayRender, useCurrentFrame, useVideoConfig} from 'remotion';
import {computeSceneTransforms, type Cycle, type Pair, type Track} from './motion';
import {assetImageSource} from './assets';
import {LayoutMeasurement} from './LayoutMeasurement';
import {beatAtFrame,scopedAnimationId,validateChapterPlan,type AnimationChapterPlan,type RenderAnimationPlan} from './chapters';

export type AnimationAsset={data_uri:string;pivot:Pair;sha256:string;capabilities?:string[]};
export type AnimationObject={
  id:string;asset_id?:string;kind:'svg'|'text'|'rect'|'line';width:number;height:number;
  pivot?:Pair;color?:string;text?:string;font_size?:number;z_index?:number;
  stroke_width?:number;arrow_end?:boolean;tracks:Track[];
  label_card?:boolean;
};
export type AnimationPlan={
  schema_version:'infofilm.animation-motion.v1';scene_id:string;visual_message:string;
  fps:number;duration_seconds:number;width:number;height:number;background:string;
  objects:AnimationObject[];captions:{start:number;end:number;text:string}[];
  cycles?:Cycle[];
  checkpoints:{id:string;time:number;reason:string}[];
};
export type AnimationProps={plan:AnimationPlan;assets:Record<string,AnimationAsset>;audio_src?:string};
export type AnimationRenderProps={plan:RenderAnimationPlan;assets:Record<string,AnimationAsset>;audio_src?:string};

export const AnimationCaption:React.FC<{captions:AnimationPlan['captions']}>=({captions})=>{
  const frame=useCurrentFrame(),{fps,width,height}=useVideoConfig();
  const caption=captions.find(cue=>frame/fps>=cue.start&&frame/fps<cue.end);
  return caption?<div data-animation-measure="caption" data-animation-source-id="caption" data-animation-kind="caption" style={{position:'absolute',left:'5%',right:'5%',bottom:'4%',zIndex:1000,
    padding:'12px 20px',fontSize:Math.min(width,height)*.038,lineHeight:1.3,textAlign:'center',borderRadius:12,background:'rgba(4,17,27,0.9)',boxSizing:'border-box'}}>{caption.text}</div>:null;
};

/** The scene contains data only: no evaluated scripts, imported generated JSX or external assets. */
export const AnimationExperiment:React.FC<AnimationProps&{render_captions?:boolean;render_measurement?:boolean;id_namespace?:string}>=({plan,assets,audio_src,render_captions=true,render_measurement=true,id_namespace})=>{
  const frame=useCurrentFrame();
  const {fps,width,height}=useVideoConfig();
  const seconds=frame/fps;
  const transforms=computeSceneTransforms(plan,seconds,assets);
  const [assetHandle]=useState(()=>delayRender('Animation SVG assets'));
  useEffect(()=>{
    const required=plan.objects.filter(object=>object.kind==='svg').map(object=>object.asset_id);
    Promise.all([...new Set(required)].map(id=>new Promise<void>((resolve,reject)=>{
      const asset=id?assets[id]:undefined;
      if(!asset)return reject(new Error('Unresolved animation SVG asset: '+id));
      const image=new Image();
      image.onload=()=>resolve();
      image.onerror=()=>reject(new Error('Cannot load animation SVG asset: '+id));
      image.src=asset.data_uri;
    }))).then(()=>continueRender(assetHandle)).catch(cancelRender);
  },[assets,plan.objects,assetHandle]);

  const content=<AbsoluteFill style={{background:plan.background,fontFamily:'Arial, sans-serif',color:'#f4f8fb'}}>
    {audio_src?<Audio src={audio_src}/>:null}
    {plan.objects.map(object=>{
      const asset=object.asset_id?assets[object.asset_id]:undefined;
      const pivot=object.pivot??asset?.pivot??[.5,.5];
      const transform=transforms[object.id];
      const objectWidth=object.width*width,objectHeight=object.height*height;
      const style:React.CSSProperties={position:'absolute',left:transform.x*width-pivot[0]*objectWidth,top:transform.y*height-pivot[1]*objectHeight,
        width:objectWidth,height:objectHeight,opacity:transform.opacity,zIndex:object.z_index??0,
        transform:`rotate(${transform.rotation}deg)`,transformOrigin:`${pivot[0]*100}% ${pivot[1]*100}%`,boxSizing:'border-box'};
      let content:React.ReactNode;
      if(object.kind==='svg'){
        if(!asset)throw new Error('Unresolved animation asset: '+object.asset_id);
        content=<svg width="100%" height="100%" style={{display:'block'}}><image href={assetImageSource(asset,object.color)} width="100%" height="100%" preserveAspectRatio="none"/></svg>;
      }else if(object.kind==='text'){
        Object.assign(style,{color:object.color??'#f4f8fb',fontSize:object.font_size??Math.min(width,height)*.034,lineHeight:1.2,whiteSpace:'pre-wrap',overflowWrap:'normal',display:'flex',alignItems:'center',justifyContent:'center',textAlign:'center'});
        if(object.label_card)Object.assign(style,{background:'rgba(22,43,57,0.96)',border:'1px solid rgba(131,229,245,0.35)',
          borderRadius:8,padding:6,boxShadow:'0 2px 6px rgba(0,0,0,0.18)'});
        content=object.text??'';
      }else if(object.kind==='rect'){
        Object.assign(style,{background:object.color??'#244454'});
        content=null;
      }else{
        const markerId='animation-arrow-'+(id_namespace?scopedAnimationId(id_namespace,object.id):object.id);
        const lineWidth=object.stroke_width??3;
        content=<svg width="100%" height="100%" viewBox={`0 0 ${objectWidth} ${objectHeight}`} style={{display:'block',overflow:'visible'}}>
          {object.arrow_end?<defs><marker id={markerId} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill={object.color??'#83e5f5'}/></marker></defs>:null}
          <line x1={lineWidth} y1={objectHeight/2} x2={Math.max(lineWidth,objectWidth-lineWidth*(object.arrow_end?5:1))} y2={objectHeight/2} stroke={object.color??'#83e5f5'} strokeWidth={lineWidth} markerEnd={object.arrow_end?`url(#${markerId})`:undefined}/>
        </svg>;
      }
      return <div key={object.id} data-animation-measure={id_namespace?scopedAnimationId(id_namespace,object.id):object.id}
        data-animation-source-id={object.id} data-animation-beat-id={id_namespace} data-animation-kind={object.kind} style={style}>{content}</div>;
    })}
    {render_captions?<AnimationCaption captions={plan.captions}/>:null}
  </AbsoluteFill>;
  return render_measurement?<LayoutMeasurement>{content}</LayoutMeasurement>:content;
};

const chapterClock=(plan:AnimationChapterPlan)=>(frame:number)=>{
  const active=beatAtFrame(plan,frame);
  return active?{beat_id:active.beat.beat_id,source_beat_id:active.beat.source_beat_id??active.beat.beat_id,local_frame:active.local_frame}:null;
};

/** Independent beat geometry, shared narration/captions and one global QA clock. */
export const AnimationChapter:React.FC<Omit<AnimationRenderProps,'plan'>&{plan:AnimationChapterPlan}>=({plan,assets,audio_src})=>{
  validateChapterPlan(plan);
  return <LayoutMeasurement beatAtFrame={chapterClock(plan)}>
    <AbsoluteFill style={{background:plan.background,fontFamily:'Arial, sans-serif',color:'#f4f8fb'}}>
      {audio_src?<Audio src={audio_src}/>:null}
      {plan.beats.map(beat=><Sequence key={beat.beat_id} from={beat.start_frame} durationInFrames={beat.duration_frames} name={beat.beat_id}>
        <AnimationExperiment plan={beat.plan} assets={assets} render_captions={false} render_measurement={false} id_namespace={beat.beat_id}/>
      </Sequence>)}
      <AnimationCaption captions={plan.captions}/>
    </AbsoluteFill>
  </LayoutMeasurement>;
};

export const AnimationRender:React.FC<AnimationRenderProps>=props=>props.plan.schema_version==='infofilm.animation-beat-chapter.v1'
  ?<AnimationChapter {...props} plan={props.plan}/>:<AnimationExperiment {...props} plan={props.plan}/>;
