"""Generate the planned initial images once, with image review explicitly skipped."""
from __future__ import annotations

import contextlib
from pathlib import Path

from .composition import asset_composition
from .core import digest, read, write
from .image_providers import provider_config
from .image_recovery import generation_prompt, provider_session, render_take
from .person_references import bind_reference_params, reference_addendum

PROTOCOL = 'infofilm.images-unreviewed.v1'


def generate_unreviewed_images(config, store, library, *, on_progress=None):
    registry = config.get('image_provider_registry')
    provider_id = config.get('image_provider_id', 'qwen_local')
    current = config
    if registry and not config.get('image_engine'):
        if config.get('image_recovery_mode') == 'message_v1':
            provider_id = config.get('image_recovery_provider', 'qwen_local')
        else:
            policy = registry['policies'][config.get('image_provider_policy', registry['default_policy'])]
            provider_id = policy['tiers'][0]['provider']
        current = provider_config(config, provider_id, registry['providers'][provider_id])
    total = len(library['assets'])
    output, pending = {}, []
    for position, asset in enumerate(library['assets'], 1):
        if config.get('image_recovery_mode') == 'message_v1':
            if any(not isinstance(asset.get(k), str) or not asset[k].strip()
                   for k in ['prompt', 'visual_message', 'setting', 'visual_style']):
                raise ValueError(f"{asset['id']} needs the complete planned image contract")
            case = {'asset_id': asset['id'], 'library': {**library, 'assets': [asset]},
                    'message_annotation': {'setting': asset['setting']}}
            prompt = generation_prompt(case, asset['prompt'])
        else:
            prompt = ('Composition: ' + asset_composition(library, asset)['generation_direction'] +
                      '\nShared visual style: ' + library['style'] + '\nScene illustration: ' + asset['prompt'] + reference_addendum(asset))
        identity = digest({'protocol': PROTOCOL, 'provider': provider_id, 'url': current['image_url'],
                           'engine': current.get('image_engine'), 'asset': asset, 'prompt': prompt,
                           'dimensions': [current['image_width'], current['image_height']], 'steps': current['image_steps'],
                           'cfg': current.get('image_cfg_scale'), 'multiple': current.get('image_dimension_multiple')})
        root = store.root / 'images' / ('direct-' + identity[:20])
        params = {'prompt': prompt, 'width': str(current['image_width']), 'height': str(current['image_height']),
                  'steps': str(current['image_steps']), 'guidance': '0', 'seed': str(int(identity[:7], 16)),
                  'output_format': 'png'}
        params = bind_reference_params(params, asset)
        selected = root / 'selected.json'
        if selected.exists():
            value = read(selected)
            if (value.get('asset') != asset['id'] or value.get('protocol') != PROTOCOL
                    or value.get('review_performed') is not False or value.get('settings') != params
                    or not Path(value['file']).is_file() or digest(Path(value['file']).read_bytes()) != value.get('sha256')):
                raise ValueError('Unreviewed image checkpoint changed')
            output[asset['id']] = value
        else:
            pending.append((position, asset, root, params))
    write(store.root / 'image_manifest.json', output)
    store.event('images', 'planned', {'planned_images': total, 'generated_images': len(output),
                 'remaining_images': len(pending), 'image_review': False, 'max_attempts': 1, 'provider': provider_id})
    with provider_session(current, store) if pending else contextlib.nullcontext():
        for position, asset, root, params in pending:
            store.event(asset['id'], 'rendering', {'position': position, 'planned_images': total,
                         'provider': provider_id, 'image_review': False, 'attempt': 1})
            candidate = render_take(current, store, root, params, f"{asset['id']} {position}/{total}")
            value = {'asset': asset['id'], 'file': str(candidate), 'sha256': digest(candidate.read_bytes()),
                     'prompt': params['prompt'], 'settings': params, 'provider_id': provider_id,
                     'visual_message': asset.get('visual_message'), 'protocol': PROTOCOL,
                     'selection_status': 'unreviewed', 'review_performed': False,
                     'review': {'status': 'skipped', 'pass': None, 'reason': 'disabled_by_user'}, 'total_attempts': 1}
            value.update({k: asset[k] for k in ['people_in_frame', 'person_reference_status', 'reference_card'] if k in asset})
            write(root / 'selected.json', value)
            output[asset['id']] = value
            write(store.root / 'image_manifest.json', output)
            store.event(asset['id'], 'generated', {'image_review': False, 'review_state': 'skipped',
                                                 'file': str(candidate), 'position': position, 'planned_images': total})
            store.event('images', 'progress', {'completed_images': len(output), 'planned_images': total,
                         'remaining_images': total - len(output), 'image_review': False})
            if on_progress:
                on_progress()
    write(store.root / 'image-direct-report.json', {'protocol': PROTOCOL, 'image_review': False,
                 'planned_images': total, 'generated_images': len(output), 'reviewed_images': 0})
    return output
