"""Versioned motion capabilities, shared cycles and declarative mechanical links.

The compiler verifies declared behavior and geometry, not universal scientific truth.
Semantic review owns whether a demonstration explains the supplied objective.
"""
import copy
import math

from .animation_experiment import anchor_time, compile_motion, finite

CAPABILITIES = {
    'translation': 'linear, keyframes, projectile or slider_crank tracks',
    'continuous_rotation': 'rotation_from/rotation_to on hold or linear; unwrapped keyframe angles',
    'synchronized_cycle': 'tracks reference a shared cycle_id; one clock and period for all participants',
    'linked_joints': 'pin_link joins two moving landmarks; slider_crank solves a rigid rod and slider axis',
    'slider_crank': 'rigid crank/rod/slider geometry using the slider_crank track',
    'pin_link': 'rigid connection between two moving landmarks using the pin_link track',
    'projectile_motion': 'projectile equations with declared physical units',
    'visibility': 'hold opacity or interpolated keyframe opacity',
    'keyframes': 'supported keyed poses and visibility; declarative drawing, not a physics solver',
    'linear': 'supported linear translation between two positions',
}


def capability_key(value):
    """Retain saved explicit capability names with descriptive suffixes.

    Only declared renderer names and the historical opacity alias are accepted;
    arbitrary scientific behaviors still require a structured capability gap.
    """
    if not isinstance(value,str):return value
    prefix=value.split(':',1)[0].strip()
    if prefix in CAPABILITIES:return prefix
    if value.strip()=='hold opacity':return 'visibility'
    return value
RULES = """Motion v2 extends hold/linear/projectile. Every object still starts at its frozen placement.
hold and linear may use rotation_from and rotation_to for continuous angular interpolation.
keyframes needs keyframes:[{phase:0..1,x:frame_fraction,y:frame_fraction,rotation:unwrapped_degrees,opacity:0..1}].
At least two keyframes; first phase=0,last=1,strictly increasing. Use unwrapped angles, e.g. 0 to 720.
cycles:[{id,start_anchor,end_anchor,period_seconds,phase_offset:0..1}] define shared clocks. phase_offset
is optional (default zero) and below one; code may supply it to carry a previous beat's cycle phase.
A track with cycle_id must
span exactly that cycle window. Its interpolation progress repeats 0..1 each period. Closing positions,
visibility and rotations modulo 360 must match; do not loop a projectile landing by teleportation.
pin_link tracks need a:{object_id,local:[u,v]},b:{object_id,local:[u,v]},local_a:[u,v],local_b:[u,v].
local means a fractional landmark inside the object's box. pin_link rotates/translates a rigid object
to join those two moving landmarks; its fixed pixel length must match their separation within two pixels.
slider_crank tracks need crank:{object_id,local:[u,v]}, axis_origin:[frame_x,frame_y],
axis_direction:[pixel_direction_x,pixel_direction_y] (unit vector),rod_length_px,local_pin:[u,v],branch:1|-1.
The slider pin follows the intersection of the axis and a circle of rod_length_px about the moving crank pin.
Objects can depend on other objects; dependency cycles are forbidden. Joint geometry uses pixel units,
so consider viewport dimensions and SVG pivots. No evaluated expressions or generated code.
animation_phases:[{id,kind:guided|loop|replay|hold,beat_ids,start_anchor,end_anchor,
cycle_id:null|cycle_ID,transition:continuous|explained_reset,purpose}] describe the teaching flow.
Guided movements follow individual MFA words; loops begin at an explanation/recap anchor, use shared
clocks, and keep all related parts synchronized. Looping is optional; one-time events can replay or hold.
Continuous transitions cannot teleport visible objects. An explained_reset must be meaningful in narration.
motion_requirements:[{behavior,object_ids,importance:essential|optional,reason}] declare required behavior.
capability_gaps:[{behavior,object_ids,importance:essential|optional,reason,lost_explanation}] expose gaps;
never silently substitute a schematic approximation for an essential teaching behavior.
"""


def _pair(v):
    return isinstance(v, list) and len(v) == 2 and all(finite(x) for x in v)


def dependencies(track):
    refs = ([track.get('a'), track.get('b')] if track.get('kind') == 'pin_link'
            else [track.get('crank')] if track.get('kind') == 'slider_crank' else [])
    return [r['object_id'] for r in refs if isinstance(r, dict) and isinstance(r.get('object_id'), str)]


def landmark(obj, transform, local, width, height, assets):
    pivot = obj.get('pivot', assets.get(obj.get('asset_id'), {}).get('pivot', [.5, .5]))
    dx = (local[0] - pivot[0]) * obj['width'] * width
    dy = (local[1] - pivot[1]) * obj['height'] * height
    angle = math.radians(transform['rotation']); c, s = math.cos(angle), math.sin(angle)
    return [transform['x'] * width + dx*c - dy*s, transform['y'] * height + dx*s + dy*c]


def transforms(plan, seconds, assets, cycle_phases=None):
    objects = {o['id']: o for o in plan['objects']}; cycles = {c['id']: c for c in plan.get('cycles', [])}
    memo = {}; visiting = set(); width, height = plan['width'], plan['height']

    def evaluate(identity):
        if identity in memo:return memo[identity]
        if identity in visiting:raise ValueError('Motion dependency cycle: '+identity)
        visiting.add(identity); obj = objects[identity]; tracks = obj['tracks']
        started = [t for t in tracks if t['start'] <= seconds]
        track = started[-1] if started else tracks[0]
        progress = max(0, min(1, (seconds-track['start'])/(track['end']-track['start'])))
        if track.get('cycle_id'):
            cycle = cycles[track['cycle_id']]
            elapsed = max(0, min(seconds, cycle['end'])-cycle['start'])
            progress = (cycle.get('phase_offset', 0) + elapsed / cycle['period_seconds']) % 1
            if cycle_phases and track['cycle_id'] in cycle_phases:progress=cycle_phases[track['cycle_id']]
        opacity = track.get('opacity', 1) if started else 0
        rotation = track.get('rotation', 0)
        if 'rotation_from' in track:rotation = track['rotation_from']+(track['rotation_to']-track['rotation_from'])*progress
        kind = track['kind']
        if kind == 'hold':x, y = track['x'], track['y']
        elif kind == 'linear':
            x, y = [a+(b-a)*progress for a,b in zip(track['from'],track['to'])]
        elif kind == 'projectile':
            t = progress*track['physical_seconds']; sx,sy = track['fraction_per_meter']
            x = track['origin'][0]+sx*track['vx']*t
            y = track['origin'][1]-sy*(track['vy']*t-.5*track['gravity']*t*t)
        elif kind == 'keyframes':
            frames = track['keyframes']; upper = next((i for i,f in enumerate(frames[1:],1) if f['phase']>=progress),len(frames)-1)
            a,b = frames[upper-1],frames[upper]; p = (progress-a['phase'])/(b['phase']-a['phase'])
            x,y,rotation = [a[k]+(b[k]-a[k])*p for k in ('x','y','rotation')]
            opacity = (a.get('opacity',1)+(b.get('opacity',1)-a.get('opacity',1))*p) if started else 0
        elif kind in {'pin_link','slider_crank'}:
            def reference(ref):return landmark(objects[ref['object_id']],evaluate(ref['object_id']),ref['local'],width,height,assets)
            if kind == 'pin_link':
                a,b = reference(track['a']),reference(track['b'])
                local_a,local_b = track['local_a'],track['local_b']
                local_dx=(local_b[0]-local_a[0])*obj['width']*width
                local_dy=(local_b[1]-local_a[1])*obj['height']*height
                if abs(math.hypot(b[0]-a[0],b[1]-a[1])-math.hypot(local_dx,local_dy))>2:
                    raise ValueError(identity+' rigid pin separation differs by more than two pixels')
                rotation = math.degrees(math.atan2(b[1]-a[1],b[0]-a[0])-math.atan2(local_dy,local_dx))
                target,local = a,local_a
            else:
                pin=reference(track['crank']); origin=[track['axis_origin'][0]*width,track['axis_origin'][1]*height]
                ux,uy=track['axis_direction']; dx,dy=pin[0]-origin[0],pin[1]-origin[1]
                projection=dx*ux+dy*uy; perpendicular=dx*dx+dy*dy-projection*projection
                radicand=track['rod_length_px']**2-perpendicular
                if radicand < -1e-6:raise ValueError(identity+' slider-crank has no geometric solution')
                distance=projection+track['branch']*math.sqrt(max(0,radicand))
                target=[origin[0]+distance*ux,origin[1]+distance*uy];local=track['local_pin']
            offset=landmark(obj,{'x':0,'y':0,'rotation':rotation},local,width,height,assets)
            x,y=(target[0]-offset[0])/width,(target[1]-offset[1])/height
        else:raise ValueError('Unsupported motion primitive: '+str(kind))
        result={'x':x,'y':y,'rotation':rotation,'opacity':opacity}
        visiting.remove(identity);memo[identity]=result;return result
    for identity in objects:evaluate(identity)
    return memo


def compile_motion_v2(draft, components, assets, narration, width, height, fps=30):
    """Validate basic contracts through v1, then validate v2 motion at every rendered frame."""
    errors=[]; proxy=copy.deepcopy(draft); ids={o['id'] for o in components['objects']}; cycles={}
    for cycle in draft.get('cycles',[]):
        try:
            identity=cycle['id']
            if not isinstance(identity,str) or not identity or identity in cycles:raise ValueError('Unique cycle IDs required')
            start,end=anchor_time(cycle['start_anchor'],narration),anchor_time(cycle['end_anchor'],narration)
            period=cycle['period_seconds']
            if not start<end or not finite(period) or period<=0 or end-start<period:raise ValueError('Cycle needs room for one complete positive period')
            offset=cycle.get('phase_offset', 0)
            if not finite(offset) or not 0<=offset<1:raise ValueError('Cycle phase_offset must be finite and between zero inclusive and one exclusive')
            cycles[identity]={**cycle,'start':start,'end':end}
        except (KeyError,TypeError,ValueError) as exc:errors.append('Cycle: '+str(exc))
    originals={}; graph={identity:set() for identity in ids}
    for obj in proxy.get('objects',[]):
        if not isinstance(obj,dict) or obj.get('id') not in ids:continue
        identity=obj['id'];raw=copy.deepcopy(obj.get('tracks',[])); originals[identity]=raw
        for index,track in enumerate(obj.get('tracks',[])):
            try:
                kind=track['kind']; start,end=anchor_time(track['start_anchor'],narration),anchor_time(track['end_anchor'],narration)
                if track.get('cycle_id'):
                    c=cycles[track['cycle_id']]
                    if abs(start-c['start'])>1e-6 or abs(end-c['end'])>1e-6:raise ValueError('Cycle tracks must share the complete cycle window')
                    if kind=='projectile':raise ValueError('Projectile loops require an explicitly planned replay, not a cyclic teleport')
                if ('rotation_from' in track) != ('rotation_to' in track):raise ValueError('Rotation requires both endpoints')
                if 'rotation_from' in track and (kind not in {'hold','linear'} or not all(finite(track[k]) for k in ('rotation_from','rotation_to'))):raise ValueError('Invalid continuous rotation')
                if kind=='keyframes':
                    frames=track['keyframes']
                    if not isinstance(frames,list) or len(frames)<2 or frames[0]['phase']!=0 or frames[-1]['phase']!=1:raise ValueError('Keyframes must span phase zero through one')
                    prior=-1
                    for f in frames:
                        if not all(finite(f.get(k)) for k in ('phase','x','y','rotation')) or not prior<f['phase']<=1:raise ValueError('Keyframes need increasing finite phases and transforms')
                        if not finite(f.get('opacity',1)) or not 0<=f.get('opacity',1)<=1:raise ValueError('Invalid keyframe opacity')
                        prior=f['phase']
                    if 'opacity' in track and any(f.get('opacity',1)!=track['opacity'] for f in frames):raise ValueError('Put keyframe opacity on each keyframe; do not mix conflicting track opacity')
                    track.update(kind='hold',x=frames[0]['x'],y=frames[0]['y'],rotation=frames[0]['rotation'])
                elif kind in {'pin_link','slider_crank'}:
                    refs=[track.get('a'),track.get('b')] if kind=='pin_link' else [track.get('crank')]
                    for ref in refs:
                        if not isinstance(ref,dict) or ref.get('object_id') not in ids or not _pair(ref.get('local')) or not all(0<=v<=1 for v in ref['local']):raise ValueError('Link needs an existing object and fractional local landmark')
                    graph[identity].update(dependencies(track))
                    if kind=='pin_link':
                        if not all(_pair(track.get(k)) and all(0<=v<=1 for v in track[k]) for k in ('local_a','local_b')) or track['local_a']==track['local_b']:raise ValueError('Pin link needs two distinct local endpoints')
                    else:
                        if not _pair(track.get('axis_origin')) or not _pair(track.get('axis_direction')) or abs(math.hypot(*track['axis_direction'])-1)>1e-6:raise ValueError('Slider axis needs a unit pixel direction')
                        if not _pair(track.get('local_pin')) or not all(0<=v<=1 for v in track['local_pin']) or not finite(track.get('rod_length_px')) or track['rod_length_px']<=0 or track.get('branch') not in (-1,1):raise ValueError('Invalid slider-crank geometry')
                    track.update(kind='hold',x=obj.get('initial_x',.5),y=obj.get('initial_y',.4),opacity=0)
            except (ValueError,KeyError,TypeError,IndexError) as exc:errors.append(f'{identity}.tracks[{index}]: {exc}')
    visiting=set();done=set()
    def visit(i):
        if i in visiting:raise ValueError('Motion dependencies form a cycle at '+i)
        if i in done:return
        visiting.add(i)
        for ref in graph[i]:visit(ref)
        visiting.remove(i);done.add(i)
    try:
        for i in graph:visit(i)
    except ValueError as exc:errors.append(str(exc))
    plan,basic=compile_motion(proxy,components,assets,narration,width,height,fps);errors.extend(basic)
    if errors:return plan,errors
    for obj in plan['objects']:
        obj['tracks']=[{**{k:v for k,v in t.items() if k not in {'start_anchor','end_anchor'}},
            'start':anchor_time(t['start_anchor'],narration),'end':anchor_time(t['end_anchor'],narration)} for t in originals[obj['id']]]
    plan['cycles']=list(cycles.values())
    # Sample all rendered frames plus exact keyframe, track and loop boundaries.
    times={i/fps for i in range(math.ceil(narration['duration_seconds']*fps))}
    for obj in plan['objects']:
        for t in obj['tracks']:
            times.update([t['start'],t['end']])
            for f in t.get('keyframes',[]):
                if not t.get('cycle_id'):times.add(t['start']+f['phase']*(t['end']-t['start']))
    try:
        for seconds in sorted(times):
            poses=transforms(plan,seconds,assets)
            for obj in plan['objects']:
                pose=poses[obj['id']]
                if pose['opacity']<=.001:continue
                for point in ([0,0],[0,1],[1,0],[1,1]):
                    x,y=landmark(obj,pose,point,width,height,assets)
                    if not -.5<=x<=width+.5 or not -.5<=y<=height*.8+.5:raise ValueError(f"{obj['id']} leaves visual bounds at {seconds:.3f}s")
    except (ValueError,KeyError,TypeError) as exc:errors.append(str(exc))
    # Visible transforms at a loop seam must match. Angular equivalence uses modulo 360.
    try:
        for cycle in cycles.values():
            a=transforms(plan,cycle['start'],assets,{cycle['id']:0})
            b=transforms(plan,cycle['start'],assets,{cycle['id']:1})
            for obj in plan['objects']:
                if not any(t.get('cycle_id')==cycle['id'] for t in obj['tracks']):continue
                first,last=a[obj['id']],b[obj['id']]
                if math.hypot((first['x']-last['x'])*width,(first['y']-last['y'])*height)>1 or abs((first['rotation']-last['rotation']+180)%360-180)>.1 or abs(first['opacity']-last['opacity'])>.01:errors.append(obj['id']+' loop does not close seamlessly')
    except (ValueError,KeyError,TypeError) as exc:errors.append(str(exc))
    return plan,errors
