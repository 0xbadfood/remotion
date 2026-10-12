"""Deterministic beat clocks, catalogue slices and chapter assembly.

These helpers do not ask a model to invent times or carry state.  Original MFA
identities and immutable drawings are retained; placements belong to a beat.
"""
from __future__ import annotations

import copy
import math
import re

from .animation_experiment import anchor_time, captions, finite
from .animation_frontier import merge_animation
from .animation_motion import compile_motion_v2, dependencies, transforms
from .core import digest

MANIFEST_SCHEMA = 'infofilm.animation-beat-manifest.v1'
SELECTION_SCHEMA = 'infofilm.animation-beat-selection.v1'
CHAPTER_SCHEMA = 'infofilm.animation-beat-chapter.v1'
STATE_SCHEMA = 'infofilm.animation-beat-state.v1'
SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
SELECTION_SHAPE = {
    'schema_version': SELECTION_SCHEMA, 'beat_id': 'accepted teaching beat ID',
    'object_ids': ['existing catalogue instance ID'],
    'object_roles': [{'id': 'selected instance ID', 'reason': 'why needed here',
                     'physics_role': 'optional static|projectile|annotation for this beat'}],
    'transition': {'kind': 'continuous|explained_reset',
                   'retained_object_ids': ['previous visible instance to carry'],
                   'reason': 'why this boundary supports the explanation',
                   'cycle_carries': [{'cycle_id': 'new local cycle ID',
                                     'from_cycle_id': 'previous local cycle ID'}]},
    'component_requests': [],
}


def _identities(rows, key='id'):
    if not isinstance(rows, list):
        raise ValueError('Expected an ordered object list')
    result = {}
    for row in rows:
        identity = row.get(key) if isinstance(row, dict) else None
        if not isinstance(identity, str) or not SAFE_ID.fullmatch(identity) or identity in result:
            raise ValueError('Expected unique safe '+key+' identities')
        result[identity] = row
    return result


def build_manifest(teaching, timing, narration, canvas, components,
                   source_hashes=None, fps=30, word_spans=None):
    """Assign every rendered frame once, holding the previous beat through pauses.

    ``word_spans`` should come from the accepted narration paragraph mapping.
    Timing cues can start inside paragraphs, so they cannot replace that mapping.
    For simple inputs without a paragraph map, cues partition the original word
    order (including leading words in the first beat).
    """
    if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
        raise ValueError('fps must be a positive integer')
    duration = narration.get('duration_seconds')
    if not finite(duration) or duration <= 0:
        raise ValueError('Narration needs a positive duration')
    beats = _identities(teaching['teaching_sequence'])
    cues = _identities(timing['beats'], 'beat_id')
    if set(cues) != set(beats):
        raise ValueError('Timing must cover the accepted teaching beats exactly')
    words = _identities(narration['words'])
    ordered_words = list(words.values())
    if not ordered_words:
        raise ValueError('Beat execution requires MFA words')
    if word_spans is None:
        offsets = [0]
        for identity in list(beats)[1:]:
            when = anchor_time(cues[identity]['start_anchor'], narration)
            index = next((i for i, word in enumerate(ordered_words) if word['start'] >= when-1e-7), len(words))
            offsets.append(index)
        offsets.append(len(words))
        word_spans = {identity: ordered_words[offsets[i]:offsets[i+1]]
                      for i, identity in enumerate(beats)}
    if set(word_spans) != set(beats):
        raise ValueError('Paragraph mapping must cover each accepted beat')
    mapped = []
    spans = {}
    for identity in beats:
        rows = word_spans[identity]
        if not isinstance(rows, list) or not rows:
            raise ValueError(identity+' needs at least one narration word')
        rows = [words[row['id']] for row in rows]
        spans[identity] = rows
        mapped.extend(row['id'] for row in rows)
    if mapped != list(words):
        raise ValueError('Beat paragraph spans must partition all MFA words in teaching order; overlapping spans need explicit subdivision')
    frame_count = math.ceil(duration*fps-1e-8)
    boundaries = [0]
    for identity in list(beats)[1:]:
        # The first frame containing the new spoken beat belongs to that beat.
        boundaries.append(math.floor(spans[identity][0]['start']*fps+1e-8))
    boundaries.append(frame_count)
    if any(a >= b for a, b in zip(boundaries, boundaries[1:])):
        raise ValueError('Each teaching beat needs a positive frame ownership window')
    rows = []
    for index, (identity, beat) in enumerate(beats.items()):
        cue = copy.deepcopy(cues[identity]); start, end = boundaries[index:index+2]
        spoken = spans[identity]
        for name in ('start_anchor', 'end_anchor', 'repeat_anchor'):
            if cue.get(name):
                when = anchor_time(cue[name], narration)
                if not start/fps-1e-7 <= when <= end/fps+1e-7:
                    raise ValueError(identity+' '+name+' leaves its frame ownership window')
        rows.append({'beat_id': identity, 'source_beat_id': identity,
            'position': index+1, 'total': len(beats), 'teaching': copy.deepcopy(beat),
            'start_frame': start, 'duration_frames': end-start,
            'visual_window': {'start': start/fps, 'end': end/fps},
            'narration_window': {'start': spoken[0]['start'], 'end': spoken[-1]['end']},
            'word_ids': [word['id'] for word in spoken], 'timing': cue,
            'rounding': {'boundary': 'floor_next_paragraph_start',
                         'start_offset_seconds': start/fps-spoken[0]['start']},
        })
    manifest = {'schema_version': MANIFEST_SCHEMA, 'scene_id': components['scene_id'],
        'visual_message': components['visual_message'], 'fps': fps,
        'width': canvas['viewport']['width'], 'height': canvas['viewport']['height'],
        'orientation': canvas.get('orientation'), 'duration_seconds': frame_count/fps,
        'source_duration_seconds': duration, 'duration_frames': frame_count,
        'background': canvas['palette']['background'], 'beats': rows,
        'source_hashes': copy.deepcopy(source_hashes or {}),
        'upstream_hashes': {'teaching': digest(teaching), 'timing': digest(timing),
            'narration': digest(narration), 'canvas': digest(canvas), 'components': digest(components)},
    }
    manifest['source_hash'] = digest(manifest)
    return manifest


def manifest_beat(manifest, beat_id):
    return next(row for row in manifest['beats'] if row['beat_id'] == beat_id)


def beat_narration(manifest, beat_id, narration):
    """Rebase selected words while preserving global IDs and original provenance."""
    beat = manifest_beat(manifest, beat_id); offset = beat['start_frame']/manifest['fps']
    duration = beat['duration_frames']/manifest['fps']; ids = set(beat['word_ids'])
    words = []
    for word in narration['words']:
        if word['id'] not in ids:
            continue
        row = copy.deepcopy(word)
        row.update(global_start=word['start'], global_end=word['end'])
        row['start'] = max(0, word['start']-offset)
        row['end'] = min(duration, word['end']-offset)
        if row['end'] < row['start']:
            raise ValueError('Selected MFA word lies outside its beat window: '+word['id'])
        words.append(row)
    if [word['id'] for word in words] != beat['word_ids']:
        raise ValueError('Beat word identities changed since manifest creation')
    result = {key: copy.deepcopy(value) for key, value in narration.items() if key not in {'words', 'transcript', 'duration_seconds'}}
    result.update(words=words, transcript=' '.join(word['word'] for word in words),
        duration_seconds=duration, transcript_sha256=digest(' '.join(word['word'] for word in words)),
        clock={'beat_id': beat_id, 'start_frame': beat['start_frame'], 'fps': manifest['fps'],
               'global_offset_seconds': offset, 'source_duration_seconds': narration['duration_seconds']})
    return result


def selection_errors(value, beat, components, previous_state=None):
    if not isinstance(value, dict):
        return ['Beat selection must be an object']
    errors = []; allowed = {'schema_version','beat_id','object_ids','object_roles','transition','component_requests'}
    if set(value)-allowed:
        errors.append('Selection has unsupported fields: '+', '.join(sorted(set(value)-allowed)))
    if value.get('schema_version') != SELECTION_SCHEMA or value.get('beat_id') != beat['beat_id']:
        errors.append('Selection schema and beat_id must match the accepted beat')
    ids = value.get('object_ids'); known = {obj['id'] for obj in components['objects']}
    if not isinstance(ids, list) or not ids or any(not isinstance(i,str) or i not in known for i in ids) or len(set(ids)) != len(ids):
        return errors+['Selection needs unique existing catalogue instance IDs']
    roles = value.get('object_roles')
    if not isinstance(roles, list) or len(roles) != len(ids) or any(not isinstance(row,dict) or set(row)-{'id','reason','physics_role'} or row.get('id') not in ids or not isinstance(row.get('reason'),str) or not row['reason'].strip() for row in roles) or len({row.get('id') for row in roles if isinstance(row,dict)}) != len(ids):
        errors.append('Selection roles must explain every selected object exactly once')
    else:
        objects = {obj['id']:obj for obj in components['objects']}
        for role in roles:
            physics = role.get('physics_role',objects[role['id']]['physics_role'])
            if physics not in {'static','projectile','annotation'}:
                errors.append('Unknown scoped physics_role: '+role['id'])
            elif objects[role['id']]['kind'] == 'text' and physics != 'annotation':
                errors.append('Native text must keep annotation physics_role: '+role['id'])
    transition = value.get('transition')
    if not isinstance(transition, dict):
        return errors+['Selection needs an explicit transition']
    if set(transition)-{'kind','retained_object_ids','reason','cycle_carries'}:
        errors.append('Unsupported transition fields')
    if transition.get('kind') not in {'continuous','explained_reset'} or not isinstance(transition.get('reason'),str) or not transition['reason'].strip():
        errors.append('Transition needs kind and teaching reason')
    retained = transition.get('retained_object_ids')
    if not isinstance(retained,list) or any(not isinstance(i,str) or i not in ids for i in retained) or len(set(retained)) != len(retained):
        errors.append('Retained objects must be unique selected instance IDs')
    elif retained:
        previous_objects = (previous_state or {}).get('objects',{})
        if not set(retained) <= set(previous_objects):
            errors.append('Retained objects must exist in the preceding computed state')
        else:
            for identity in retained:
                if not previous_objects[identity].get('visible',False):
                    errors.append('Retained objects must be visible in the preceding computed state: '+identity)
                if not set(previous_objects[identity].get('dependency_ids',[])) <= set(retained):
                    errors.append('Retained mechanism needs its whole dependency group: '+identity)
    carries = transition.get('cycle_carries', [])
    if not isinstance(carries,list) or any(not isinstance(row,dict) or set(row) != {'cycle_id','from_cycle_id'} or not all(isinstance(row.get(k),str) and SAFE_ID.fullmatch(row[k]) for k in ('cycle_id','from_cycle_id')) for row in carries):
        errors.append('Cycle carries need safe source and destination cycle IDs')
    elif len({row['cycle_id'] for row in carries}) != len(carries):
        errors.append('Cycle carry destination IDs must be unique')
    else:
        for carry in carries:
            source = (previous_state or {}).get('cycles',{}).get(carry['from_cycle_id'])
            if not source or not isinstance(retained,list) or not set(source['object_ids']) <= set(retained):
                errors.append('Cycle carry needs the preceding clock and complete retained participant group')
    if transition.get('kind') == 'explained_reset' and (retained or carries):
        errors.append('An explained reset cannot claim retained objects or continuous cycles')
    requests = value.get('component_requests', [])
    if requests != []:
        errors.append('First beat execution selects the existing catalogue; new assets require a versioned catalogue delta')
    return errors


def component_subset(components, selection, placement=None, motion=None):
    """Close a selected mechanism over declared and executable object references."""
    ids = selection['object_ids'] if isinstance(selection,dict) else selection
    objects = _identities(components['objects']); selected = set(ids)
    if not selected <= set(objects):
        raise ValueError('Selection contains an unknown catalogue instance')
    graph = {}
    for identity,obj in objects.items():
        refs = obj.get('dependency_ids', [])
        if not isinstance(refs,list) or any(not isinstance(ref,str) for ref in refs):
            raise ValueError('Object dependency_ids must be a list of existing object IDs: '+identity)
        graph[identity] = set(refs)
    if motion:
        for obj in motion.get('objects',[]):
            if obj['id'] not in objects:
                raise ValueError('Motion contains an unknown object: '+obj['id'])
            for track in obj.get('tracks',[]):
                graph[obj['id']].update(dependencies(track))
    if placement:
        for relation in placement.get('relationships',[]):
            if relation.get('relation') in {'pinned_to','connected_to','mechanically_linked','rigid_linked_to'}:
                graph[relation['subject']].add(relation['object'])
    visiting = set(); done = set()
    def visit(identity):
        if identity not in objects:
            raise ValueError('Missing object dependency: '+identity)
        if identity in visiting:
            raise ValueError('Object dependency cycle: '+identity)
        if identity in done:
            return
        visiting.add(identity); selected.add(identity)
        for other in graph[identity]:
            visit(other)
        visiting.remove(identity); done.add(identity)
    for identity in list(selected):
        visit(identity)
    result = copy.deepcopy(components)
    result['objects'] = [copy.deepcopy(obj) for obj in components['objects'] if obj['id'] in selected]
    roles = {row['id']:row for row in selection.get('object_roles',[])} if isinstance(selection,dict) else {}
    for obj in result['objects']:
        if 'physics_role' in roles.get(obj['id'],{}):
            scoped = roles[obj['id']]['physics_role']
            if scoped not in {'static','projectile','annotation'} or (obj['kind'] == 'text' and scoped != 'annotation'):
                raise ValueError('Invalid scoped physics_role for '+obj['id'])
            obj['physics_role'] = scoped
    request_ids = {obj.get('request_id') for obj in result['objects']}
    result['component_requests'] = [copy.deepcopy(request) for request in components.get('component_requests',[]) if request['id'] in request_ids]
    result['selection_provenance'] = {'requested_object_ids': list(ids), 'dependency_object_ids': sorted(selected-set(ids))}
    return result


def terminal_state(plan, assets, beat_id=None):
    """Receipt from the compiler's final displayed frame, including artwork state."""
    frames = round(plan['duration_seconds']*plan['fps']); seconds = max(0,frames-1)/plan['fps']
    poses = transforms(plan,seconds,assets); cycles = {cycle['id']:cycle for cycle in plan.get('cycles',[])}
    result = {'schema_version': STATE_SCHEMA, 'beat_id': beat_id,
        'at_frame': frames-1, 'at_seconds': seconds, 'width': plan['width'], 'height': plan['height'],
        'objects': {}, 'cycles': {}}
    style_fields = ('color','font_size','text','label_card','z_index','stroke_width','arrow_end')
    for obj in plan['objects']:
        asset = assets.get(obj.get('asset_id'),{})
        tracks = [track for track in obj['tracks'] if track['start'] <= seconds]
        active = tracks[-1] if tracks else obj['tracks'][0]
        result['objects'][obj.get('catalogue_id',obj['id'])] = {
            'id': obj.get('catalogue_id',obj['id']), 'kind': obj['kind'], 'asset_id': obj.get('asset_id'),
            'asset_sha256': asset.get('sha256', asset.get('svg_sha256')),
            'width': obj['width'], 'height': obj['height'],
            'pivot': copy.deepcopy(obj.get('pivot',asset.get('pivot',[.5,.5]))),
            'styles': {key:copy.deepcopy(obj[key]) for key in style_fields if key in obj},
            **poses[obj['id']], 'visible': poses[obj['id']]['opacity'] > .001,
            'dependency_ids': dependencies(active), 'cycle_id': active.get('cycle_id'),
        }
    for identity,cycle in cycles.items():
        participants = [identity for identity,obj in result['objects'].items() if obj['cycle_id'] == cycle['id']]
        if not participants:
            continue
        elapsed = max(0,min(seconds,cycle['end'])-cycle['start'])
        result['cycles'][identity] = {'period_seconds': cycle['period_seconds'],
            'phase': (elapsed/cycle['period_seconds']+cycle.get('phase_offset',0)) % 1,
            'object_ids': participants}
    result['sha256'] = digest(result)
    return result


def continuity_errors(previous_state, compiled, assets, transition):
    if not previous_state:
        return [] if not transition.get('retained_object_ids') else ['First beat cannot retain preceding objects']
    if transition.get('kind') == 'explained_reset':
        return []
    errors = []; before = previous_state['objects']; after = transforms(compiled,0,assets)
    current = {obj['id']:obj for obj in compiled['objects']}
    retained = set(transition.get('retained_object_ids',[]))
    carries = {row['cycle_id']:row['from_cycle_id'] for row in transition.get('cycle_carries',[])}
    for identity in retained:
        if identity not in before or identity not in current:
            errors.append('Retained object is absent: '+identity)
    for identity in set(before)&set(current):
        old = before[identity]; obj = current[identity]; pose = after[identity]
        if not old['visible'] or pose['opacity'] <= .001:
            if identity in retained:
                errors.append('Retained object must be visible at both boundary frames: '+identity)
            continue
        asset = assets.get(obj.get('asset_id'),{}); pivot = obj.get('pivot',asset.get('pivot',[.5,.5]))
        styles = {key:copy.deepcopy(obj[key]) for key in ('color','font_size','text','label_card','z_index','stroke_width','arrow_end') if key in obj}
        if (old['kind'] != obj['kind'] or old['asset_id'] != obj.get('asset_id') or
            abs(old['width']-obj['width']) > 1e-7 or abs(old['height']-obj['height']) > 1e-7 or old['pivot'] != pivot or old['styles'] != styles):
            errors.append('Continuous object changes artwork, size, pivot or style; use a new instance or explained reset: '+identity)
        if old.get('asset_sha256') != asset.get('sha256',asset.get('svg_sha256')):
            errors.append('Continuous object changes immutable asset bytes: '+identity)
        if (math.hypot((old['x']-pose['x'])*compiled['width'],(old['y']-pose['y'])*compiled['height']) > 1 or
            abs((old['rotation']-pose['rotation']+180)%360-180) > .1 or abs(old['opacity']-pose['opacity']) > .01):
            errors.append('Visible object jumps across a continuous beat boundary: '+identity)
        if identity in retained and not set(old.get('dependency_ids',[])) <= retained:
            errors.append('Retained mechanism must carry its complete dependency group: '+identity)
        first = next((track for track in obj['tracks'] if track['start'] <= 0), None)
        if old.get('cycle_id') and first and first.get('cycle_id') and carries.get(first['cycle_id']) != old['cycle_id']:
            errors.append('A shared visible repeating mechanism needs an explicit shared cycle carry: '+identity+
                '. Use the declared complete cycle carry, or hold the computed incoming pose first '
                'and begin a separately narrated replay/reset later; do not restart phase zero at beat entry.')
    return errors


def placement_continuity_errors(previous_state, placement, components, assets, transition, canvas):
    """Reject uncarryable frozen placement before asking for detailed animation.

    A continuous beat starts shared previously visible instances at their carried
    geometry. To change their treatment use an explicitly explained reset or a
    different catalogue instance, rather than leaving motion an impossible job.
    """
    if transition.get('kind') == 'explained_reset':
        return []
    if not isinstance(placement,dict) or not isinstance(placement.get('objects'),list):
        return ['Placement must provide objects for continuity validation']
    definitions = {obj['id']:obj for obj in components['objects']}
    plan = {'width':canvas['viewport']['width'],'height':canvas['viewport']['height'],'objects':[],'cycles':[]}
    try:
        for placed in placement['objects']:
            source = definitions[placed['id']]
            obj = {key:copy.deepcopy(value) for key,value in placed.items() if key not in {'x','y','rotation'}}
            obj.update(kind=source['kind'],asset_id=source.get('asset_id'),
                tracks=[{'kind':'hold','start':0,'end':1,'x':placed['x'],'y':placed['y'],
                         'rotation':placed.get('rotation',0),'opacity':1}])
            plan['objects'].append(obj)
        return continuity_errors(previous_state,plan,assets,transition)
    except (KeyError,TypeError,ValueError) as exc:
        return ['Invalid placement continuity geometry: '+str(exc)]


def apply_cycle_carries(motion, previous_state, transition):
    """Give validators and compilation the same code-computed shared cycle phase."""
    draft = copy.deepcopy(motion)
    if not isinstance(draft,dict):
        return draft, ['Motion plan must be an object']
    try:
        cycles = _identities(draft.get('cycles',[]))
    except ValueError as exc:
        return draft, [str(exc)]
    errors = []
    for carry in transition.get('cycle_carries',[]):
        source = (previous_state or {}).get('cycles',{}).get(carry['from_cycle_id']); dest = cycles.get(carry['cycle_id'])
        if not source or not dest:
            errors.append('A carried cycle needs an existing source and destination')
            continue
        try:
            participants = {obj['id'] for obj in draft['objects'] if any(track.get('cycle_id') == carry['cycle_id'] for track in obj['tracks'])}
        except (KeyError,TypeError,AttributeError):
            errors.append('Cycle carry needs valid motion objects and tracks')
            continue
        if participants != set(source['object_ids']) or not participants <= set(transition.get('retained_object_ids',[])):
            errors.append('Carried cycle participants must match the retained dependency group')
        if not finite(dest.get('period_seconds')) or abs(dest['period_seconds']-source['period_seconds']) > 1e-7:
            errors.append('Carried cycle period must match the preceding clock')
        dest['phase_offset'] = source['phase']
    return draft, errors


def compile_beat(motion, placement, canvas, components, assets, narration, beat,
                 previous_state=None, transition=None, fps=30):
    """Compile a scoped plan, injecting only code-owned cycle carry offsets."""
    transition = transition or {'kind':'explained_reset','retained_object_ids':[]}
    carried,errors = apply_cycle_carries(motion,previous_state,transition)
    draft = merge_animation(carried,placement,canvas)
    compiled,bounds = compile_motion_v2(draft,components,assets,narration,
        canvas['viewport']['width'],canvas['viewport']['height'],fps)
    errors.extend(bounds)
    if compiled and not bounds:
        expected = beat['duration_frames']/fps
        if abs(narration['duration_seconds']-expected) > 1e-7:
            errors.append('Beat narration clock differs from owned frames')
        # The code-owned integer window is authoritative.  Floating multiplication
        # inside the legacy compiler can otherwise add a spurious ceiling frame.
        compiled['duration_seconds'] = expected
        compiled.update(beat_id=beat['beat_id'],source_beat_id=beat.get('source_beat_id',beat['beat_id']))
        placed = {obj['id']:obj for obj in placement['objects']}
        for obj in compiled['objects']:
            pose = transforms(compiled,obj['tracks'][0]['start'],assets)[obj['id']]; initial = placed[obj['id']]
            if math.hypot((pose['x']-initial['x'])*compiled['width'],(pose['y']-initial['y'])*compiled['height']) > 1 or abs((pose['rotation']-initial.get('rotation',0)+180)%360-180) > .1:
                errors.append('Object must begin at its beat placement: '+obj['id'])
        errors.extend(continuity_errors(previous_state,compiled,assets,transition))
    return compiled, errors


def namespaced_id(beat_id, identity):
    if not SAFE_ID.fullmatch(beat_id) or not isinstance(identity,str) or not SAFE_ID.fullmatch(identity):
        raise ValueError('Namespace requires a safe beat ID and nonempty source ID')
    # Encoding both lengths makes identities unambiguous even when IDs contain underscores.
    return f'b{len(beat_id)}_{beat_id}__i{len(identity)}_{identity}'


def namespace_plan(plan, beat_id):
    """Optional complete reference transform; SVG asset IDs and MFA IDs stay global."""
    result = copy.deepcopy(plan); ids = {obj['id']:namespaced_id(beat_id,obj['id']) for obj in result['objects']}
    cycles = {cycle['id']:namespaced_id(beat_id,cycle['id']) for cycle in result.get('cycles',[])}
    for obj in result['objects']:
        obj['catalogue_id'] = obj['id']; obj['id'] = ids[obj['id']]
        for track in obj['tracks']:
            if track.get('cycle_id'):
                track['cycle_id'] = cycles[track['cycle_id']]
            for ref in ('a','b','crank'):
                if isinstance(track.get(ref),dict):
                    track[ref]['object_id'] = ids[track[ref]['object_id']]
    for cycle in result.get('cycles',[]):
        cycle['source_id'] = cycle['id']; cycle['id'] = cycles[cycle['id']]
    for checkpoint in result.get('checkpoints',[]):
        checkpoint['source_id'] = checkpoint['id']; checkpoint['id'] = namespaced_id(beat_id,checkpoint['id'])
        if 'object_ids' in checkpoint:
            checkpoint['object_ids'] = [ids[identity] for identity in checkpoint['object_ids']]
    return result


def assemble_chapter(manifest, beat_plans, narration):
    plans = beat_plans if isinstance(beat_plans,dict) else {row.get('beat_id'):row.get('plan',row) for row in beat_plans}
    expected = {row['beat_id'] for row in manifest['beats']}
    if set(plans) != expected:
        raise ValueError('Assembly requires each accepted teaching beat exactly once')
    result = {key:copy.deepcopy(manifest[key]) for key in ('scene_id','visual_message','width','height','fps','duration_seconds','background')}
    result.update(schema_version=CHAPTER_SCHEMA, duration_frames=manifest['duration_frames'],
        captions=captions(narration['words']), beats=[], checkpoints=[], manifest_sha256=manifest['source_hash'])
    for row in manifest['beats']:
        plan = plans[row['beat_id']]
        if any(plan[key] != manifest[key] for key in ('width','height','fps')) or abs(plan['duration_seconds']-row['duration_frames']/manifest['fps']) > 1e-7:
            raise ValueError('Beat viewport, fps or duration differs from execution manifest')
        local = copy.deepcopy(plan); local['captions'] = []
        result['beats'].append({'beat_id':row['beat_id'],'source_beat_id':row['source_beat_id'],
            'start_frame':row['start_frame'],'duration_frames':row['duration_frames'],'plan':local})
        for checkpoint in plan.get('checkpoints',[]):
            result['checkpoints'].append({**copy.deepcopy(checkpoint),
                'id':namespaced_id(row['beat_id'],checkpoint['id']), 'source_id':checkpoint['id'],
                'beat_id':row['beat_id'],'time':row['start_frame']/manifest['fps']+checkpoint['time']})
    return result


def dependency_receipt(beat, *, canvas, components, assets, narration,
                       selection=None, placement=None, motion=None, incoming_state=None):
    """Content-address only actual incoming dependencies, not the whole prior scene."""
    selected = selection or {'object_ids':[obj['id'] for obj in components['objects']], 'transition':{'retained_object_ids':[]}}
    subset = component_subset(components,selected,motion=motion)
    asset_ids = {obj.get('asset_id') for obj in subset['objects'] if obj.get('asset_id')}
    transition = selected.get('transition',{})
    retained = set(transition.get('retained_object_ids',[]))
    old_objects = (incoming_state or {}).get('objects',{})
    if transition.get('kind') == 'continuous':
        retained.update(obj['id'] for obj in subset['objects'] if obj['id'] in old_objects and old_objects[obj['id']].get('visible',True))
    incoming = {identity:copy.deepcopy(old_objects.get(identity)) for identity in sorted(retained)}
    cycle_ids = {obj.get('cycle_id') for obj in incoming.values() if isinstance(obj,dict) and obj.get('cycle_id')}
    cycle_ids.update(carry['from_cycle_id'] for carry in transition.get('cycle_carries',[]))
    incoming_cycles = {identity:copy.deepcopy((incoming_state or {}).get('cycles',{}).get(identity)) for identity in sorted(cycle_ids)}
    base = {'beat':beat,'canvas':canvas,'narration_words':narration['words'],'components':subset,
            'assets':{identity:assets[identity] for identity in sorted(asset_ids)}}
    selection_hash = digest(base)
    placement_hash = digest({'selection':selection_hash,'chosen':selected,'incoming_dependencies':incoming,'incoming_cycles':incoming_cycles})
    motion_hash = digest({'placement':placement_hash,'placement_plan':placement})
    compiled_hash = digest({'motion':motion_hash,'motion_plan':motion})
    return {'schema_version':'infofilm.animation-beat-dependencies.v1',
        'selection':selection_hash,'placement':placement_hash,'motion':motion_hash,
        'compiled':compiled_hash,'render':compiled_hash,'incoming_object_ids':sorted(retained)}


def invalidated_phases(previous, current):
    stages = ('selection','placement','motion','compiled','render')
    return [phase for phase in stages if previous.get(phase) != current.get(phase)]
