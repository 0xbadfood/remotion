#!/usr/bin/env python3
"""Prepare and publish the isolated image/MG-Gen/Manim gravity pilot."""
import argparse
import base64
import html
import json
from pathlib import Path

from PIL import Image


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def geometry(args):
    root = args.output.resolve()
    extraction = read(root / 'layers/extraction.json')
    image = Image.open(root / 'input.png')
    balls = {}
    for role in ('fired', 'dropped'):
        layer = extraction['balls'][role]
        x1, y1, x2, y2 = layer['bounding_box_xyxy_px']
        balls[role] = {'bbox_xyxy_px': [x1, y1, x2, y2],
                       'centre_px': [(x1 + x2) / 2, (y1 + y2) / 2],
                       'size_px': [x2 - x1, y2 - y1],
                       'layer': layer['file'], 'extraction_method': layer['method']}
    if args.ground_y is None or args.landing_x is None:
        raise ValueError('Supply the inspected image ground level and fired landing x')
    radius = sum(ball['size_px'][1] for ball in balls.values()) / 4
    launch_y = sum(ball['centre_px'][1] for ball in balls.values()) / 2
    if not launch_y + radius < args.ground_y < image.height:
        raise ValueError('Ground must be below the balls and inside the source image')
    if not balls['fired']['centre_px'][0] < args.landing_x < image.width - radius:
        raise ValueError('Landing x must be right of the fired ball and inside the image')
    if balls['dropped']['centre_px'][0] >= balls['fired']['centre_px'][0] - radius * 2:
        raise ValueError('The dropped ball must have a separate clear lane left of the projectile')
    scene = {'schema_version': 'infofilm.gravity-image-pilot.v1',
             'learning_objective': 'Horizontal launch does not change vertical fall time. Two equal-sized balls start from the same height with zero initial vertical velocity and land together, ignoring air resistance.',
             'canvas': {'width': image.width, 'height': image.height},
             'balls': balls, 'ground_y_px': args.ground_y,
             'physics': {'radius_px': radius,
                         'horizontal_range_px': args.landing_x - balls['fired']['centre_px'][0]},
             'animation': {'intro_seconds': 2, 'flight_seconds': 4, 'landing_hold_seconds': 2, 'fps': 30},
             'provenance': {'object_centres': 'Measured from actual segmentation alpha bounds',
                            'ground_calibration': 'One source-image ground level chosen by visual inspection of the flat meadow; not inferred by MG-Gen',
                            'landing_x': 'An illustrative landing point chosen within the clear right-hand meadow',
                            'normalisation': 'Average the tiny source height/radius differences so equal-height equal-size comparison is exact',
                            'motion': 'Shared linear time parameter; vertical displacement is quadratic, fired horizontal displacement is linear',
                            'time_scale': 'Display time is illustrative; no metre scale is asserted',
                            'scope': 'One image-derived gravity illustration, without narration or general-purpose motion planning'}}
    write(root / 'scene.json', scene)
    print(json.dumps({'stage': 'geometry', 'ball_centres': {k: v['centre_px'] for k, v in balls.items()},
                      'launch_y_px': launch_y, 'normalised_radius_px': radius, 'ground_y_px': args.ground_y}))


def report(args):
    root = args.output.resolve()
    scene = read(root / 'scene.json')
    extraction = read(root / 'layers/extraction.json')
    source = read(root / 'image-generation.json')
    upstream = read(root / 'extraction/receipt.json')
    metrics = read(root / 'manim/metrics.json')
    if not metrics.get('checks') or not all(metrics['checks'].values()):
        raise ValueError('Actual Manim motion and media checks must pass before publication')
    encoded = base64.b64encode((root / 'manim/gravity-same-time.mp4').read_bytes()).decode()
    extraction_seconds = upstream['elapsed_seconds'] + extraction['elapsed_seconds']
    result = {'schema_version': 'infofilm.gravity-image-pilot-result.v1', 'completed': True,
              'source': source, 'scene': scene, 'extraction': extraction,
              'upstream_extraction': upstream, 'render': metrics,
              'limitations': ['Known colour roles for this pilot, including one automatic box fallback',
                              'Ground and illustrative horizontal range are inspected source anchors',
                              'Small source sprite size and height differences are normalised',
                              'No automated narration, teaching plan or general diagram parsing']}
    write(root / 'pilot-result.json', result)
    files = ['report.html', 'input.png', 'image-generation.json', 'scene.json', 'pilot-result.json',
             'layers/extraction.json', 'extraction/receipt.json', 'manim/metrics.json']
    layer_figures = []
    for name, title in [('fired-ball.png', 'Orange projectile · transparent layer'),
                        ('dropped-ball.png', 'Blue dropped ball · transparent layer'),
                        ('background.png', 'Colourful background · both balls removed')]:
        file = 'layers/' + name
        files.append(file)
        layer_figures.append(f'<figure><div class="checker"><img src="{file}" alt="{html.escape(title)}"></div><figcaption>{html.escape(title)}</figcaption></figure>')
    still_figures = []
    for index, title in [(2, 'Early fall'), (3, 'Halfway through flight'), (5, 'Simultaneous landing')]:
        file = f'manim/stills/checkpoint-{index:02d}.png'
        if not (root / file).is_file():
            raise ValueError('Missing decoded video checkpoint: ' + file)
        files.append(file)
        still_figures.append(f'<figure><img src="{file}" alt="{title}"><figcaption>{title}</figcaption></figure>')
    for file in files[1:]:
        if not (root / file).is_file():
            raise ValueError('Missing publication asset: ' + file)
    receipts = html.escape(json.dumps({'scene': scene, 'extraction': extraction,
                                      'upstream': upstream, 'render': metrics}, indent=2))
    prompt = html.escape(source['initial_prompt'] + '\n\nLayout edit:\n' + source['prompt'])
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gravity · image layers in motion</title><style>
*{box-sizing:border-box}body{margin:0;background:#edf2ef;color:#16342f;font:16px/1.6 system-ui,sans-serif}
main{max-width:1180px;margin:auto;padding:30px 22px 60px}.eyebrow{letter-spacing:.15em;text-transform:uppercase;font-size:12px;color:#48756a}h1{font-size:clamp(28px,4vw,44px);line-height:1.18;margin:12px 0}h2{font-size:22px;margin-top:0}p{max-width:900px}section{background:white;border:1px solid #d5e2da;border-radius:20px;padding:24px;margin:24px 0;box-shadow:0 8px 25px #183e3010}video,img{display:block;width:100%;border-radius:12px}video{background:#16342f}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.facts{display:flex;gap:12px;flex-wrap:wrap}.fact{padding:10px 16px;background:#e0ede7;border-radius:12px;font-size:14px}.fact strong{display:block;font-size:22px}figure{margin:0}figcaption{font-size:13px;padding-top:8px;color:#4d695f}.checker{background:repeating-conic-gradient(#e5ece8 0% 25%,white 0% 50%) 0/18px 18px;border-radius:12px;min-height:100px;display:grid;place-items:center}.checker img{max-height:200px;object-fit:contain}figure:last-child .checker img{max-height:none}details{margin:16px 0}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 monospace;color:#294a42}a{color:#146650}.note{color:#536f65;font-size:14px}@media(max-width:750px){.grid{grid-template-columns:1fr}section{padding:16px}}
</style><main><div class="eyebrow">Isolated physics animation pilot</div><h1>Different paths.<br>Same landing time.</h1><p>One ball is fired horizontally from the elevated cannon. Another is dropped from the same height at the same instant. Their horizontal motion differs, while gravity gives them identical vertical motion.</p>'''
    page += f'<div class="facts"><div class="fact"><strong>{metrics["duration_seconds"]:.0f} seconds</strong>720p · 30 fps</div><div class="fact"><strong>{extraction_seconds:.1f} seconds</strong>CPU extraction + selective background fill</div><div class="fact"><strong>{metrics["render_seconds"]:.1f} seconds</strong>CPU Manim render</div></div>'
    page += '<section><h2>Watch the experiment</h2><video controls loop playsinline preload="metadata" src="data:video/mp4;base64,' + encoded + '"></video><p class="note">Orange: fired horizontally · Blue: dropped. Same height, same release time, zero initial vertical velocity. Air resistance is ignored. Display timing is illustrative.</p></section>'
    page += '<section><h2>Decoded frames</h2><div class="grid">' + ''.join(still_figures) + '</div></section>'
    page += '<section><h2>The generated canvas</h2><img src="input.png" alt="Sunny illustrated landscape with a cannon on a raised stone pedestal and two balls at equal height"></section>'
    page += '<section><h2>Image layers</h2><div class="grid">' + ''.join(layer_figures) + '</div><p>The cannon and landscape remain fixed. Transparent ball layers move independently above that colourful background.</p></section>'
    page += '<section><h2>What this pilot establishes</h2><p>The image supplies appearance and composition. MG-Gen supplies object masks and its LaMa model fills the two vacated ball regions. Manim uses one shared time parameter: quadratic vertical fall for both balls, linear horizontal travel for the projectile. Both impacts appear in the same video frame.</p><p class="note">MG-Gen detected the orange ball. The blue ball required an automatically colour-derived box for SAM2. Ground level and landing range were calibrated by inspection; small source size and height differences were normalised. This is a scoped visual experiment, without narration or general-purpose teaching-plan generation.</p>'
    page += '<details><summary>Exact imagegen prompts</summary><pre>' + prompt + '</pre></details><details><summary>Geometry, extraction and rendering receipts</summary><pre>' + receipts + '</pre></details><p><a href="pilot-result.json">Pilot receipt</a> · <a href="scene.json">Scene contract</a> · <a href="/experiments/circular-image-mggen-manim/report.html">Earlier circular-motion pilot</a></p></section></main></html>'
    (root / 'report.html').write_text(page)
    write(root / 'public-report.json', {'schema_version': 'infofilm.public-experiment-report.v1', 'files': files})
    print(json.dumps({'stage': 'published', 'report': str(root / 'report.html')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['geometry', 'report'])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ground-y', type=float)
    parser.add_argument('--landing-x', type=float)
    args = parser.parse_args()
    (geometry if args.stage == 'geometry' else report)(args)


if __name__ == '__main__':
    main()
