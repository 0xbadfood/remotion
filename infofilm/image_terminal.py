"""Bounded premium rescue and explicit best-available, expert-review selection."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .core import digest, read, write
from .image_providers import check_candidate, image_review_errors, provider_config

PROTOCOL = 'infofilm.image-terminal-score.v1'
REVIEW_TAG = 'AI generated, needs expert review'


def terminal_settings(config):
    value = config.get('image_terminal_recovery', {'enabled': False})
    if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
        raise ValueError('image_terminal_recovery needs an enabled boolean')
    if not value['enabled']:
        return value
    if value.get('provider') not in config['image_provider_registry']['providers']:
        raise ValueError('Unknown terminal image provider')
    if type(value.get('max_candidates')) is not int or not 1 <= value['max_candidates'] <= 2:
        raise ValueError('Terminal recovery permits one or two premium candidates')
    return {**value, 'protocol': PROTOCOL}


def score_errors(value, message, *, require_inventory=False, reference_available=None):
    errors = image_review_errors(value, require_inventory=require_inventory, reference_available=reference_available)
    if not isinstance(value, dict):
        return errors
    if value.get('visual_message') != message:
        errors.append('Echo visual_message exactly')
    for key, maximum in [('message_marks', 50), ('prompt_marks', 30), ('usability_marks', 20)]:
        score = value.get(key)
        if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= maximum:
            errors.append(f'{key} must be finite, 0..{maximum}')
    if 'score' in value and not errors:
        expected = round(sum(value[k] for k in ['message_marks', 'prompt_marks', 'usability_marks']), 3)
        if type(value['score']) not in {float, int} or value['score'] != expected:
            errors.append('score must equal the rubric marks total')
    return errors


def score_image(models, case, candidate, prompt, name):
    from .educational_review import review_guidance, component_checks_enabled, normalize_inventory
    from .person_references import reference_review_kwargs
    asset = case['library']['assets'][0]
    reference_kwargs = reference_review_kwargs(asset)
    packet = {'visual_message': case['visual_message'], 'target_prompt': prompt,
              'acceptance': asset.get('acceptance', [])}
    packet['source_facts'] = [f for f in case.get('source_context', {}).get('facts', []) if f['id'] in asset.get('fact_ids', [])]
    packet['rendering'] = {'orientations': case['library'].get('orientations'),
        'landscape_display': 'Center cover crop to 16:9; essential content must survive the crop.'}
    task = review_guidance(asset) + (
        'Score the attached image against this shared target prompt and visual_message. '
        'You do not know its provider, earlier results, or other candidates. '
        'Give message_marks 0..50 for conveying the informative meaning, prompt_marks 0..30 for '
        'matching the requested concrete scene, and usability_marks 0..20 for coherent, usable illustration. '
        'These marks are independent of pass/fail: imperfect images still need a meaningful score. '
        'The same target and rubric are used for every candidate. Score what is visible; '
        'do not require the image to encode every narrated date or prove a causal claim. '
        'Respect the acceptance rules consistently, including generated titles/legends/labels when '
        'raster typography is forbidden. Ordinary incidental period writing is not an overlay automatically. '
        'Do not invent historical objections, such as assuming religious mouth cloths are modern masks; '
        'uncertain facts go in limitations. Demonstrable contradictions with the prompt reduce marks. '
        'No model escalation or prompt repair is needed now. Return JSON '
        '{visual_message:string echoed exactly, message_marks:number, prompt_marks:number, '
        'usability_marks:number, pass:boolean, reason_keys:[allowed key], issues:[visible defect string], '
        'limitations:[uncertain observation string], correction:string}. '
        'For pass use empty reason_keys/issues/correction; for fail use concrete evidence.\n'
    )
    from .image_providers import IMAGE_REVIEW_REASONS
    task += 'REASON KEYS:\n' + json.dumps(IMAGE_REVIEW_REASONS) + '\nINPUT:\n' + json.dumps(packet)
    if reference_kwargs:
        task = ('The first image is the candidate; the second is its sourced, labeled appearance card. '
                'Include observable appearance fidelity in prompt marks when these named people are depicted, '
                'allowing requested style/pose/lighting. Do not infer identity from pixels. Card labels belong only '
                'to the reference. Planned people: ' + json.dumps(asset.get('people_in_frame', [])) + '\n' + task)
    feedback = ''
    for index in range(2):
        try:
            review = models.call('reviewer', name + f'-json-{index}', task + feedback,
                                 image_path=candidate, max_tokens=2200, **reference_kwargs)
            review = normalize_inventory(review)
            errors = score_errors(review, case['visual_message'], require_inventory=component_checks_enabled(asset), reference_available=bool(reference_kwargs))
        except ValueError as exc:
            errors = [str(exc)]
        if not errors:
            return {**review, 'score': round(sum(review[k] for k in
                    ['message_marks', 'prompt_marks', 'usability_marks']), 3), 'score_protocol': PROTOCOL}
        feedback = '\nRepair only the score JSON schema for the same image: ' + json.dumps(errors)
    raise ValueError('Terminal score JSON still invalid: ' + '; '.join(errors))


def selectable(value):
    """Best-available is separate from approval; never forge review.pass."""
    if value.get('needs_review'):
        return (value.get('needs_review') is True and value.get('selection_status') == 'best_available'
             and value.get('terminal_recovery', {}).get('protocol') == PROTOCOL
             and value.get('review', {}).get('score_protocol') == PROTOCOL
             and type(value.get('review', {}).get('score')) in {int, float}
             and not score_errors(value.get('review'), value.get('visual_message')))
    return value.get('review', {}).get('pass') is True


def terminal_candidates(state):
    """Include a generated candidate whose original review could not be parsed."""
    candidates = list(state.get('takes', []))
    if state.get('pending_review'):
        candidates.append(state['pending_review'])
    return candidates


def checkpoint(config, root, case, policy):
    state = read(root / 'message-state.json')
    candidates = terminal_candidates(state)
    if not candidates:
        raise RuntimeError('No usable prior candidate for terminal recovery')
    target = candidates[-1]['settings']['prompt']
    provider = config['image_provider_registry']['providers'][policy['provider']]
    identity = digest({'policy': policy, 'provider': provider, 'target_prompt': target,
                       'visual_message': case['visual_message']})
    return root / 'terminal-recovery' / identity[:20], identity


def needs_generation(config, root, case, policy):
    rescue, _ = checkpoint(config, root, case, policy)
    return not (rescue / 'selected.json').exists() and any(not (rescue / f'take-{i:02d}/candidate-0.png').exists()
                                                        for i in range(policy['max_candidates']))


def recover(config, store, root, case, models, policy, *, session_managed=False):
    from .image_recovery import provider_session, render_take
    state = read(root / 'message-state.json')
    candidates = terminal_candidates(state)
    if not candidates:
        raise RuntimeError('No usable prior candidate for terminal recovery')
    target = candidates[-1]['settings']['prompt']
    provider = config['image_provider_registry']['providers'][policy['provider']]
    premium = provider_config(config, policy['provider'], provider)
    rescue, identity = checkpoint(config, root, case, policy)
    selection = rescue / 'selected.json'
    if selection.exists():
        value = read(selection)
        if not selectable(value) or digest(Path(value['file']).read_bytes()) != value['sha256']:
            raise ValueError('Terminal selection changed')
        return value
    write(rescue / 'policy.json', {'settings': policy, 'target_prompt': target, 'visual_message': case['visual_message']})
    # Optional explicit manual pilot candidates; never discover unrelated images
    # by subject or filename. Caller supplies the identity and actual prompt.
    for extra in config.get('image_terminal_extra_candidates', {}).get(case['asset_id'], []):
        if extra['visual_message'] != case['visual_message'] or digest(Path(extra['candidate']).read_bytes()) != extra['sha256']:
            raise ValueError('Extra candidate identity changed')
        candidates.append(extra)
    store.event(case['asset_id'], 'premium_rescue', {'provider': policy['provider'], 'prior_candidates': len(candidates),
        'max_premium_candidates': policy['max_candidates'], 'selection_policy': 'highest prompt score; expert-review tag'})
    # Sessions are never nested under the standard provider lease. A checkpoint
    # already containing all premium files avoids loading the GPU on resume.
    paths = [rescue / f'take-{i:02d}' / 'candidate-0.png' for i in range(policy['max_candidates'])]
    import contextlib
    session = provider_session(premium, store) if not session_managed and any(not p.exists() for p in paths) else contextlib.nullcontext()
    with session:
        for index, path in enumerate(paths):
            params = {'prompt': target, 'width': str(premium['image_width']), 'height': str(premium['image_height']),
                      'steps': str(premium['image_steps']), 'guidance': '0',
                      'seed': str(int(identity[:7], 16) + index), 'output_format': 'png'}
            from .person_references import bind_reference_params
            params = bind_reference_params(params, case['library']['assets'][0])
            candidate = render_take(premium, store, path.parent, params, case['asset_id'] + ' premium')
            candidates.append({'provider': policy['provider'], 'candidate': str(candidate), 'settings': params,
                               'sha256': digest(candidate.read_bytes()), 'visual_message': case['visual_message']})
    scored, seen = [], set()
    for ordinal, item in enumerate(candidates):
        if item['sha256'] in seen:
            continue
        seen.add(item['sha256'])
        path = Path(item['candidate'])
        if digest(path.read_bytes()) != item['sha256']:
            raise ValueError('Image candidate changed before scoring')
        check_candidate(path, store, case['asset_id'])
        score_path = rescue / f'score-{ordinal:02d}-{item["sha256"][:12]}.json'
        review = read(score_path) if score_path.exists() else score_image(models, case, path, target,
                      'terminal-score-' + case['asset_id'] + '-' + identity[:12] + '-' + str(ordinal))
        if score_errors(review, case['visual_message']):
            raise ValueError('Saved terminal score is invalid')
        write(score_path, review)
        scored.append({**item, 'review': review, 'evaluated_prompt': target})
        store.event(case['asset_id'], 'candidate_scored', {'candidate': str(path), 'provider': item['provider'],
             'score': review['score'], 'pass': review['pass'], 'position': len(scored), 'candidate_pool': len(candidates)})
        write(rescue / 'scores.json', scored)
    best = max(scored, key=lambda item: (item['review']['score'], item['provider'] == policy['provider']))
    value = {'asset': case['asset_id'], 'visual_message': case['visual_message'], 'file': best['candidate'],
             'sha256': best['sha256'], 'prompt': best['settings']['prompt'], 'settings': best['settings'],
             'provider_id': best['provider'], 'review': best['review'], 'evaluated_prompt': target,
             'total_attempts': len(candidates), 'needs_review': True, 'review_tag': REVIEW_TAG,
             'selection_status': 'best_available', 'terminal_recovery': {'protocol': PROTOCOL,
                'premium_provider': policy['provider'], 'scores': str(rescue / 'scores.json'),
                'premium_candidates': policy['max_candidates'], 'rule': 'highest marks; premium wins ties'}}
    asset = case['library']['assets'][0]
    value.update({k: asset[k] for k in ['people_in_frame', 'person_reference_status', 'reference_card'] if k in asset})
    write(selection, value)
    state.update(status='best_available', selected=value, terminal_recovery=value['terminal_recovery'])
    write(root / 'message-state.json', state)
    store.event(case['asset_id'], 'best_available_selected', {'provider': best['provider'], 'score': best['review']['score'],
        'review_pass': best['review']['pass'], 'needs_review': True, 'tag': REVIEW_TAG})
    return value
