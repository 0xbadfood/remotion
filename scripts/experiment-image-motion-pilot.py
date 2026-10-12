#!/usr/bin/env python3
"""Fit image-derived pilot geometry and publish MG-Gen/Manim results.

This deliberately scoped pilot recognizes a blue circular guide and uses the
actual extracted foreground alpha. It does not claim general diagram parsing.
"""
import argparse
import base64
import html
import json
from pathlib import Path

import numpy as np
from PIL import Image


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def fit_circle(image):
    pixels = np.asarray(image.convert('RGB')).astype(float)
    # This colour is part of the declared experiment input, not an object mask.
    blue = ((pixels[:, :, 2] > 170) & (pixels[:, :, 2] > pixels[:, :, 0] + 55)
            & (pixels[:, :, 1] > pixels[:, :, 0] + 25))
    yy, xx = np.nonzero(blue)
    if len(xx) < 500:
        raise ValueError('The declared blue circular guide was not found')
    x0, y0 = float(xx.mean()), float(yy.mean())
    x, y = xx - x0, yy - y0
    a, b, c = np.linalg.lstsq(np.column_stack([2 * x, 2 * y, np.ones(len(x))]),
                            x * x + y * y, rcond=None)[0]
    cx, cy, radius = a + x0, b + y0, np.sqrt(c + a * a + b * b)
    residual = np.hypot(xx - cx, yy - cy) - radius
    occupied = np.unique(np.floor((np.arctan2(yy - cy, xx - cx) + np.pi)
                                  / (2 * np.pi) * 360).astype(int))
    rms = float(np.sqrt(np.mean(residual ** 2)))
    if rms > radius * .02 or len(occupied) < 280:
        raise ValueError('The source guide is not sufficiently close to a visible circle')
    colour = np.median(pixels[blue], axis=0).round().astype(int)
    return {'center_px': [float(cx), float(cy)], 'radius_px': float(radius),
            'guide_colour': '#' + ''.join(f'{value:02x}' for value in colour),
            'guide_width_px': float(len(xx) / (radius * len(occupied) * np.pi / 180)),
            'fit': {'method': 'Least-squares circle from source blue guide pixels',
                    'source_pixels': len(xx), 'angular_coverage_degrees': len(occupied),
                    'radial_residual_rms_px': rms,
                    'radial_residual_median_px': float(np.median(abs(residual)))}}


def make_geometry(args):
    root = args.output.resolve()
    source = Image.open(args.image).convert('RGB')
    foreground = Image.open(args.ball).convert('RGBA')
    bbox = foreground.getchannel('A').getbbox()
    if not bbox:
        raise ValueError('MG-Gen foreground alpha is empty')
    extraction = read(root / 'layers/extraction.json')
    if foreground.size == source.size:
        source_bbox = list(bbox)
    else:
        source_bbox = extraction['ball']['bounding_box_xyxy_px']
        if len(source_bbox) != 4:
            raise ValueError('Cropped foreground needs original image coordinates')
    # Centre the original alpha crop, then project its anchor onto the fitted guide.
    x, y, right, bottom = source_bbox
    ball_center = [(x + right) / 2, (y + bottom) / 2]
    circle = fit_circle(source)
    cx, cy = circle['center_px']
    delta = np.asarray(ball_center) - np.asarray([cx, cy])
    distance = float(np.linalg.norm(delta))
    if distance <= 0:
        raise ValueError('Extracted ball lies at the guide centre, not its circumference')
    start = (np.asarray([cx, cy]) + delta * circle['radius_px'] / distance).tolist()
    scene = {'schema_version': 'infofilm.image-motion-pilot.v1',
             'canvas': {'width': source.width, 'height': source.height},
             'path': {'type': 'circle', 'center_px': circle['center_px'],
                      'radius_px': circle['radius_px'], 'start_px': start,
                      'direction': 'counterclockwise', 'draw_guide': True,
                      'guide_colour': circle['guide_colour'],
                      'guide_width_px': circle['guide_width_px'],
                      'guide_reconstruction': 'Fit the visible source guide; reconstruct the small arc occluded by the ball'},
             'ball': {'bounding_box_xyxy_px': source_bbox,
                      'visible_center_px': ball_center,
                      'size_px': [right - x, bottom - y]},
             'source_geometry': {**circle['fit'],
                                 'ball_anchor_projection_px': abs(distance - circle['radius_px']),
                                 'placement': 'Measured from generated source and MG-Gen alpha; no model-written coordinates'},
             'animation': {'duration_seconds': 12, 'revolutions': 2,
                           'timing': 'linear progress along the Manim circular path', 'audio': None}}
    write(root / 'scene.json', scene)
    print(json.dumps({'stage': 'geometry', 'center_px': circle['center_px'],
                      'radius_px': circle['radius_px'], 'ball_bbox': source_bbox,
                      'ball_anchor_projection_px': scene['source_geometry']['ball_anchor_projection_px']}))


def publish(args):
    root = args.output.resolve()
    scene = read(root / 'scene.json')
    extraction = read(root / 'layers/extraction.json')
    generation = read(root / 'image-generation.json')
    files = ['report.html', 'input.png', 'scene.json', 'layers/extraction.json', 'image-generation.json']
    sections = []
    clips = []
    for name, label in [('ball-orbit', 'One ball following the measured circle'),
                        ('ball-orbit-relations', 'String, tangent velocity and inward force follow the same ball')]:
        path = root / 'manim' / name / (name + '.mp4')
        if not path.exists():
            continue
        receipt = path.parent / 'metrics.json'
        metrics = read(receipt)
        if not metrics.get('checks') or not all(metrics['checks'].values()):
            raise ValueError('Manim motion/media checks did not pass: ' + name)
        clips.append(metrics)
        encoded = base64.b64encode(path.read_bytes()).decode()
        sections.append('<section><h2>' + html.escape(label) + '</h2><video controls loop playsinline '
                        'src="data:video/mp4;base64,' + encoded + '"></video></section>')
        if name == 'ball-orbit-relations':
            sections[-1] = sections[-1].replace('</section>', '<p>Orange: inward force · Teal: tangent velocity · Grey: string. All three follow the same moving ball.</p></section>')
        rel = str(receipt.relative_to(root));files.append(rel)
        for still in sorted((path.parent / 'stills').glob('checkpoint-*.png')):
            rel = str(still.relative_to(root));files.append(rel)
    if len(clips) != 2:
        raise ValueError('Both actual Manim render receipts are required before publication')
    layers = []
    for filename, label in [('ball.png', 'Extracted ball, with transparency'),
                            ('background.png', 'MG-Gen inpainted background'),
                            ('ball-mask.png', 'Actual foreground segmentation'),
                            ('raw-layer-1000.png', 'Extracted orbit, including the original occluded gap')]:
        path = root / 'layers' / filename
        if path.exists():
            rel = str(path.relative_to(root));files.append(rel)
            layers.append('<figure><img class="checker" src="' + rel + '"><figcaption>'
                          + html.escape(label) + '</figcaption></figure>')
    summary = {'schema_version': 'infofilm.image-motion-pilot-result.v1',
               'completed': True, 'source_image': 'input.png',
               'extraction': extraction, 'source_geometry': scene['source_geometry'],
               'renders': clips, 'limitations': [
                   'One text-free circular diagram, not a general subject classifier.',
                   'No narration generation or MFA: this isolates visual extraction and motion.',
                   'Image generation, segmentation and background reconstruction still need inspection.',
                   'The pilot uses the MG-Gen image-object extraction path, not its Gemini/Anime.js animation generator.']}
    write(root / 'pilot-result.json', summary);files.append('pilot-result.json')
    data = html.escape(json.dumps({'geometry': scene, 'extraction': extraction, 'renders': clips}, indent=2))
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Image → MG-Gen layers → Manim motion</title><style>
body{margin:0;background:#f3f6fa;color:#192b40;font:16px/1.55 system-ui}main{max-width:1080px;margin:auto;padding:28px}
h1{font-size:30px}h2{font-size:22px}section{background:white;border-radius:14px;padding:20px;margin:22px 0}
img,video{display:block;width:100%;height:auto;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px}
figure{margin:0}figcaption{margin-top:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 monospace}
.checker{background:repeating-conic-gradient(#edf0f4 0% 25%,white 0% 50%) 0/18px 18px}a{color:#1260bc}
</style><main><h1>Image → MG-Gen layers → Manim motion</h1>
<p>Small isolated circular-motion pilot. The generated image establishes appearance and composition. MG-Gen extracts the foreground and reconstructs the background. A measured circle controls Manim's motion; dependent objects update from the same position.</p>
<section><h2>Generated source image</h2><img src="input.png"></section>'''
    page += ''.join(sections) + '<section><h2>Extracted layers</h2><div class="grid">' + ''.join(layers) + '</div></section>'
    projection = scene['source_geometry']['ball_anchor_projection_px']
    page += f'<section><h2>Scope and provenance</h2><p>The source uses built-in imagegen. MG-Gen\'s text modules are omitted for this text-free input. The guide is fitted from the original visible circle and reconstructed to complete its occluded arc; the initial ball anchor is projected {projection:.2f} source pixels onto that fitted path. Model detections, segmentation and reconstruction details are recorded below. Manim runs on CPU; production remains stopped. This demonstrates extraction and motion for one simple diagram; teaching, narration and motion-intent planning are not automated in this pilot.</p>'
    page += '<details><summary>Image prompt</summary><pre>' + html.escape(generation['prompt']) + '</pre></details>'
    page += '<details><summary>Geometry, extraction and rendering receipts</summary><pre>' + data + '</pre></details>'
    page += '<p><a href="pilot-result.json">Pilot receipt</a> · <a href="scene.json">Measured scene</a></p></section></main></html>'
    (root / 'report.html').write_text(page)
    write(root / 'public-report.json', {'schema_version': 'infofilm.public-experiment-report.v1', 'files': files})
    print(json.dumps({'stage': 'published', 'report': str(root / 'report.html')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['geometry', 'report'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image', type=Path)
    parser.add_argument('--ball', type=Path)
    args = parser.parse_args()
    if args.stage == 'geometry':
        if args.image is None or args.ball is None:parser.error('geometry needs --image and --ball')
        make_geometry(args)
    else:
        publish(args)


if __name__ == '__main__':
    main()
