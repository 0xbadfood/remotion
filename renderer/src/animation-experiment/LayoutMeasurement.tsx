import React,{useEffect,useRef} from 'react';
import {AbsoluteFill,cancelRender,continueRender,delayRender,useCurrentFrame,useVideoConfig} from 'remotion';

/** One owner reports global frames even when children use Sequence-local clocks. */
export const LayoutMeasurement:React.FC<React.PropsWithChildren<{beatAtFrame?:(frame:number)=>{beat_id:string;source_beat_id?:string;local_frame:number}|null}>>=({children,beatAtFrame})=>{
  const frame=useCurrentFrame(),{fps,width,height}=useVideoConfig();
  const container=useRef<HTMLDivElement>(null);
  useEffect(()=>{
    const handle=delayRender('Animation layout measurement');let cancelled=false;
    document.fonts.ready.then(()=>requestAnimationFrame(()=>{
      if(cancelled)return;
      const boxes=Array.from(container.current?.querySelectorAll<HTMLElement>('[data-animation-measure]')??[])
        .filter(element=>Number(getComputedStyle(element).opacity)>.001).map(element=>{
          const box=element.getBoundingClientRect();
          return {id:element.dataset.animationMeasure,source_id:element.dataset.animationSourceId,
            beat_id:element.dataset.animationBeatId,kind:element.dataset.animationKind,
            x:box.x,y:box.y,width:box.width,height:box.height,
            overflow:element.scrollWidth>element.clientWidth+1||element.scrollHeight>element.clientHeight+1};
        });
      const failures:string[]=[];
      for(const box of boxes){
        if(![box.x,box.y,box.width,box.height].every(Number.isFinite))failures.push('Invalid bounds: '+box.id);
        if(box.x<-.5||box.y<-.5||box.x+box.width>width+.5||box.y+box.height>height+.5)failures.push('Outside viewport: '+box.id);
        if(box.overflow&&(box.kind==='text'||box.kind==='caption'))failures.push('Text overflow: '+box.id);
      }
      console.log('ANIMATION_LAYOUT '+JSON.stringify({frame,seconds:frame/fps,width,height,...beatAtFrame?.(frame),boxes,failures}));
      continueRender(handle);
    })).catch(cancelRender);
    return ()=>{cancelled=true;continueRender(handle);};
  },[frame,fps,width,height,beatAtFrame]);
  return <AbsoluteFill ref={container}>{children}</AbsoluteFill>;
};
