import React, {useEffect, useState} from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, delayRender, continueRender, cancelRender} from 'remotion';
import {landingTime, position, presentationTime, type Physics} from './motion';

type ObjectInstance = {id:string; asset_id:string; role:string; instance:{width?:number;height:number}};
type Asset = {data_uri:string; pivot:[number,number]; sha256:string};
export type GravityPlan = {id:string;title:string;visual_message:string;fps:number;duration_seconds:number;
  width:number;height:number; physics:Physics;objects:ObjectInstance[];
  animation:{first_launch:{start_seconds:number;end_seconds:number};comparison:{start_seconds:number;end_seconds:number};trajectory_intervals:number;vertical_acceleration_arrow:boolean;replay_fade:{start_seconds:number;end_seconds:number}};
  cues:{start:number;end:number;heading:string;text:string}[]};
export type GravityProps = {plan:GravityPlan;assets:Record<string,Asset>};
const clamp={extrapolateLeft:'clamp',extrapolateRight:'clamp'} as const;

export const GravitySeed:React.FC<GravityProps> = ({plan,assets}) => {
  const frame=useCurrentFrame();
  const {fps,width,height}=useVideoConfig();
  const seconds=frame/fps;
  const [handle]=useState(()=>delayRender('Gravity object assets'));
  useEffect(()=>{
    Promise.all(Object.values(assets).map(asset=>new Promise<void>((resolve,reject)=>{
      const image=new Image(); image.onload=()=>resolve(); image.onerror=()=>reject(new Error('Cannot load SVG object')); image.src=asset.data_uri;
    }))).then(()=>continueRender(handle)).catch(cancelRender);
  },[assets,handle]);
  useEffect(()=>{
    const measurement=delayRender('Gravity layout measurement');
    requestAnimationFrame(()=>{
      const boxes=Array.from(document.querySelectorAll('[data-seed-measure]')).map(element=>{
        const b=element.getBoundingClientRect();
        return {role:element.getAttribute('data-seed-measure'),x:b.x,y:b.y,width:b.width,height:b.height,
          overflow:element.scrollWidth>element.clientWidth+2 || element.scrollHeight>element.clientHeight+2};
      });
      const failures:string[]=[];
      for(const b of boxes){
        if(b.x<0 || b.y<0 || b.x+b.width>width+.5 || b.y+b.height>height+.5)failures.push('Outside frame: '+b.role);
        if(b.overflow)failures.push('Text overflow: '+b.role);
      }
      for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++){
        const a=boxes[i],b=boxes[j];
        if(a.x<b.x+b.width && a.x+a.width>b.x && a.y<b.y+b.height && a.y+a.height>b.y)failures.push('Overlap: '+a.role+'/'+b.role);
      }
      console.log('GRAVITY_LAYOUT '+JSON.stringify({frame,width,height,boxes,failures}));
      continueRender(measurement);
    });
  },[frame,width,height]);
  const portrait=height>width;
  const w=portrait?900:1440, h=portrait?770:640;
  const ground=portrait?605:505, origin=portrait?240:350;
  const scale=38;
  const release=ground-plan.physics.release_height_m*scale;
  const comparison=seconds>=plan.animation.comparison.start_seconds-1;
  const clock=comparison?plan.animation.comparison:plan.animation.first_launch;
  const t=presentationTime(seconds,clock.start_seconds,clock.end_seconds,plan.physics);
  const ball=position(plan.physics,t), drop=position(plan.physics,t,true);
  const toScreen=(p:{x:number;y:number})=>({x:origin+p.x*scale,y:ground-p.y*scale});
  const fired=toScreen(ball), dropped=toScreen(drop);
  const cue=plan.cues.find(c=>seconds>=c.start && seconds<c.end) || plan.cues[plan.cues.length-1];
  const elapsed=Math.max(0,Math.min(1,(seconds-clock.start_seconds)/(clock.end_seconds-clock.start_seconds)));
  const landed=seconds>=clock.end_seconds;
  const fade=interpolate(seconds,[plan.animation.replay_fade.start_seconds,(plan.animation.replay_fade.start_seconds+plan.animation.replay_fade.end_seconds)/2,plan.animation.replay_fade.end_seconds],[1,0,1],clamp);
  const object=(role:string)=>{
    const value=plan.objects.find(o=>o.role===role);
    if(!value || !assets[value.asset_id])throw new Error('Unresolved animation object: '+role);
    return value;
  };
  const sprite=(role:string,x:number,y:number,options:{width?:number;height?:number;opacity?:number;rotate?:number}={})=>{
    const obj=object(role),asset=assets[obj.asset_id];
    const width=options.width??obj.instance.width??100,height=options.height??obj.instance.height;
    return <g transform={`translate(${x},${y}) rotate(${options.rotate??0})`} opacity={options.opacity??1}>
      <image href={asset.data_uri} x={-asset.pivot[0]*width} y={-asset.pivot[1]*height} width={width} height={height} preserveAspectRatio="none"/>
    </g>;
  };
  const trail=Array.from({length:plan.animation.trajectory_intervals+1},(_,i)=>{
    const sample=i/plan.animation.trajectory_intervals*landingTime(plan.physics);
    return {sample,point:toScreen(position(plan.physics,sample))};
  }).filter(p=>p.sample<=t+1e-8);
  return <AbsoluteFill style={{background:'#071b29',fontFamily:'Arial, sans-serif',color:'#f4f8fb',padding:portrait?'64px 38px':'35px 58px',boxSizing:'border-box'}}>
    <div data-seed-measure="series" style={{fontSize:portrait?19:15,letterSpacing:4,color:'#8fb0c3',fontWeight:700}}>GRAVITY · ANIMATION SEED</div>
    <div data-seed-measure="title" style={{fontSize:portrait?48:42,fontWeight:700,marginTop:portrait?24:14,lineHeight:1.08}}>{plan.title}</div>
    <div data-seed-measure="subtitle" style={{fontSize:portrait?25:20,color:'#a8c2d3',marginTop:14}}>Horizontal motion + downward acceleration</div>
    <div data-seed-measure="board" style={{position:'absolute',left:portrait?'4%':'3%',right:portrait?'4%':'3%',top:portrait?'29%':'22%',height:portrait?'42%':'52%',opacity:fade}}>
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height="100%" style={{overflow:'visible',display:'block'}}>
        <defs><linearGradient id="sky" x2="0" y2="1"><stop stopColor="#123346"/><stop offset="1" stopColor="#0b2434"/></linearGradient></defs>
        <rect x="25" y="20" width={w-50} height={h-40} rx="24" fill="url(#sky)"/>
        {[.25,.5,.75].map(f=><line key={f} x1="55" x2={w-55} y1={ground-plan.physics.release_height_m*scale*f} y2={ground-plan.physics.release_height_m*scale*f} stroke="#345465" strokeDasharray="3 10" opacity=".55"/>)}
        <rect x={Math.max(55,origin-250)} y={release+78} width={origin-53-Math.max(55,origin-250)} height={ground+13.5-release-78} fill="#244454"/>
        <line x1={Math.max(55,origin-250)} x2={origin-53} y1={release+78} y2={release+78} stroke="#92b0bc" strokeWidth="4"/>
        {sprite('ground',55,ground+13.5,{width:w-110})}
        {sprite('launcher',origin,release)}
        <line x1={origin+20} x2={w-90} y1={release} y2={release} stroke="#7196a9" strokeDasharray="8 9" opacity=".6"/>
        <text x={w-90} y={release-22} textAnchor="end" fontSize="27" fill="#b5cbd8">Same starting height: {plan.physics.release_height_m} m</text>
        {trail.length>1 && <path d={`M ${origin} ${release} Q ${(origin+fired.x)/2} ${release} ${fired.x} ${fired.y}`} fill="none" stroke="#65d7ed" strokeWidth="4" opacity=".55"/>}
        {trail.map((p,i)=><g key={i}>{sprite('launched-ball',p.point.x,p.point.y,{width:13,height:13,opacity:.4})}</g>)}
        {comparison && <>
          <line x1={dropped.x} x2={dropped.x} y1={release} y2={dropped.y} stroke="#f8d08f" strokeWidth="3" strokeDasharray="6 8"/>
          <line x1={dropped.x+20} x2={fired.x-20} y1={fired.y} y2={fired.y} stroke="#dae8f0" strokeDasharray="5 8" opacity={elapsed>.08 && !landed?.6:0}/>
          {sprite('dropped-ball',dropped.x,dropped.y)}
        </>}
        {sprite('launched-ball',fired.x,fired.y)}
        {plan.animation.vertical_acceleration_arrow && elapsed>0 && !landed && fired.y+76<ground && <>
          {sprite('gravity-vector',fired.x+30,fired.y-4,{rotate:90})}
          <text x={fired.x+52} y={fired.y+52} fontSize="25" fill="#ff9da7">g</text>
        </>}
        {landed && <>
          <circle cx={fired.x} cy={ground} r="22" fill="none" stroke="#65d7ed" strokeWidth="3"/>
          {comparison && <circle cx={dropped.x} cy={ground} r="22" fill="none" stroke="#f8d08f" strokeWidth="3"/>}
        </>}
        <text x="65" y="78" fontSize="27" fill="#dbeaf2">{comparison?'LAUNCH + DROP':'HORIZONTAL LAUNCH'}</text>
        <text x={w-65} y="78" textAnchor="end" fontSize="26" fill="#88b3ca">t = {t.toFixed(2)} s</text>
        <circle cx="80" cy={h-68} r="7" fill="#65d7ed"/><text x="100" y={h-59} fontSize="25" fill="#bdcfd9">Launched path</text>
        {comparison && <><circle cx={portrait?400:440} cy={h-68} r="7" fill="#f8d08f"/><text x={portrait?420:460} y={h-59} fontSize="25" fill="#bdcfd9">Dropped path</text></>}
      </svg>
    </div>
    <div data-seed-measure="cue" style={{position:'absolute',left:portrait?45:70,right:portrait?45:70,bottom:portrait?195:92}}>
      <div style={{fontSize:portrait?37:30,lineHeight:1.16,fontWeight:700,color:comparison?'#ffd28f':'#83e5f5'}}>{cue.heading}</div>
      <div style={{fontSize:portrait?26:21,lineHeight:1.4,color:'#bdd1de',marginTop:12}}>{cue.text}</div>
    </div>
    <div data-seed-measure="assumptions" style={{position:'absolute',left:portrait?45:70,right:portrait?45:70,bottom:portrait?65:34,fontSize:portrait?19:16,lineHeight:1.4,color:'#82a4b9'}}>Uniform gravity · Air resistance ignored · Motion slowed for clarity</div>
    <div style={{position:'absolute',left:0,bottom:0,width:`${100*seconds/plan.duration_seconds}%`,height:5,background:'#65d7ed'}}/>
  </AbsoluteFill>;
};
