"""Image framing guidance derived from the actual Remotion scene layout.

Standard landscape text sits on the left; minimal scenes place text below the
picture. Standard portrait scenes crop toward 72% of the source image width.
"""
from __future__ import annotations


def image_composition(contexts, orientations, banners=None):
    orientations = list(orientations or ['landscape', 'portrait'])
    standard = not contexts or any(c.get('copy_density', 'standard') != 'minimal' for c in contexts)
    left_text = standard and 'landscape' in orientations
    right_crop = standard and 'portrait' in orientations
    banner_by_scene = {b.get('scene_key'): b.get('text') for b in banners or []}
    # In minimal portrait delivery the contained source image sits below the
    # banner, which overlays only the blurred backdrop.
    has_banner = ((standard or 'landscape' in orientations) and
                  any(c.get('banner_override', banner_by_scene.get(c.get('scene_key'))) for c in contexts))
    if left_text:
        framing = ('Place the complete main subject and essential objects in the right half of the frame, '
                   'with their visual centre near 72% of the image width. '
                   'The left 43% is a low-detail continuation of the same setting, such as softly lit sky, '
                   'plain earth or a subdued wall, leaving room for composited text. '
                   'Keep a wide rod, document or group contained in the right half through camera distance or angle.')
    elif right_crop:
        framing = ('Place the complete main subject near 72% of the image width, with essential details grouped '
                   'together for a narrow portrait crop. The scene background continues naturally across the frame.')
    else:
        framing = 'Use a natural full-frame composition with the complete main subject clearly visible. '
        if 'landscape' in orientations:
            framing += 'Keep essential details above the lower quarter, where composited text will appear. '
        framing += 'A centred or right-positioned subject suits this layout.'
    if has_banner:
        framing += ' Reserve a calm upper-right strip for the composited banner; place important faces and objects below it.'
    return {'schema_version': 'infofilm.image-composition.v1', 'orientations': orientations,
            'copy_density': 'standard' if standard else 'minimal',
            'left_text_area_required': left_text, 'right_portrait_crop_required': right_crop,
            'upper_right_banner_required': has_banner, 'generation_direction': framing,
            'review_policy': 'Reject placement only when essential content is obscured by the actual overlay or lost in the requested crop. '
                             'A low-detail text area need not be completely empty. Right and right-of-centre are compatible; '
                             'precise coordinates and optional prompt framing preferences are advisory. '
                             'Minimal-copy scenes do not require an empty left side.'}


def asset_composition(library, asset):
    return image_composition(library.get('scene_contexts_by_asset', {}).get(asset['id'], []),
                             library.get('orientations'), library.get('banners'))
