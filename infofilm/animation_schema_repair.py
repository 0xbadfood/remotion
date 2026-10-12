"""Failure-only local verdicts and copy patches; local prose never enters a plan."""
from __future__ import annotations

import copy
import json
import math

import httpx

from .core import digest, write

POLICY = 'infofilm.animation-scoped-schema-repair.v1'
PATCH_SCHEMA = {
    'patches': [{'op': 'copy', 'path': 'one supplied target path',
                 'from': 'its supplied frozen source path'}],
    'prose_checks': [{'field': 'exact supplied field path', 'same_meaning': True,
                     'confidence': 0.99, 'reason': 'Explain whether anything factual or causal changed'}],
    'explanation': 'Short structural-repair explanation; never a replacement plan'
}


def scope(candidate, context):
    """Derive permitted copies from authoritative inputs, never model invention."""
    frozen, allowed, prose, geometry_constraints = {}, [], [], []

    def offer(path, value):
        source = '/frozen_values/F' + str(len(frozen) + 1).zfill(3)
        frozen[source.rsplit('/', 1)[1]] = copy.deepcopy(value)
        allowed.append({'path': path, 'from': source})

    teaching = context.get('teaching_plan', {}).get('teaching_sequence')
    current = candidate.get('teaching_sequence')
    schema = context.get('output_schema', {})
    if schema.get('schema_version') == 'infofilm.animation-placement.v1':
        viewport = context.get('canvas_plan', {}).get('viewport', {})
        width, height = viewport.get('width'), viewport.get('height')
        definitions = {o['id']: o for o in context.get('accepted_components', {}).get('objects', [])}
        assets = context.get('resolved_assets', {})
        if (type(width) in {int, float} and type(height) in {int, float}
                and math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
            for index, obj in enumerate(candidate.get('objects', [])):
                if not isinstance(obj, dict):continue
                definition = definitions.get(obj.get('id'), {})
                asset = assets.get(definition.get('asset_id'), {})
                w, h = obj.get('width'), obj.get('height')
                if (definition.get('kind') == 'svg' and 'circular' in asset.get('capabilities', [])
                        and type(w) in {int, float} and type(h) in {int, float}
                        and math.isfinite(w) and math.isfinite(h) and w > 0 and h > 0
                        and abs(w * width / (h * height) - 1) > .02):
                    # Preserve the chosen pixel diameter and center; normalize height only.
                    wanted = w * width / height
                    offer(f'/objects/{index}/height', wanted)
                    geometry_constraints.append({'object_id': obj['id'], 'policy': 'preserve_width_make_circle_in_pixels',
                        'canvas_pixels': [width, height], 'original_width_fraction': w,
                        'computed_height_fraction': wanted})
    if schema.get('schema_version') == 'infofilm.animation-canvas.v1' and isinstance(teaching, list) and teaching != current:
        # Changed beat identity/order is a planning decision, not a prose repair.
        if (isinstance(current, list) and all(isinstance(b, dict) for b in current)
                and [b.get('id') for b in current] == [b['id'] for b in teaching]):
            for index, (original, proposed) in enumerate(zip(teaching, current)):
                for field in ('message', 'demonstration'):
                    a, b = original.get(field), proposed.get(field)
                    if a != b and isinstance(a, str) and isinstance(b, str):
                        prose.append({'field': f'/teaching_sequence/{index}/{field}',
                                      'field_purpose': field, 'original': a, 'candidate': b})
            offer('/teaching_sequence', teaching)

    if schema.get('schema_version') == 'infofilm.animation-components.v1':
        requests = {r.get('id'): r for r in candidate.get('component_requests', []) if isinstance(r, dict)}
        for index, obj in enumerate(candidate.get('objects', [])):
            if not isinstance(obj, dict):
                continue
            caps = obj.get('required_capabilities')
            wanted = None
            if caps is None:
                if obj.get('kind') in {'text', 'rect', 'line'}:
                    wanted = []
                elif obj.get('kind') == 'svg':
                    wanted = requests.get(obj.get('request_id'), {}).get('required_capabilities')
            elif isinstance(caps, list) and all(isinstance(c, str) and c.strip() for c in caps):
                unique = list(dict.fromkeys(caps))
                if unique != caps:
                    wanted = unique
            if isinstance(wanted, list) and all(isinstance(c, str) and c.strip() for c in wanted):
                offer(f'/objects/{index}/required_capabilities', list(dict.fromkeys(wanted)))

    requirement = context.get('learning_objective') or context.get('requirement') or {}
    if isinstance(requirement, dict):
        for field in ('requirement_id', 'scene_id', 'visual_message'):
            wanted = requirement.get('visual_message' if field == 'visual_message' else 'id')
            if field in schema and wanted is not None and candidate.get(field) != wanted:
                offer('/' + field, wanted)
    version = schema.get('schema_version')
    if isinstance(version, str) and version.startswith('infofilm.') and candidate.get('schema_version') != version:
        offer('/schema_version', version)
    scene_schema, scene = schema.get('scene'), candidate.get('scene')
    if isinstance(scene_schema, dict) and isinstance(scene, dict):
        scene_version = scene_schema.get('schema_version')
        if (isinstance(scene_version, str) and scene_version.startswith('infofilm.')
                and scene.get('schema_version') != scene_version):
            offer('/scene/schema_version', scene_version)
    return {'frozen_values': frozen, 'allowed_copies': allowed, 'prose_pairs': prose,
            'geometry_constraints': geometry_constraints}


def apply_verdict(candidate, limits, verdict):
    """Enforce scope, require every prose check, and copy only trusted values."""
    if not isinstance(verdict, dict) or not isinstance(verdict.get('patches'), list):
        raise ValueError('Local repair must return copy patches')
    pairs = {p['field']: p for p in limits['prose_pairs']}
    checks = verdict.get('prose_checks')
    if not isinstance(checks, list) or any(not isinstance(c, dict) for c in checks):
        raise ValueError('Local repair needs prose_checks, empty when no prose differs')
    fields = [c.get('field') for c in checks]
    if any(not isinstance(f, str) for f in fields) or len(set(fields)) != len(fields) or set(fields) != set(pairs):
        raise ValueError('Local equivalence checks must cover exactly the supplied prose pairs')
    for check in checks:
        confidence = check.get('confidence')
        if (check.get('same_meaning') is not True or type(confidence) not in {int, float}
                or not math.isfinite(confidence) or confidence < .9 or confidence > 1
                or not isinstance(check.get('reason'), str) or not check['reason'].strip()):
            raise ValueError('Prose meaning changed or equivalence is uncertain: ' + check['field'])
    allowed = {p['path']: p['from'] for p in limits['allowed_copies']}
    result, seen = copy.deepcopy(candidate), set()
    for patch in verdict['patches']:
        if (not isinstance(patch, dict) or set(patch) != {'op', 'path', 'from'}
                or patch['op'] != 'copy' or not isinstance(patch['path'], str)
                or patch['path'] not in allowed or patch['from'] != allowed[patch['path']]
                or patch['path'] in seen):
            raise ValueError('Local repair attempted an unauthorized edit or source')
        seen.add(patch['path'])
        parts = patch['path'].split('/')[1:]
        target = result
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        target[parts[-1]] = copy.deepcopy(limits['frozen_values'][patch['from'].rsplit('/', 1)[1]])
    if not seen:
        raise ValueError('Local repair returned no permitted copies')
    return result


class LocalSchemaRepair:
    def __init__(self, models, store, max_attempts=2):
        self.models, self.store, self.max_attempts = models, store, max_attempts

    def repair(self, name, candidate, context, errors, validator, directory, previous_review=None):
        if not errors or not isinstance(candidate, dict):
            return candidate, None
        limits = scope(candidate, context)
        if not limits['allowed_copies']:
            return candidate, None
        inputs = {'policy': POLICY, 'phase': name, 'output_schema': context.get('output_schema'),
                  'failed_candidate': candidate, 'validator_errors': errors,
                  'previous_semantic_review': previous_review,
                  'objective': (context.get('learning_objective') or context.get('requirement') or {}).get('visual_message'),
                  **limits, 'response_schema': PATCH_SCHEMA}
        identity = digest(inputs)
        root = directory / 'local-schema-repair' / identity[:16]
        write(root / 'input.json', inputs)
        task = (
            'Your job is SCOPED SCHEMA REPAIR ONLY after a validation failure. You are not the planner. '
            'Return JSON with patches, prose_checks and explanation. Each patch must use op copy and exactly one '
            'allowed path/from pair. Never return replacement prose, new facts, objects, placement or motion. '
            'For each supplied prose pair assess whether BOTH sentences preserve exactly the same teaching meaning. '
            'Negation, conditions, causality, quantities, qualifications and exclusions matter. Return same_meaning '
            'false when anything substantive changes or you are unsure. Metadata is not prose. Do not judge missing '
            'metadata as a semantic change; copy its accepted authoritative value. The pipeline independently '
            'supplies any circle geometry constraint: preserve its chosen width and center, copy only the '
            'computed height that makes equal pixel diameters. Never invent a different size. The pipeline '
            'enforces allowed paths and revalidates every result. The original frozen prose, not your prose, '
            'will be used. A separate frontier semantic review still follows.\nINPUT:\n')
        prompt = task + json.dumps(inputs, ensure_ascii=False)
        if len(prompt.encode()) > 120000:
            self.store.event('animation:'+name, 'local_schema_repair_skipped', {'reason': 'Scoped input exceeds byte bound'})
            return candidate, None
        history, feedback = [], ''
        for index in range(self.max_attempts):
            call_name = f'animation-local-schema-{name}-{identity[:8]}-{index}'
            self.store.event('animation:'+name, 'local_schema_repair_running', {'attempt': index+1, 'original_errors': errors})
            try:
                verdict = self.models.call('reviewer', call_name, prompt+feedback, max_tokens=2500)
                write(root / f'verdict-{index}.json', verdict)
                fixed = apply_verdict(candidate, limits, verdict)
                remaining = validator(fixed)
            except (ValueError, KeyError, TypeError, IndexError, RuntimeError, httpx.HTTPError) as exc:
                fixed, remaining = None, [str(exc)]
            history.append({'attempt': index+1, 'errors': remaining, 'accepted': not remaining})
            receipt = {'policy': POLICY, 'source_sha256': digest(candidate), 'attempts': history,
                       'original_errors': errors, 'accepted': not remaining,
                       'semantic_review': 'still_required', 'artifacts': str(root)}
            if not remaining:
                receipt['repaired_sha256'] = digest(fixed)
                write(root / 'repaired.json', fixed)
                write(root / 'evaluation.json', receipt)
                self.store.event('animation:'+name, 'local_schema_repair_passed', {'attempt': index+1, 'semantic_review': 'still_required'})
                return fixed, receipt
            write(root / 'evaluation.json', receipt)
            self.store.event('animation:'+name, 'local_schema_repair_rejected', {'attempt': index+1, 'errors': remaining})
            feedback = '\nYour previous copy/verdict failed code checks: ' + json.dumps(remaining) + '. Return only permitted copy operations and complete prose verdicts.'
        return candidate, receipt
