"""Source-backed person references and deterministic per-scene contact cards."""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .llm_settings import llm_config
from .core import ROOT, digest, read, write

PROTOCOL = 'infofilm.person-references.v1'
FACE_PROTOCOL = 'infofilm.person-faces.v1'
FACE_MODEL = ROOT / 'models' / 'face_detection_yunet_2023mar.onnx'
MAX_SEPARATE_FACES = 3          # Qwen follows up to three reference images; more people share one text-free strip


def reference_settings(config):
    value = config.get('person_references', {})
    if not isinstance(value, dict) or type(value.get('enabled', False)) is not bool:
        raise ValueError('person_references must be an object with boolean enabled')
    settings = {'enabled': False, 'max_people': 12, 'max_candidates': 5, 'max_source_pages': 3,
                'panel_width': 384, 'panel_height': 480, **value}
    for key, low, high in [('max_people', 1, 30), ('max_candidates', 1, 8),
                           ('max_source_pages', 1, 5), ('panel_width', 256, 512), ('panel_height', 320, 640)]:
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ValueError(f'person_references.{key} must be {low}..{high}')
    return settings


def person_errors(people):
    if not isinstance(people, list) or len(people) > 6:
        return ['people_in_frame must be an array of at most six named public historical figures']
    errors, names = [], set()
    for person in people:
        if not isinstance(person, dict):
            errors.append('Each visible person needs a name, appearance_context and reference_kind')
            continue
        for key, minimum, maximum in [('name', 2, 100), ('appearance_context', 5, 240)]:
            text = person.get(key)
            if not isinstance(text, str) or not minimum <= len(text.strip()) <= maximum:
                errors.append(f'Visible person needs a bounded {key}')
        if person.get('reference_kind') not in {'photograph', 'historical_portrait'}:
            errors.append('reference_kind must be photograph or historical_portrait')
        if person.get('birth_year') is not None and (type(person['birth_year']) is not int or not -3000 <= person['birth_year'] <= 2030):
            errors.append('birth_year must be an integer year (negative for BCE)')
        name = str(person.get('name', '')).casefold().strip()
        if name in names:
            errors.append('Do not duplicate a person within one scene')
        names.add(name)
    return errors


# Reliable photographs are rare for people born before this year; they are drawn from the evidence without a reference.
PHOTO_ERA_BIRTH_YEAR = 1850


def person_key(person):
    return digest({'protocol': PROTOCOL, **person})[:20]


def validate_reference(reference):
    path = Path(reference['file'])
    if not path.is_file() or digest(path.read_bytes()) != reference['sha256']:
        raise ValueError('Person reference file is missing or changed')
    with Image.open(path) as image:
        image.verify()
    return path


def _font(size):
    path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    return ImageFont.truetype(str(path), size) if path.exists() else ImageFont.load_default()


def _lines(draw, text, font, width):
    lines, current = [], ''
    for word in text.split():
        trial = (current + ' ' + word).strip()
        if current and draw.textlength(trial, font=font) > width:
            lines.append(current); current = word
        else:
            current = trial
    return lines + ([current] if current else [])


def build_contact_card(root, references, settings):
    """Only referenced people enter the card; labels are conditioning, not footage."""
    identity = digest({'protocol': PROTOCOL, 'references': references,
                       'size': [settings['panel_width'], settings['panel_height']]})
    directory = Path(root) / 'cards' / identity[:20]
    saved = directory / 'manifest.json'
    for ref in references:
        validate_reference(ref)
    if saved.exists():
        result = read(saved)
        validate_reference(result)
        if ([p.get('person') for p in result.get('people', [])] != [r['person'] for r in references]
                or [p.get('source_sha256') for p in result.get('people', [])] != [r['sha256'] for r in references]
                or [p.get('source_url') for p in result.get('people', [])] != [r['source_url'] for r in references]):
            raise ValueError('Contact card source bindings changed')
        return result
    width, height, gap, margin = settings['panel_width'], settings['panel_height'], 16, 20
    columns = min(3, len(references)); rows = (len(references) + columns - 1) // columns
    card = Image.new('RGB', (2*margin + columns*width + (columns-1)*gap,
                            2*margin + rows*height + (rows-1)*gap), '#e5e0d8')
    draw = ImageDraw.Draw(card); people = []
    for index, ref in enumerate(references):
        x = margin + (index % columns)*(width+gap); y = margin + (index // columns)*(height+gap)
        # Reserve enough space for full names/context without cutting a face.
        image_height = height - 140
        with Image.open(ref['file']) as image:
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((width, image_height), Image.Resampling.LANCZOS)
            card.paste(image, (x + (width-image.width)//2, y + (image_height-image.height)//2))
        person = ref['person']
        name_font, context_font = _font(20), _font(15)
        name_lines = _lines(draw, f"{index+1}. {person['name']}", name_font, width-12)
        if len(name_lines) > 3:
            name_font = _font(16); name_lines = _lines(draw, f"{index+1}. {person['name']}", name_font, width-12)
        cursor = y + image_height + 6
        for line in name_lines:
            draw.text((x+6, cursor), line, fill='#151515', font=name_font); cursor += 24
        context = (f"Source: {ref['depiction_kind'].replace('_', ' ')}" +
                   (f", {ref['date']}" if ref.get('date') else '') + f". Scene: {person['appearance_context']}")
        for line in _lines(draw, context, context_font, width-12):
            if cursor + 18 > y + height:
                break
            draw.text((x+6, cursor), line, fill='#333333', font=context_font); cursor += 18
        people.append({'panel': index+1, 'person': person, 'source_url': ref['source_url'],
                       'source_sha256': ref['sha256'], 'depiction_kind': ref['depiction_kind'],
                       'bbox': [x, y, x+width, y+image_height]})
    directory.mkdir(parents=True, exist_ok=True)
    image_file = directory / 'contact-card.png'
    card.save(image_file)
    result = {'protocol': PROTOCOL, 'file': str(image_file), 'sha256': digest(image_file.read_bytes()),
              'people': people, 'size': list(card.size)}
    write(saved, result)
    return result


def face_box(image):
    """Head-and-shoulders crop box: the detected face widened 2.2x, from half a face above it to 1.6 faces below its
    top (chin and collar, above name tags and patches). The largest face wins in a group photo; without a detector
    result, the top centre of the portrait."""
    width, height = image.size
    faces = []
    try:
        import cv2
        import numpy as np
    except ImportError:
        cv2 = None
    if cv2 is not None and FACE_MODEL.exists():
        try:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
            pixels = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
            detector = cv2.FaceDetectorYN.create(str(FACE_MODEL), '', (width, height), 0.7)
            found = detector.detect(pixels)[1]
            faces = [] if found is None else [tuple(map(float, f[:4])) for f in found]
        except cv2.error:
            faces = []
    if faces:
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        centre, side = x + w / 2, w * 2.2
        box = (centre - side / 2, y - h * 0.5, centre + side / 2, y + h * 1.6)
        method = 'face' if len(faces) == 1 else 'largest_face'
    else:
        side = min(width, height * 0.6)
        box = ((width - side) / 2, 0, (width + side) / 2, side * 1.1)
        method = 'top_centre'
    box = (max(0, int(box[0])), max(0, int(box[1])), min(width, int(box[2])), min(height, int(box[3])))
    return box, method, len(faces)


def face_reference(root, ref):
    """One person's text-free face crop for the image generator, cached by source checksum."""
    validate_reference(ref)
    directory = Path(root) / 'faces' / ref['sha256'][:20]
    saved = directory / 'face.json'
    if saved.exists():
        value = read(saved)
        validate_reference(value)
        return value
    with Image.open(ref['file']) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        box, method, faces = face_box(image)
        crop = image.crop(box)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'face.png'
    crop.save(target)
    value = {'protocol': FACE_PROTOCOL, 'file': str(target), 'sha256': digest(target.read_bytes()),
             'person': ref['person']['name'], 'source_sha256': ref['sha256'], 'box': list(box),
             'method': method, 'faces_detected': faces}
    write(saved, value)
    return value


def build_face_references(root, references):
    """Generator conditioning without any printed text: separate face crops for up to three people, otherwise one
    left-to-right strip. The labeled contact card stays with the reviewer, which needs the names."""
    faces = [face_reference(root, ref) for ref in references]
    if len(faces) <= MAX_SEPARATE_FACES:
        return {'protocol': FACE_PROTOCOL, 'combined': False, 'people': [f['person'] for f in faces],
                'images': [{'file': f['file'], 'sha256': f['sha256']} for f in faces], 'faces': faces}
    identity = digest([f['sha256'] for f in faces])
    target = Path(root) / 'faces' / 'strips' / (identity[:20] + '.png')
    if not target.exists():
        tiles = []
        for face in faces:
            with Image.open(face['file']) as image:
                tile = image.convert('RGB'); tile.thumbnail((320, 400), Image.Resampling.LANCZOS); tiles.append(tile)
        strip = Image.new('RGB', (sum(t.width for t in tiles) + 16 * (len(tiles) + 1), max(t.height for t in tiles) + 32), '#d9d6d0')
        x = 16
        for tile in tiles:
            strip.paste(tile, (x, 16)); x += tile.width + 16
        target.parent.mkdir(parents=True, exist_ok=True)
        strip.save(target)
    return {'protocol': FACE_PROTOCOL, 'combined': True, 'people': [f['person'] for f in faces],
            'images': [{'file': str(target), 'sha256': digest(target.read_bytes())}], 'faces': faces}


def prepare_person_references(config, store, library):
    settings = reference_settings(config)
    if not settings['enabled']:
        return library
    unique = {}
    for asset in library['assets']:
        errors = person_errors(asset.get('people_in_frame'))
        if errors:
            raise ValueError(f"{asset['id']} person plan: {'; '.join(errors)}")
        for person in asset['people_in_frame']:
            unique[person_key(person)] = person
    # Chapters share immutable source portraits; cards still contain only their cast.
    cache_root = (store.root.parent.parent if (store.root / 'chapter-context.json').exists() else store.root) / 'person-references'
    identity = digest({'protocol': PROTOCOL, 'people': unique, 'settings': settings,
                       'models': {k: config[k] for k in ['planner_url', 'reviewer_url']}})
    archive = store.root / 'person-references' / 'requests' / identity[:20]
    result_file = archive / 'result.json'
    store.event('person-references', 'planned', {'people': len(unique), 'assets': len(library['assets']),
                'max_people': settings['max_people']})
    if len(unique) > settings['max_people']:
        raise ValueError('Person reference budget exceeded; increase person_references.max_people')
    if unique and not result_file.exists():
        # The application environment already has Pillow. The KB environment
        # need not install image dependencies for this specialized research task.
        interpreter = Path(config.get('person_reference_python', sys.executable))
        request_file = write(archive / 'request.json', {'people': unique, 'settings': settings,
            'directory': str(archive), 'cache_root': str(cache_root), 'project': str(store.root),
            'model_config': llm_config(config)})
        subprocess.run([str(interpreter), '-u', '-m', 'infofilm.person_reference_worker', str(request_file)],
                       cwd=ROOT, check=True)
    people = read(result_file)['people'] if unique else {}
    if set(people) != set(unique):
        raise ValueError('Person reference research must cover exactly the planned people')
    for key, ref in people.items():
        if ref.get('person') != unique[key] or ref.get('status') not in {'ready', 'unavailable'}:
            raise ValueError('Person reference identity or source status changed')
        if ref['status'] == 'ready' and ref.get('depiction_kind') != unique[key]['reference_kind']:
            raise ValueError('Person reference is not the planned photograph/portrait kind')
    result = {**library, 'assets': []}
    for asset in library['assets']:
        asset = dict(asset); refs, missing = [], []
        for person in asset['people_in_frame']:
            ref = people[person_key(person)]
            if ref['status'] == 'ready':
                validate_reference(ref); refs.append(ref)
            else:
                missing.append({'person': person, 'reason': ref['reason']})
        if refs:
            asset['reference_card'] = build_contact_card(cache_root, refs, settings)
            asset['reference_faces'] = build_face_references(cache_root, refs)
        asset['person_reference_status'] = {'referenced': len(refs), 'unavailable': missing}
        if asset['people_in_frame']:
            store.event(asset['id'], 'person_references_bound', {'people_in_frame': [p['name'] for p in asset['people_in_frame']],
                'referenced': len(refs), 'unavailable': [p['person']['name'] for p in missing],
                'contact_card': asset.get('reference_card', {}).get('file')})
        result['assets'].append(asset)
    write(store.root / 'person-references' / 'manifest.json', {'protocol': PROTOCOL, 'people': people,
          'assets': {a['id']: {'people_in_frame': a['people_in_frame'], 'status': a['person_reference_status'],
                    'card': a.get('reference_card')} for a in result['assets']}})
    store.event('person-references', 'succeeded', {'people': len(people),
        'ready': sum(p['status'] == 'ready' for p in people.values()),
        'unavailable': sum(p['status'] != 'ready' for p in people.values()),
        'cards': sum(bool(a.get('reference_card')) for a in result['assets'])})
    return result


def reference_addendum(asset):
    faces = asset.get('reference_faces')
    if faces:
        order = ('one reference image with the faces left to right: ' if faces['combined'] else 'reference images in order: ')
        names = '; '.join(f"{index}. {name}" for index, name in enumerate(faces['people'], 1))
        return ('\nAppearance reference: the attached ' + order + names + '. They show faces only. Preserve each '
                "person's observable facial features in the requested scene and treatment; adapt age, hair and clothing "
                'to the scene period, since a source photo may come from another adult period. Render a single '
                'full-frame scene in which these people appear naturally, with the scene\'s own lighting and poses.')
    card = asset.get('reference_card')
    if not card:
        return ''
    labels = '; '.join(f"panel {p['panel']}: {p['person']['name']}" for p in card['people'])
    return ('\nAppearance reference: use the attached labeled contact card for the people depicted (' + labels +
            '). Preserve their observable facial features and distinctive appearance in the requested scene and treatment. '
            'Adapt age and clothing to the scene period; the source portrait may come from another adult period. '
            'The card supplies appearance only; render one composed scene with scene lighting and pose. '
            'Keep the labels, panel borders and card layout confined to the reference.')


def bind_reference_params(params, asset):
    faces = asset.get('reference_faces')
    if faces:
        for image in faces['images']:
            validate_reference(image)
        return {**params, 'reference_images': [dict(image) for image in faces['images']]}
    card = asset.get('reference_card')
    if not card:
        return params
    validate_reference(card)
    return {**params, 'reference_images': [{'file': card['file'], 'sha256': card['sha256']}]}


def reference_review_kwargs(asset):
    card = asset.get('reference_card')
    if not card:
        return {}
    return {'reference_image_paths': [validate_reference(card)]}


def submit_qwen_job(config, params, request_fn):
    """References are immutable inputs and must accompany both POST and recovery."""
    fields = {k: v for k, v in params.items() if k != 'reference_images'}
    if len(fields['prompt']) > 4000:
        raise ValueError('Qwen prompt with appearance binding exceeds 4000 characters')
    references = params.get('reference_images', [])
    if not references:
        return request_fn(config, 'image', 'POST', '/api/generate', data=fields)
    with contextlib.ExitStack() as stack:
        files = []
        for ref in references:
            path = validate_reference(ref)
            files.append(('references', (path.name, stack.enter_context(path.open('rb')), 'image/png')))
        return request_fn(config, 'image', 'POST', '/api/generate', data=fields, files=files)
