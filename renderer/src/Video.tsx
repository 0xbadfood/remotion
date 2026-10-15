import React, {useEffect, useState} from 'react';
import {AbsoluteFill, Img, OffthreadVideo, Sequence, staticFile, useCurrentFrame, useVideoConfig, delayRender, continueRender, cancelRender, interpolate} from 'remotion';
import {DepthImage} from './DepthImage';

export type Scene = {id:string; point_id:string; from:number; duration:number; kind:'image'|'flow'|'compare'|'cycle'; src?:string; depth?:string; clip?:string; clipSeconds?:number; backdrop?:string;
  heading:string; summary:string; labels:string[]; details:string[]; banner?:string; needs_review?:boolean; review_tag?:string; copy_density?:'minimal'|'standard'; motion:'push'|'pull'|'drift'|'hold'; index:number; source_ids:string[]; reveals?:number[]};
export type FilmData = {title:string;subject:string;width:number;height:number;fps:number;durationInFrames:number;transitionFrames?:number;depthStrength?:number;
  creativeBrief?:{resolved_mood:string;intensity:string;theme:{background:string;accent:string;text:string}};
  scenes:Scene[]; captions:{start:number;end:number;text:string}[];sources:{id:string;url:string;title:string}[]};

const clamp = {extrapolateLeft:'clamp',extrapolateRight:'clamp'} as const;
const amber='var(--film-accent, #f3b85e)', ink='var(--film-text, #f7f3e9)', teal='var(--film-bg, #082d34)';
let fontsPromise:Promise<void>|undefined;
const loadFonts=()=>{
  if(!fontsPromise)fontsPromise=Promise.all([['InfoSerif','info-serif.ttf'],['InfoSans','info-sans.ttf']].map(async ([family,file])=>{
    const font=await new FontFace(family,`url(${staticFile('fonts/'+file)})`).load();
    (document.fonts as FontFaceSet & {add:(font:FontFace)=>void}).add(font);
  })).then(()=>undefined);
  return fontsPromise;
};

const Diagram:React.FC<{scene:Scene;progress:number;frame:number}> = ({scene,progress,frame}) => {
  const {width,height}=useVideoConfig();
  const portrait=height>width;
  const compare=scene.kind==='compare';
  const n=scene.labels.length;
  return <div style={{position:'absolute',left:portrait?'8%':'45%',right:portrait?'8%':'5.5%',top:portrait?'41%':'22%',bottom:portrait?'17%':'22%',display:'flex',flexDirection:'column',justifyContent:'center',gap:16}}>
    {scene.labels.map((label,i)=>{
      // Each card appears when the narration names it (reveals, frames from the scene start); older timelines: by progress.
      const show=scene.reveals?interpolate(frame,[scene.reveals[i]??0,(scene.reveals[i]??0)+12],[0,1],clamp):Math.min(1,Math.max(0,(progress*n-i*.65)*2+1));
      return <React.Fragment key={i}>
        <div data-measure="diagram-card" style={{position:'relative',border:'2px solid #4a7c82',borderRadius:22,padding:'20px 30px',background:compare && i===1?'#483927':'#103e46',opacity:show,transform:`translateY(${(1-show)*18}px)`}}>
          <div style={{position:'absolute',right:28,top:25,color:amber,fontSize:25,fontWeight:700}}>0{i+1}</div>
          <div data-measure="diagram-label" style={{fontSize:portrait?48:40,fontWeight:700,color:ink,maxWidth:'83%'}}>{label}</div>
          <div data-measure="diagram-detail" style={{fontSize:portrait?32:26,color:'#c5dadd',marginTop:8,lineHeight:1.25,maxWidth:'91%'}}>{scene.details[i]}</div>
        </div>
        {!compare && i<n-1 && <div style={{textAlign:'center',height:18,color:amber,fontSize:34,lineHeight:'18px'}}>↓</div>}
      </React.Fragment>;
    })}
  </div>;
};

const SceneView:React.FC<{scene:Scene;lead:number;brief?:FilmData['creativeBrief'];depthStrength:number}> = ({scene,lead,brief,depthStrength}) => {
  // The sequence starts `lead` frames early so this scene fades in over the previous one; `frame` counts from the cut.
  const raw=useCurrentFrame();
  const frame=raw-lead;
  const fade=lead?interpolate(raw,[0,lead],[0,1],clamp):1;
  const {height,width,fps}=useVideoConfig();
  const portrait=height>width;
  const minimal=scene.kind==='image' && scene.copy_density==='minimal';
  const p=Math.max(0,frame)/Math.max(1,scene.duration-1);
  // The camera moves through the whole visible span, including the fade-in, so it never starts from a stop on screen.
  const c=raw/Math.max(1,scene.duration+lead-1);
  const ease=c*c*(3-2*c);
  const appear=interpolate(frame,[0,Math.min(12,scene.duration-1)],[0,1],clamp);
  const strength=(brief?.resolved_mood==='reflective'?.05:brief?.resolved_mood==='funny'?.1:.08)*(brief?.intensity==='strong'?1.2:1);
  // Aim at the subject where the image composition put it: standard scenes near 72% across (text on the left),
  // minimal scenes centred above the lower-third copy.
  const focus=minimal?{x:50,y:42}:{x:72,y:50};
  const zoom=scene.motion==='pull'?1.03+strength*(1-ease):scene.motion==='hold'?1.02+strength*.25*ease:scene.motion==='drift'?1.03+strength*.6:1.03+strength*ease;
  // Drift pans towards the subject, within the margin the zoom leaves; alternate scenes rise or settle slightly.
  const margin=(zoom-1)*width*.45;
  const x=scene.motion==='drift'?margin*(1-2*ease)*(focus.x>=50?1:-1):0;
  const y=scene.motion==='drift'?(zoom-1)*height*.2*(scene.index%2?1:-1)*(ease-.5):0;
  return <AbsoluteFill style={{background:teal,overflow:'hidden',opacity:fade}}>
    {portrait && minimal && scene.src && <Img src={staticFile(scene.src)} style={{position:'absolute',width:'100%',height:'100%',objectFit:'cover',filter:'blur(28px) brightness(.45)',transform:'scale(1.08)'}}/>}
    {scene.kind==='image' && scene.clip && !portrait
      // A short video made from the accepted still, slowed so that it spans the whole scene (no one speaks on screen).
      ? <OffthreadVideo src={staticFile(scene.clip)} muted playbackRate={Math.max(.3,(scene.clipSeconds??5)/((scene.duration+lead)/fps))}
          style={{position:'absolute',top:0,width:'100%',height:'100%',objectFit:'cover',transform:`scale(${1+(zoom-1)*.5})`,transformOrigin:`${focus.x}% ${focus.y}%`}}/>
      : scene.kind==='image' && scene.src && (scene.depth && !portrait
      // Landscape scenes with a depth map get the 2.5D camera: parallax follows the pan, near layers grow on a push.
      ? <DepthImage src={scene.src} depth={scene.depth} width={width} height={height} focus={focus}
          shift={[x/width*.45*depthStrength, y/height*.45*depthStrength]}
          grow={(scene.motion==='pull'?1-ease:scene.motion==='hold'?.35*ease:scene.motion==='drift'?0:ease)*strength*.9*depthStrength}
          style={{position:'absolute',top:0,width:'100%',height:'100%',transform:`translate(${x}px,${y}px) scale(${zoom})`,transformOrigin:`${focus.x}% ${focus.y}%`}}/>
      : <Img src={staticFile(scene.src)} style={{position:'absolute',top:portrait&&minimal?'8%':0,width:'100%',height:portrait?'63%':'100%',objectFit:portrait&&minimal?'contain':'cover',objectPosition:portrait&&!minimal?'72% 50%':'50% 50%',transform:`translate(${x}px,${y}px) scale(${zoom})`,transformOrigin:`${focus.x}% ${focus.y}%`}}/>)}
    {scene.kind==='image' && <AbsoluteFill style={{background:minimal?'linear-gradient(180deg,rgba(0,0,0,.48) 0%,rgba(0,0,0,0) 24%,rgba(0,0,0,0) 52%,rgba(0,0,0,.8) 100%)':portrait?'linear-gradient(180deg,rgba(5,25,30,.16) 0%,rgba(5,25,30,.08) 38%,rgba(5,25,30,.94) 60%,rgba(5,25,30,1) 68%)':'linear-gradient(90deg,rgba(5,25,30,.97) 0%,rgba(5,25,30,.9) 25%,rgba(5,25,30,.56) 43%,rgba(5,25,30,.06) 73%)'}}/>}
    {scene.kind!=='image' && <>
      <AbsoluteFill style={{background:`radial-gradient(ellipse at 78% 38%,color-mix(in srgb, ${teal}, white 12%) 0%,${teal} 65%)`}}/>
      {scene.backdrop && <Img src={staticFile(scene.backdrop)} style={{position:'absolute',width:'100%',height:'100%',objectFit:'cover',filter:'blur(5px) brightness(.42) saturate(.85)',transform:`scale(${1.06+0.04*p})`,transformOrigin:'50% 50%'}}/>}
      {scene.backdrop && <AbsoluteFill style={{background:`radial-gradient(ellipse at 50% 50%,color-mix(in srgb, ${teal} 45%, transparent) 0%,color-mix(in srgb, ${teal} 80%, transparent) 75%)`}}/>}
      <Diagram scene={scene} progress={p} frame={frame}/>
    </>}
    {scene.kind==='image' && scene.banner && <div data-measure="context-banner" style={{position:'absolute',top:portrait?'10%':'14%',right:portrait?'8%':'6%',maxWidth:portrait?'84%':'45%',padding:'15px 23px',background:amber,color:'#082d34',borderRadius:6,fontSize:portrait?36:29,fontWeight:800,lineHeight:1.18,letterSpacing:1,textTransform:'uppercase',boxShadow:'0 4px 22px rgba(0,0,0,.16)'}}>{scene.banner}</div>}
    {scene.kind==='image' && scene.needs_review && <div data-measure="expert-review-tag" style={{position:'absolute',left:portrait?'8%':96,top:12,maxWidth:portrait?'84%':'55%',fontSize:portrait?26:23,fontWeight:600,lineHeight:1.25,color:'#ffffff',background:'rgba(65,30,6,.9)',padding:'6px 13px',border:'1px solid #d8ac70',borderRadius:5}}>{scene.review_tag || 'AI generated, needs expert review'}</div>}
    <div style={{position:'absolute',left:portrait?'8%':96,top:minimal?'72%':portrait?(scene.kind==='image'?'61%':'14%'):'29%',width:minimal?'84%':portrait?'84%':'36%',opacity:appear,transform:`translateY(${(1-appear)*18}px)`}}>
      {!minimal && <div style={{height:5,width:66,background:amber,marginBottom:30}}/>}
      <div data-measure="heading" style={{fontFamily:minimal?'InfoSans,sans-serif':'InfoSerif,serif',fontSize:minimal?(portrait?56:52):portrait?78:(scene.heading.length>35?72:86),fontWeight:700,lineHeight:1.1,letterSpacing:-1.5,color:ink,textWrap:'balance'}}>{scene.heading}</div>
      {!minimal && scene.summary && <div data-measure="summary" style={{fontSize:portrait?39:33,lineHeight:1.35,color:ink,opacity:.86,marginTop:30,maxWidth:portrait?'100%':640}}>{scene.summary}</div>}
    </div>
    <div style={{position:'absolute',left:portrait?'8%':96,bottom:portrait?180:153,fontSize:portrait?17:19,letterSpacing:portrait?1:2,color:'#9ebebc'}}>ILLUSTRATED EXPLAINER <span style={{marginLeft:24,letterSpacing:1}}> SOURCES: {scene.source_ids.join(' · ')}</span></div>
  </AbsoluteFill>;
};

// Header and progress bar sit above the scenes, so they stay steady while scenes crossfade.
const Chrome:React.FC<{data:FilmData}> = ({data}) => {
  const frame=useCurrentFrame();
  const {height,width}=useVideoConfig();
  const portrait=height>width;
  const total=data.scenes.length;
  const index=Math.max(0,data.scenes.findIndex(s=>frame>=s.from && frame<s.from+s.duration));
  const scene=data.scenes[index];
  const p=scene?Math.min(1,(frame-scene.from)/Math.max(1,scene.duration-1)):0;
  return <>
    <div style={{position:'absolute',top:64,left:96,right:100,display:'flex',justifyContent:'space-between',alignItems:'center'}}>
      <div data-measure="series-label" style={{fontSize:portrait?20:23,fontWeight:700,letterSpacing:portrait?2:5,color:'#bfd5d5',textTransform:'uppercase',maxWidth:'80%'}}>{data.subject}</div>
      <div style={{color:'#bfd5d5',fontSize:25,fontWeight:600}}> {String(index+1).padStart(2,'0')} <span style={{color:'#68898d'}}> / {String(total).padStart(2,'0')}</span></div>
    </div>
    <div style={{position:'absolute',bottom:0,left:0,right:0,height:6,background:'#183c42'}}>
      <div style={{height:'100%',width:`${((index+p)/Math.max(1,total))*100}%`,background:amber}}/>
    </div>
  </>;
};

export const InfoFilm:React.FC<{data:FilmData}> = ({data}) => {
  const frame=useCurrentFrame();
  const [handle]=useState(()=>delayRender('Pinned fonts'));
  useEffect(()=>{
    loadFonts().then(()=>continueRender(handle)).catch(cancelRender);
  },[handle]);
  useEffect(()=>{
    const measurement=delayRender('Measuring final font layout');
    loadFonts().then(()=>{
      requestAnimationFrame(()=>{
        const boxes=Array.from(document.querySelectorAll('[data-measure]')).map(element=>{
          const box=element.getBoundingClientRect();
          const html=element as HTMLElement;
          return {role:element.getAttribute('data-measure'),text:element.textContent,
            x:box.x,y:box.y,width:box.width,height:box.height,
            scrollWidth:html.scrollWidth,clientWidth:html.clientWidth,
            scrollHeight:html.scrollHeight,clientHeight:html.clientHeight};
        }).filter(box=>box.width>0 && box.height>0);
        console.log('INFOFILM_LAYOUT '+JSON.stringify({frame,width:data.width,height:data.height,boxes}));
        continueRender(measurement);
      });
    }).catch(cancelRender);
  },[frame,data.width,data.height]);
  const caption=data.captions.find(c=>frame/data.fps>=c.start && frame/data.fps<c.end);
  const theme=data.creativeBrief?.theme;
  return <AbsoluteFill style={{fontFamily:'InfoSans,sans-serif',background:teal,
    '--film-bg':theme?.background||'#082d34','--film-accent':theme?.accent||'#f3b85e','--film-text':theme?.text||'#f7f3e9'} as React.CSSProperties}>
    {data.scenes.map((scene,i)=>{
      // Crossfade: each scene after the first fades in over the last moments of the previous one (no hard cuts).
      const prev=data.scenes[i-1];
      const lead=prev?Math.max(0,Math.min(data.transitionFrames??0,Math.floor(prev.duration/3),Math.floor(scene.duration/3))):0;
      return <Sequence key={scene.id} from={scene.from-lead} durationInFrames={scene.duration+lead} premountFor={20}>
        <SceneView scene={scene} lead={lead} brief={data.creativeBrief} depthStrength={data.depthStrength??1}/>
      </Sequence>;
    })}
    <Chrome data={data}/>
    {caption && <div style={{position:'absolute',left:'8%',right:'8%',bottom:46,textAlign:'center'}}>
      <span data-measure="caption" style={{display:'inline-block',fontSize:data.height>data.width?37:31,fontWeight:500,lineHeight:1.35,color:'#ffffff',padding:'12px 24px',borderRadius:12,background:'rgba(3,19,23,.84)',maxWidth:'100%'}}>{caption.text}</span>
    </div>}
  </AbsoluteFill>;
};
