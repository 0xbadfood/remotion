/** Deterministic motion primitives. All spatial coordinates are frame fractions. */
export type Pair = [number, number];
type BaseTrack = {start:number;end:number;opacity?:number;rotation?:number;rotation_from?:number;rotation_to?:number;cycle_id?:string};
export type HoldTrack = BaseTrack & {kind:'hold';x:number;y:number};
export type LinearTrack = BaseTrack & {kind:'linear';from:Pair;to:Pair};
export type ProjectileTrack = BaseTrack & {
  kind:'projectile';origin:Pair;fraction_per_meter:Pair;
  gravity:number;vx:number;vy:number;physical_seconds:number;
};
export type KeyframeTrack = BaseTrack & {kind:'keyframes';keyframes:{phase:number;x:number;y:number;rotation:number;opacity?:number}[]};
type Pin = {object_id:string;local:Pair};
export type PinLinkTrack = BaseTrack & {kind:'pin_link';a:Pin;b:Pin;local_a:Pair;local_b:Pair};
export type SliderTrack = BaseTrack & {kind:'slider_crank';crank:Pin;axis_origin:Pair;axis_direction:Pair;rod_length_px:number;local_pin:Pair;branch:1|-1};
export type Track = HoldTrack | LinearTrack | ProjectileTrack | KeyframeTrack | PinLinkTrack | SliderTrack;
export type Cycle = {id:string;start:number;end:number;period_seconds:number;phase_offset?:number};
export type Transform = {x:number;y:number;opacity:number;rotation:number};
const clamp = (value:number) => Math.max(0,Math.min(1,value));

/**
 * Later tracks replace earlier tracks, including after their end. Before the
 * first track the object is hidden. Physical time is explicitly supplied by
 * the planner; narration time never silently changes the physical model.
 */
export function computeTransform(tracks:Track[],seconds:number,cycles:Cycle[]=[]):Transform {
  if(!tracks.length)throw new Error('An animation object needs at least one track');
  const sorted=tracks.map((track,index)=>({track,index})).sort((a,b)=>a.track.start-b.track.start || a.index-b.index);
  const started=sorted.filter(({track})=>track.start<=seconds);
  const active=started[started.length-1];
  const track=(active??sorted[0]).track;
  let progress=track.end>track.start?clamp((seconds-track.start)/(track.end-track.start)):(seconds>=track.start?1:0);
  if(track.cycle_id){
    const cycle=cycles.find(value=>value.id===track.cycle_id);
    if(!cycle)throw new Error('Unknown animation cycle: '+track.cycle_id);
    if(!Number.isFinite(cycle.phase_offset??0)||(cycle.phase_offset??0)<0||(cycle.phase_offset??0)>=1)throw new Error('Invalid animation cycle phase_offset: '+cycle.id);
    progress=((cycle.phase_offset??0)+Math.max(0,Math.min(seconds,cycle.end)-cycle.start)/cycle.period_seconds)%1;
  }
  let rotation=track.rotation_from===undefined?(track.rotation??0):track.rotation_from+((track.rotation_to??track.rotation_from)-track.rotation_from)*progress;
  let opacity=active?clamp(track.opacity??1):0;
  let x:number,y:number;
  if(track.kind==='hold')({x,y}=track);
  else if(track.kind==='linear'){
    x=track.from[0]+(track.to[0]-track.from[0])*progress;
    y=track.from[1]+(track.to[1]-track.from[1])*progress;
  }else if(track.kind==='projectile'){
    const t=progress*track.physical_seconds;
    x=track.origin[0]+track.fraction_per_meter[0]*track.vx*t;
    y=track.origin[1]-track.fraction_per_meter[1]*(track.vy*t-0.5*track.gravity*t*t);
  }else if(track.kind==='keyframes'){
    const upper=track.keyframes.findIndex((value,index)=>index>0 && value.phase>=progress);
    const index=upper<0?track.keyframes.length-1:upper;
    const a=track.keyframes[index-1],b=track.keyframes[index],p=(progress-a.phase)/(b.phase-a.phase);
    x=a.x+(b.x-a.x)*p;y=a.y+(b.y-a.y)*p;rotation=a.rotation+(b.rotation-a.rotation)*p;
    opacity=active?clamp((a.opacity??1)+((b.opacity??1)-(a.opacity??1))*p):0;
  }else throw new Error('Linked motion requires the scene resolver');
  return {x,y,opacity,rotation};
}

type SceneObject={id:string;asset_id?:string;tracks:Track[];width:number;height:number;pivot?:Pair};
export function computeSceneTransforms(plan:{width:number;height:number;objects:SceneObject[];cycles?:Cycle[]},seconds:number,
  assets:Record<string,{pivot:Pair}>):Record<string,Transform>{
  const objects=Object.fromEntries(plan.objects.map(object=>[object.id,object]));
  const cache:Record<string,Transform>={},visiting=new Set<string>();
  const landmark=(object:SceneObject,pose:Transform,local:Pair):Pair=>{
    const pivot=object.pivot??assets[object.asset_id??'']?.pivot??[.5,.5];
    const dx=(local[0]-pivot[0])*object.width*plan.width,dy=(local[1]-pivot[1])*object.height*plan.height;
    const theta=pose.rotation*Math.PI/180;
    return [pose.x*plan.width+dx*Math.cos(theta)-dy*Math.sin(theta),pose.y*plan.height+dx*Math.sin(theta)+dy*Math.cos(theta)];
  };
  const evaluate=(id:string):Transform=>{
    if(cache[id])return cache[id];
    if(visiting.has(id))throw new Error('Motion dependency cycle: '+id);
    visiting.add(id);const object=objects[id];
    if(!object)throw new Error('Unknown linked object: '+id);
    const started=object.tracks.filter(track=>track.start<=seconds);
    const track=started[started.length-1]??object.tracks[0];let pose:Transform;
    if(track.kind!=='pin_link' && track.kind!=='slider_crank')pose=computeTransform(object.tracks,seconds,plan.cycles);
    else{
      const reference=(pin:Pin)=>landmark(objects[pin.object_id],evaluate(pin.object_id),pin.local);
      let target:Pair,local:Pair,rotation=track.rotation??0;
      if(track.kind==='pin_link'){
        const a=reference(track.a),b=reference(track.b);
        const dx=(track.local_b[0]-track.local_a[0])*object.width*plan.width;
        const dy=(track.local_b[1]-track.local_a[1])*object.height*plan.height;
        if(Math.abs(Math.hypot(b[0]-a[0],b[1]-a[1])-Math.hypot(dx,dy))>2)throw new Error(id+' rigid pin separation mismatch');
        rotation=(Math.atan2(b[1]-a[1],b[0]-a[0])-Math.atan2(dy,dx))*180/Math.PI;
        target=a;local=track.local_a;
      }else{
        const pin=reference(track.crank),origin:Pair=[track.axis_origin[0]*plan.width,track.axis_origin[1]*plan.height];
        const [ux,uy]=track.axis_direction,dx=pin[0]-origin[0],dy=pin[1]-origin[1];
        const projection=dx*ux+dy*uy,radicand=track.rod_length_px**2-(dx*dx+dy*dy-projection**2);
        if(radicand<-.000001)throw new Error(id+' slider-crank has no solution');
        const distance=projection+track.branch*Math.sqrt(Math.max(0,radicand));
        target=[origin[0]+distance*ux,origin[1]+distance*uy];local=track.local_pin;
      }
      const offset=landmark(object,{x:0,y:0,rotation,opacity:1},local);
      pose={x:(target[0]-offset[0])/plan.width,y:(target[1]-offset[1])/plan.height,rotation,opacity:started.length?(track.opacity??1):0};
    }
    visiting.delete(id);cache[id]=pose;return pose;
  };
  for(const object of plan.objects)evaluate(object.id);
  return cache;
}
