#!/usr/bin/env python3
"""Prepare and publish a narration-backed image-layer four-stroke pilot.

This prototype records the explicit handover needed by a future LLM planner.
It reuses the accepted Luna script and existing speech/MFA, not a new planner run.
"""
import argparse
import base64
import html
import json
from pathlib import Path

from PIL import Image


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def geometry(args):
    root = args.output.resolve()
    layers = read(root / 'layers/extraction.json')
    image = Image.open(root / 'input.png')
    assets = {'background': {'path': 'layers/background.png'}}
    for role, part in layers['parts'].items():
        assets[role] = {'path': part['file'], 'bbox_xyxy_px': part['bounding_box_xyxy_px'],
                        **part['source_anchors'], 'extraction_method': part['method']}
    assets['crankshaft']['rotation_anchor_px'] = [641, 680]
    assets['connecting_rod'].update(endpoint_a_px=[641, 407], endpoint_b_px=[735, 683])
    scene = {'schema_version': 'infofilm.four-stroke-image-pilot.v1',
             'canvas': {'width': image.width, 'height': image.height}, 'assets': assets,
             'mechanism': {'controller': 'ideal_slider_crank_four_stroke',
                           'axis_x_px': 641, 'crank_center_px': [641, 680],
                           'crank_radius_px': 85, 'rod_length_px': 285,
                           'piston_reference_theta_deg': 90, 'piston_crown_offset_px': -81,
                           'bore_xyxy_px': [522, 213, 762, 576], 'chamber_head_y_px': 213,
                           'spark_tip_px': [644, 224], 'valve_lift_px': 18,
                           'rod_width_px': 17},
             'cavity_masks': [{'type': 'rectangle', 'bbox_xyxy_px': [524, 230, 761, 556], 'colour': '#DDDAD1'},
                              {'type': 'circle', 'center_px': [641, 680], 'radius_px': 171, 'colour': '#DDDAD1'}],
             'layout': {'sidebar_x_fraction': .80},
             'provenance': {'source_geometry': 'Human-inspected source-pixel part anchors; SAM2 supplies visible silhouettes',
                            'normalisation': 'Crank throw and rod length normalized slightly to provide piston/head/plug clearance in the illustrated cutaway',
                            'cavity_reconstruction': 'Flat neutral cavity fills reconstruct the bore/crankcase substrates hidden by the source moving parts and imperfect LaMa fills',
                            'rod_occlusion': 'The source rod is cropped where it passes behind the piston; analytical attachment points and small code pin joins complete the rig',
                            'controller': 'Hand-authored ideal educational slider-crank and stroke schedule; not an LLM-generated geometry or thermodynamic simulation',
                            'narration': 'Existing Luna script, VibeVoice speech and MFA reused unchanged',
                            'valves': 'Ideal stroke-specific timing; real engines use advance, delay and overlap'}}
    write(root / 'scene.json', scene)
    timing = read(root / 'narration-scenes.json')
    handover = {'schema_version': 'infofilm.image-motion-handover-prototype.v1',
                'provenance': 'Instruction prototype derived from this hand-built pilot; not a new Luna response',
                'learning_objective': 'Explain coordinated piston, crankshaft, valve and gas movement across one four-stroke spark-ignition cycle',
                'canvas_asset': 'input.png', 'layer_manifest': 'layers/extraction.json',
                'component_roles': [{'role': name, 'asset': spec['path'], 'source_anchors': {k: v for k, v in spec.items() if k.endswith('_px')}}
                                    for name, spec in assets.items() if name != 'background'],
                'controller': {'type': 'ideal_slider_crank_four_stroke', 'degrees_per_stroke': 180,
                               'degrees_per_complete_cycle': 720,
                               'relationships': ['Piston constrained to the cylinder axis',
                                                 'Rod rigid between wrist pin and rotating crank pin',
                                                 'Intake valve opens during intake', 'Exhaust valve opens during exhaust',
                                                 'Both valves closed during compression and power',
                                                 'Spark at compression/power transition'],
                               'parameters_from': 'scene.json/mechanism'},
                'asset_completion': {'policy': 'Inspect moving assets for source occlusions before animation; request completion when unseen surfaces will become visible',
                                     'pilot_faults': [{'role': 'crankshaft', 'status': 'visible_source_only',
                                                       'reason': 'The connecting rod hides part of the crankshaft in the source; rotation reveals the incomplete mask',
                                                       'production_action': 'Complete this isolated component before rendering final scenes'}]},
                'narration': {'script': timing['script'], 'audio': timing['audio_path'],
                              'timing': timing['timing'], 'duration_seconds': timing['duration_seconds']},
                'scenes': [{'id': s['id'], 'title': s['title'], 'start': s['start'], 'end': s['end'],
                            'visual_message': s['shot']['description'], 'actions': s['actions']}
                           for s in timing['scenes']],
                'planner_responsibilities': ['Describe the teaching action and its message',
                                             'Identify component roles and attachment landmarks in the source',
                                             'Select suitable motion controllers or explicitly request a missing controller',
                                             'Tie demonstrations and highlights to narration word cues',
                                             'Specify which states hold, repeat or hand off to the next scene',
                                             'Request segmentation/occlusion repair when required'],
                'executor_responsibilities': ['Segment/extract assets with explicit provenance',
                                              'Derive constrained geometry each frame', 'Render layers in the correct order',
                                              'Respect narration timing and continuity', 'Mux the preserved audio']}
    write(root / 'llm-handover-prototype.json', handover)
    print(json.dumps({'stage': 'geometry', 'scene': str(root / 'scene.json'), 'parts': len(layers['parts']),
                      'duration_seconds': timing['duration_seconds'], 'scenes': len(timing['scenes'])}))


def report(args):
    root = args.output.resolve()
    metrics = read(root / 'manim/render-results.json')
    print(json.dumps({'stage': 'report_input', 'keys': list(metrics)}))
    # Renderer publication is prepared after its actual media checks are present.
    return publish(root, metrics)


def publish(root, metrics):
    scene = read(root / 'scene.json')
    timing = read(root / 'narration-scenes.json')
    extraction = read(root / 'layers/extraction.json')
    source = read(root / 'image-generation.json')
    handover = read(root / 'llm-handover-prototype.json')
    provenance = read(root / 'narration/provenance.json')
    if metrics.get('status') != 'passed':
        raise ValueError('Actual media/animation verification must pass before publication')
    video = root / 'manim/four-stroke-narrated.mp4'
    if not video.is_file():
        raise ValueError('Missing narration-muxed final video')
    encoded = base64.b64encode(video.read_bytes()).decode()
    result = {'schema_version': 'infofilm.four-stroke-image-result.v1', 'completed': True,
              'scene': scene, 'narration': timing, 'narration_provenance': provenance,
              'extraction': extraction, 'render': metrics,
              'limitations': ['Pilot source anchors and motion controller are hand-authored',
                              'Semantic segmentation requires calibrated boxes/points, not whole-image MG-Gen detection alone',
                              'Occluded substrates need reconstruction',
                              'The source crankshaft mask is incomplete behind the rod; rotation can reveal a missing sector or detached-looking patch',
                              'Valves and gas overlays are idealized educational representations']}
    write(root / 'pilot-result.json', result)
    files = ['report.html', 'input.png', 'image-generation.json', 'scene.json', 'narration-scenes.json',
             'llm-handover-prototype.json', 'pilot-result.json', 'layers/extraction.json', 'layers/background.png',
             'layers/parts-gallery.png', 'narration/provenance.json', 'manim/render-results.json']
    timeline = []
    for s in timing['scenes']:
        description = html.escape(s['shot']['description'])
        timeline.append(f'<button onclick="jump({s["start"]})"><span>{s["start"]:.1f}s</span><strong>{html.escape(s["title"])}</strong><small>{description}</small></button>')
    receipts = html.escape(json.dumps({'scene': scene, 'extraction': extraction,
                                      'narration_provenance': provenance, 'render': metrics}, indent=2))
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Four-stroke engine · narration and image layers</title><style>
*{box-sizing:border-box}body{margin:0;background:#f0f3f1;color:#18352e;font:16px/1.6 system-ui,sans-serif}main{max-width:1200px;margin:auto;padding:30px 22px 60px}h1{font-size:clamp(30px,4vw,46px);line-height:1.15;margin:12px 0}h2{font-size:22px;margin-top:0}.eyebrow{font-size:12px;letter-spacing:.15em;text-transform:uppercase;color:#507467}section{background:white;border:1px solid #d5e1d9;border-radius:20px;padding:24px;margin:24px 0;box-shadow:0 8px 25px #183e3010}video,img{display:block;width:100%;border-radius:12px}video{background:#19332c}.timeline{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}button{display:grid;grid-template-columns:65px 1fr;text-align:left;gap:3px 12px;border:1px solid #ccddd3;border-radius:12px;background:#f5f9f6;padding:15px;cursor:pointer;color:inherit;font:inherit}button:hover{background:#e7f2eb}button span{grid-row:span 2;font-size:14px;color:#54776b}button small{font-size:13px;color:#536f65}details{margin:18px 0}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 monospace}.note{font-size:14px;color:#536f65}a{color:#12664f}.links{display:flex;gap:16px;flex-wrap:wrap}@media(max-width:750px){.timeline{grid-template-columns:1fr}section{padding:16px}}
</style><main><div class="eyebrow">Narration-backed mechanism pilot</div><h1>How a four-stroke<br>engine works</h1><p>The existing Luna narration drives seven demonstrations. The generated cutaway supplies the engine's appearance; extracted moving parts follow a constrained mechanism, with valve, gas and spark cues anchored to MFA word timings.</p>'''
    page += '<section><h2>Watch with narration</h2><video id="film" controls playsinline preload="metadata" src="data:video/mp4;base64,' + encoded + '"></video><p class="note">Original 134.28-second VibeVoice performance, muxed into this video. Simplified single-cylinder spark-ignition engine; motion is slowed and held for explanation.</p></section>'
    page += '<section><h2>Jump to a demonstration</h2><div class="timeline">' + ''.join(timeline) + '</div></section>'
    page += '<section><h2>The generated canvas</h2><img src="input.png" alt="Illustrated engine cutaway"></section><section><h2>Extracted source parts</h2><img src="layers/parts-gallery.png" alt="Segmented piston, rod, crankshaft and valves"><p class="note">MG-Gen detected the complete engine as one object. Five components were separated with calibrated SAM2 boxes and points. Valve mask corrections and occlusions are recorded. The rod hides part of the crankshaft in the source; rotation reveals that missing patch. Production needs complete moving-part assets before animation.</p></section>'
    page += '<section><h2>What the planner needs to instruct</h2><p>The planner supplies the teaching sequence, component roles and attachment landmarks, chooses a motion controller, and connects demonstrations to narration cues. The executor maintains the mechanical relationships and renders the frames.</p><p>This pilot uses a hand-authored slider-crank controller and inspected source anchors. It tests the handover shape; it does not yet automate arbitrary machine understanding. Neutral cavity fills reconstruct the areas hidden by moving parts. Valves and gas flow are teaching representations.</p>'
    page += '<div class="links"><a href="llm-handover-prototype.json">LLM handover prototype</a><a href="narration-scenes.json">Narration scene timings</a><a href="pilot-result.json">Complete receipt</a></div>'
    page += '<details><summary>Exact built-in imagegen prompt</summary><pre>' + html.escape(source['prompt']) + '</pre></details><details><summary>Geometry, extraction and rendering receipts</summary><pre>' + receipts + '</pre></details></section></main><script>function jump(t){const v=document.getElementById("film");v.currentTime=t;v.play();v.scrollIntoView({behavior:"smooth",block:"center"});}</script></html>'
    for file in files[1:]:
        if not (root / file).is_file():
            raise ValueError('Missing report asset: ' + file)
    (root / 'report.html').write_text(page)
    write(root / 'public-report.json', {'schema_version': 'infofilm.public-experiment-report.v1', 'files': files})
    print(json.dumps({'stage': 'published', 'report': str(root / 'report.html')}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['geometry', 'report'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    (geometry if args.stage == 'geometry' else report)(args)


if __name__ == '__main__':
    main()
