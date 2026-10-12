"""Small JSON contracts between the CLI's directors, image grounder and compiler."""
from __future__ import annotations

import copy
import math
import re

from .image_animation_controllers import (
    MOTION_KINDS, PRESENTATION_KINDS, box, compile_scene, compile_timeline,
    controller_capabilities, point,
)

ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
PARENT_SCHEMA = {'schema_version': 'infofilm.image-animation-film.v1',
    'film': {'subject': 'copy requested subject', 'title': 'readable title', 'audience': 'supplied audience',
             'central_question': 'question the film answers'},
    'chapters': [{'id': 'C01', 'title': 'chapter title', 'objective': 'learning objective',
        'advisory_seconds': 'positive number; duration is guidance',
        'scope': {'description': 'owned questions and boundaries', 'start_year': None, 'end_year': None},
        'source_ids': ['S1'], 'research_queries': ['scoped research questions'], 'exclude': ['already covered topics'],
        'entry_context': 'what viewers already know', 'exit_handoff': 'connection to next chapter',
        'production_kind': 'animated|illustrated', 'visual_direction': 'useful demonstration or imagery'}]}
CANVAS_SCHEMA = {'schema_version': 'infofilm.image-animation-canvases.v1',
    'canvases': [{'id': 'K01', 'beat_ids': ['actual accepted teaching beat'],
        'visual_message': 'what this canvas must convey',
        'controller_type': 'one safe controller ID, e.g. orbit_path or static_canvas; an unknown ID requests new code',
        'prompt': 'self-contained positive scene description; code composites labels',
        'component_roles': [{'role': 'controller role', 'description': 'moving component',
                              'required_landmarks': ['anchor name']}],
        'limitations': ['nonessential caveats and unavailable future options; not executable components']} ]}
GROUNDING_SCHEMA = {'schema_version': 'infofilm.image-animation-grounding.v1',
    'canvas_id': 'copy canvas id', 'background_prompt': 'edit the supplied image into a clean static layer',
    'parts': [{'id': 'safe part id', 'role': 'requested role', 'prompt': 'complete isolated part on transparent background',
               'bbox_xyxy_px': [0, 0, 1, 1], 'landmarks': {'anchor_name': [0, 0]}, 'z_index': 5}],
    'scene': {'schema_version': 'infofilm.image-animation-scene.v1', 'canvas': {'width': 1536, 'height': 864},
        'assets': {'background': {'path': 'background'}, 'part_id': {'path': 'part_id', 'bbox_xyxy_px': [0, 0, 1, 1],
            'landmarks': {}, 'z_index': 5}},
        'controller': {'type': 'supported controller', 'parameters': {}, 'bindings': {}},
        'landmarks': {}, 'incomplete_flags': []},
    'uncertainties': [{'kind': 'unresolved_geometry|caveat', 'field': 'affected geometry or asset field',
                      'description': 'Unresolved shape/anchor, or an intentional nonessential explanatory caveat'}]}
MOTION_SCHEMA = {'schema_version': 'infofilm.image-animation-cues.v1', 'beat_id': 'copy teaching beat',
    'actions': [{'id': 'safe action id', 'kind': 'hold|move|replay|loop|stroke|flight|orbit|highlight|label|spark',
        'start_word_id': 'actual word id', 'end_word_id': 'actual word id',
        'value_start': 'optional finite scalar; defaults to inherited state',
        'value_end': 'required finite scalar for moving kinds; omit for holds and labels',
        'loops': 'optional positive integer', 'label': 'short label card',
        'purpose': 'why this action belongs to these spoken words'}]}


def safe(value):
    return isinstance(value, str) and bool(ID.fullmatch(value))


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _landmark_names(value):
    return isinstance(value, list) and all(safe(name) for name in value) and len(set(value)) == len(value)


def parent_errors(value, subject, source_ids, mode):
    if not isinstance(value, dict) or value.get('schema_version') != PARENT_SCHEMA['schema_version']:
        return ['Wrong parent planning contract']
    errors = []
    film = value.get('film', {})
    if not isinstance(film, dict) or film.get('subject') != subject or not nonempty(film.get('title')):
        errors.append('Film must retain the supplied subject and provide a title')
    rows = value.get('chapters')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
        return errors + ['Need 1–12 coherent chapters']
    if mode == 'single' and len(rows) != 1:
        errors.append('Single mode requests one chapter')
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            errors.append('Chapter must be an object'); continue
        identity = row.get('id')
        if not safe(identity) or identity in seen:
            errors.append('Chapter IDs must be unique safe strings')
        seen.add(identity if isinstance(identity, str) else None)
        for key in ('title', 'objective', 'entry_context', 'exit_handoff', 'visual_direction'):
            if not nonempty(row.get(key)):
                errors.append(f'{identity} needs {key}')
        scope = row.get('scope')
        if not isinstance(scope, dict) or not nonempty(scope.get('description')):
            errors.append(f'{identity} needs its owned scope')
        seconds = row.get('advisory_seconds')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            errors.append(f'{identity} needs positive advisory seconds')
        refs = row.get('source_ids')
        if not isinstance(refs, list) or not refs or any(not isinstance(s, str) or s not in source_ids for s in refs):
            errors.append(f'{identity} source_ids must reference the supplied evidence')
        if row.get('production_kind') not in {'animated', 'illustrated'}:
            errors.append(f'{identity} production_kind must be animated or illustrated')
    return errors


def canvas_errors(value, teaching):
    if not isinstance(value, dict) or value.get('schema_version') != CANVAS_SCHEMA['schema_version']:
        return ['Wrong image canvas contract']
    rows = value.get('canvases')
    if not isinstance(rows, list) or not rows:
        return ['Need a nonempty canvas plan']
    expected = [b['id'] for b in teaching['teaching_sequence']]
    covered, seen, errors = [], set(), []
    for row in rows:
        if not isinstance(row, dict):
            errors.append('Canvas must be an object'); continue
        identity = row.get('id')
        if not safe(identity) or identity in seen:
            errors.append('Canvas IDs must be unique safe strings')
        seen.add(identity if isinstance(identity, str) else None)
        for key in ('visual_message', 'prompt', 'controller_type'):
            if not nonempty(row.get(key)):
                errors.append(f'{identity} needs {key}')
        beats = row.get('beat_ids')
        if not isinstance(beats, list) or not beats or any(b not in expected for b in beats):
            errors.append(f'{identity} must own accepted teaching beats')
        else:
            covered.extend(beats)
        parts = row.get('component_roles')
        valid_parts = isinstance(parts, list) and all(isinstance(p, dict) and safe(p.get('role'))
            and nonempty(p.get('description')) and _landmark_names(p.get('required_landmarks')) for p in parts)
        if not valid_parts:
            errors.append(f'{identity} needs explicit component roles and landmarks')
        elif len({p['role'] for p in parts}) != len(parts):
            errors.append(f'{identity} has duplicate component roles')
        controller_type = row.get('controller_type')
        if not safe(controller_type):
            errors.append(f'{identity} controller_type needs one safe controller ID, without prose or alternatives')
        capability = controller_capabilities().get(controller_type) if isinstance(controller_type, str) else None
        if capability and valid_parts:
            roles = {p.get('role') for p in parts}
            if not set(capability['roles']) <= roles:
                errors.append(f'{identity} omits controller roles: {capability["roles"]}')
            allowed = set(capability['roles'] + capability.get('optional_roles', []))
            if roles - allowed:
                errors.append(f'{identity} includes unsupported controller roles: {sorted(roles-allowed)}')
            for part in parts:
                required = set(capability.get('required_landmarks', {}).get(part['role'], []))
                if not required <= set(part['required_landmarks']):
                    errors.append(f'{identity}/{part["role"]} omits required controller landmarks: {sorted(required)}')
    if covered != expected:
        errors.append('Canvases must cover teaching order once each; adjacent beats can reuse one canvas')
    return errors


def grounding_incomplete_flags(value):
    """Derive readiness flags without changing the model response or its caveats.

    Historical string uncertainties mean unresolved geometry. A nonessential
    limitation must explicitly use kind='caveat'; prose heuristics never approve
    an uncertain source anchor or silhouette.
    """
    scene = value.get('scene', {})
    flags = scene.get('incomplete_flags', []) if isinstance(scene, dict) else []
    if not isinstance(flags, list) or any(not nonempty(flag) for flag in flags):
        raise ValueError('Scene incomplete_flags must be a list of nonempty descriptions')
    result = list(flags)
    uncertainties = value.get('uncertainties', [])
    if not isinstance(uncertainties, list):
        raise ValueError('Grounding uncertainties must be a list')
    for item in uncertainties:
        if isinstance(item, str):
            if not nonempty(item):
                raise ValueError('Grounding uncertainty descriptions cannot be empty')
            result.append('unresolved_geometry: ' + item.strip())
            continue
        if not isinstance(item, dict) or not isinstance(item.get('kind'), str) or item.get('kind') not in {'unresolved_geometry', 'caveat'} or not nonempty(item.get('description')):
            raise ValueError('Uncertainties need kind unresolved_geometry or caveat and a description')
        if item['kind'] == 'unresolved_geometry':
            field = item.get('field', '')
            if not isinstance(field, str):
                raise ValueError('An uncertainty field must be text when supplied')
            result.append('unresolved_geometry: ' + (field + ': ' if field else '') + item['description'].strip())
    return list(dict.fromkeys(result))


def grounding_errors(value, canvas, dimensions):
    if not isinstance(value, dict) or value.get('schema_version') != GROUNDING_SCHEMA['schema_version']:
        return ['Wrong image grounding contract']
    errors = []
    if value.get('canvas_id') != canvas['id']:
        errors.append('Grounding must retain canvas ID')
    if not nonempty(value.get('background_prompt')):
        errors.append('Need an editing prompt for the clean background')
    parts = value.get('parts')
    if not isinstance(parts, list) or any(not isinstance(p, dict) or not safe(p.get('id'))
            or not safe(p.get('role')) or not nonempty(p.get('prompt')) for p in parts):
        return errors + ['Need explicit complete-part requests']
    if len({p['id'] for p in parts}) != len(parts) or len({p['role'] for p in parts}) != len(parts):
        errors.append('Part IDs and roles must be distinct')
    planned = canvas.get('component_roles', [])
    if not isinstance(planned, list) or any(not isinstance(p, dict) or not safe(p.get('role'))
            or not _landmark_names(p.get('required_landmarks')) for p in planned):
        return errors + ['Canvas has invalid planned roles or required landmarks']
    if {p['role'] for p in parts} != {p['role'] for p in planned}:
        errors.append('Grounder must fulfill exactly the planned moving roles')
    scene = value.get('scene')
    if not isinstance(scene, dict):
        return errors + ['Need a grounded controller scene']
    if scene.get('canvas') != {'width': dimensions[0], 'height': dimensions[1]}:
        errors.append('Grounded canvas dimensions must match the actual source image')
    controller = scene.get('controller')
    assets = scene.get('assets')
    if not isinstance(controller, dict) or not isinstance(controller.get('parameters'), dict) or not isinstance(controller.get('bindings'), dict):
        return errors + ['Grounded controller needs type, parameters and bindings objects']
    if not isinstance(assets, dict) or any(not isinstance(asset, dict) for asset in assets.values()):
        return errors + ['Grounded scene assets must be an object of asset objects']
    if controller.get('type') != canvas['controller_type']:
        errors.append('Grounding cannot change the chosen controller')
    if set(assets) != {'background', *(p['id'] for p in parts)}:
        errors.append('Scene assets must contain background and exactly requested parts')
    if controller['bindings'] != {p['role']: p['id'] for p in parts}:
        errors.append('Controller bindings must associate each requested role with its own part ID')
    width, height = dimensions
    capabilities = controller_capabilities().get(controller.get('type')) if isinstance(controller.get('type'), str) else None
    for part in parts:
        identity = part['id']
        asset = assets.get(identity)
        if asset is None:
            continue
        try:
            bounds = box(part.get('bbox_xyxy_px'), identity + ' bbox')
            if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > width or bounds[3] > height:
                errors.append(identity + ' source bbox leaves the actual source canvas')
            if bounds != box(asset.get('bbox_xyxy_px'), identity + ' scene bbox'):
                errors.append(identity + ' part and scene source bboxes disagree')
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))
        landmarks = part.get('landmarks')
        if not isinstance(landmarks, dict) or any(not safe(name) for name in landmarks):
            errors.append(identity + ' landmarks must map safe names to source-pixel points')
            continue
        if landmarks != asset.get('landmarks'):
            errors.append(identity + ' part and scene landmarks disagree')
        for name, xy in landmarks.items():
            try:
                x, y = point(xy, identity + '/' + name)
                if not 0 <= x < width or not 0 <= y < height:
                    errors.append(identity + '/' + name + ' landmark leaves the source canvas')
            except (ValueError, TypeError) as exc:
                errors.append(str(exc))
        required = {name for p in planned if p['role'] == part['role'] for name in p['required_landmarks']}
        if capabilities:
            required.update(capabilities.get('required_landmarks', {}).get(part['role'], []))
        missing = required-set(landmarks)
        if missing:
            errors.append(identity + ' missing required source landmarks: ' + ', '.join(sorted(missing)))
        part_z, asset_z = part.get('z_index', 5), asset.get('z_index', 5)
        if not _number(part_z) or not _number(asset_z) or part_z != asset_z:
            errors.append(identity + ' part and scene z_index must be equal finite numbers')
    try:
        flags = grounding_incomplete_flags(value)
        candidate = copy.deepcopy(scene)
        candidate['incomplete_flags'] = flags
        compile_scene(candidate, allow_incomplete_preview=bool(flags))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(str(exc))
    return errors


def compile_cues(value, beat_id, words, start, end, controller, *, fps=30, title=None, display_labels=None):
    """LLMs select word IDs; only the verified MFA bundle supplies seconds."""
    if not isinstance(value, dict) or value.get('schema_version') != MOTION_SCHEMA['schema_version'] or value.get('beat_id') != beat_id:
        raise ValueError('Wrong beat motion cue contract')
    actions = value.get('actions')
    if not isinstance(actions, list):
        raise ValueError('Beat needs an action list (empty is valid for a static hold)')
    if not _number(start) or not _number(end) or not 0 <= start < end:
        raise ValueError('Beat requires a positive finite narration window')
    if not isinstance(words, list) or any(not isinstance(w, dict) or not safe(w.get('id'))
            or not _number(w.get('start')) or not _number(w.get('end')) or not 0 <= w['start'] <= w['end'] for w in words):
        raise ValueError('Motion requires actual finite MFA word spans')
    by_id = {w['id']: w for w in words}
    if len(by_id) != len(words):
        raise ValueError('MFA word IDs must be unique')
    numerical = []
    for row in actions:
        if not isinstance(row, dict) or not nonempty(row.get('purpose')):
            raise ValueError('Each action needs a teaching purpose')
        if not safe(row.get('id')):
            raise ValueError('Each cue needs a safe action ID')
        kind = row.get('kind')
        if not isinstance(kind, str) or kind not in MOTION_KINDS | PRESENTATION_KINDS:
            raise ValueError('Each cue needs an allowlisted action kind')
        if kind in MOTION_KINDS-{'hold'} and not _number(row.get('value_end')):
            raise ValueError('Moving cues require a finite value_end')
        for key in ('value_start', 'value_end'):
            if row.get(key) is not None and not _number(row[key]):
                raise ValueError('Cue scalar values must be finite numbers')
        if row.get('label') is not None and not isinstance(row['label'], str):
            raise ValueError('Cue label must be text')
        loops = row.get('loops')
        if loops is not None and (type(loops) is not int or not 1 <= loops <= 10000):
            raise ValueError('Cue loops must be a positive bounded integer')
        a, b = row.get('start_word_id'), row.get('end_word_id')
        if not isinstance(a, str) or not isinstance(b, str) or a not in by_id or b not in by_id:
            raise ValueError('Motion cues must use this beat’s actual MFA word IDs')
        lo, hi = by_id[a]['start'], by_id[b]['end']
        if not start <= lo < hi <= end:
            raise ValueError('Motion cue reverses or escapes its narration window')
        action = {k: v for k, v in row.items() if k in {'id', 'kind', 'value_start', 'value_end', 'loops', 'label'} and v is not None}
        action.update(start=round(lo-start, 9), end=round(hi-start, 9), word_ids=[a, b])
        numerical.append(action)
    scene = {'id': beat_id, 'title': title if title is not None else beat_id, 'start': 0, 'end': end-start, 'actions': numerical}
    if title is not None and not nonempty(title):
        raise ValueError('Scene title must be nonempty text')
    if display_labels is not None:
        if not isinstance(display_labels, list) or any(not nonempty(label) for label in display_labels):
            raise ValueError('Display labels must be a list of nonempty text')
        scene['display_labels'] = list(display_labels)
    timing = {'schema_version': 'infofilm.image-animation-timing.v1', 'duration_seconds': end-start, 'scenes': [scene]}
    compile_timeline(timing, controller, fps=fps)
    return timing
