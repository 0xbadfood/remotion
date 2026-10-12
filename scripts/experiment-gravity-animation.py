#!/usr/bin/env python3
"""Resolve immutable SVG objects and render a standalone gravity animation seed."""
from __future__ import annotations
import argparse
import base64
import copy
import hashlib
import html
import json
import math
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / 'animation-library'


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def resolve(plan, registry):
    assets, faults = {}, []
    for obj in plan['objects']:
        asset = registry['objects'].get(obj['asset_id'])
        absent = sorted(set(obj['required_capabilities']) - set(asset.get('capabilities', []) if asset else []))
        if not asset or absent:
            faults.append({'schema_version': 'infofilm.animation-object-request.v1', 'state': 'pending_creation',
                'reason': 'missing_asset' if not asset else 'missing_capabilities',
                'scene_id': plan['id'], 'visual_message': plan['visual_message'],
                'object_id': obj['id'], 'proposed_asset_id': obj['asset_id'], 'role': obj['role'],
                'required_capabilities': obj['required_capabilities'], 'missing_capabilities': absent,
                'instance_requirements': copy.deepcopy(obj['instance']),
                'creation_contract': {'format': 'svg', 'immutable_version': True, 'external_resources': False,
                    'review_before_registration': True}})
            continue
        path = (LIBRARY / asset['file']).resolve()
        if not path.is_relative_to(LIBRARY.resolve()):
            raise ValueError('Object file escapes its library')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset['sha256']:
            raise ValueError('Object content changed without a new registry version: ' + obj['asset_id'])
        svg = ET.fromstring(data)
        allowed = {'svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
                   'defs', 'linearGradient', 'radialGradient', 'stop', 'title', 'desc'}
        for element in svg.iter():
            if element.tag.split('}')[-1] not in allowed:
                raise ValueError('Unsupported SVG object element')
            for key, value in element.attrib.items():
                if key.split('}')[-1].startswith('on') or key.split('}')[-1] in {'href', 'style'} or 'url(' in value:
                    raise ValueError('Unsupported SVG object attribute')
        assets[obj['asset_id']] = {**asset, 'data_uri': 'data:image/svg+xml;base64,' + base64.b64encode(data).decode()}
    return assets, faults


def validate(plan):
    if plan['schema_version'] != 'infofilm.animation-scene.v1' or plan['archetype'] != 'projectile-comparison.v1':
        raise ValueError('Unsupported scene contract')
    if not plan['visual_message'].strip(): raise ValueError('Missing visual message')
    p = plan['physics']
    if not 0 < p['gravity_m_s2'] < 100 or not 3 <= p['release_height_m'] <= 10 or not 0 < p['launch_speed_m_s'] <= 15:
        raise ValueError('Physics exceeds the validated seed layout envelope')
    reach = p['launch_speed_m_s'] * math.sqrt(2*p['release_height_m']/p['gravity_m_s2'])
    if reach * 38 + 240 + 22 > 875 or not 1 <= p['drop_x_m'] <= 3:
        raise ValueError('Ball paths exceed the portrait layout or overlap at release')
    if len({o['id'] for o in plan['objects']}) != len(plan['objects']): raise ValueError('Duplicate object IDs')
    roles = {o['role'] for o in plan['objects']}
    if not {'launcher', 'launched-ball', 'dropped-ball', 'ground', 'gravity-vector'} <= roles:
        raise ValueError('Missing required object roles')
    for key in ['first_launch', 'comparison']:
        track = plan['animation'][key]
        if not 0 <= track['start_seconds'] < track['end_seconds'] <= plan['duration_seconds']:
            raise ValueError('Invalid presentation interval')
    if not 1 <= plan['animation']['trajectory_intervals'] <= 20: raise ValueError('Invalid trajectory markers')
    previous = 0
    for cue in plan['cues']:
        if cue['start'] != previous or cue['end'] <= cue['start']: raise ValueError('Cue timeline gap or overlap')
        previous = cue['end']
    if previous != plan['duration_seconds']: raise ValueError('Cues do not cover the scene')


def report(output, orientation_names):
    sections = []
    files = ['report.html', 'scene-plan.json', 'compiled-manifest.json', 'render-results.json']
    for orientation in orientation_names:
        video = output / (orientation + '.mp4')
        data_url = 'data:video/mp4;base64,' + base64.b64encode(video.read_bytes()).decode()
        images = []
        for p in sorted(output.glob(orientation + '-*.png')):
            files.append(p.name)
            images.append('<figure><img src="' + p.name + '"><figcaption>' + html.escape(p.stem) + '</figcaption></figure>')
        sections.append('<section><h2>' + orientation.title() + '</h2><video controls playsinline preload="metadata" src="' + data_url + '"></video>'
            '<p><a download="gravity-' + orientation + '.mp4" href="' + data_url + '">Download standalone MP4</a></p><div class="frames">' + ''.join(images) + '</div></section>')
    body = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Gravity animation seed</title><style>' \
        'body{background:#071b29;color:#eaf2f7;font:17px/1.5 system-ui;margin:auto;max-width:1100px;padding:28px}a{color:#83e5f5}video{display:block;max-width:100%;max-height:75vh;border:1px solid #315467;border-radius:12px}section{margin:38px 0}.frames{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}figure{margin:0}img{max-width:100%}figcaption{color:#a7c4d5;font-size:13px}</style></head><body>' \
        '<h1>Gravity · Remotion animation seed</h1><p>A hand-authored 21-second prototype. Mathematical projectile motion, shared immutable SVG objects, separate per-instance animation, and a missing-object request contract.</p>' \
        '<p>This preview is silent. Cue times are manual; production narration and MFA integration are later steps. Uniform gravity and no air resistance are explicit simplifications.</p>' \
        '<p><a href="scene-plan.json">Scene plan</a> · <a href="compiled-manifest.json">Object resolution</a> · <a href="render-results.json">Checks</a></p>' + ''.join(sections) + '</body></html>'
    (output / 'report.html').write_text(body)
    write(output / 'public-report.json', {'schema_version': 'infofilm.public-experiment-report.v1', 'files': files})


def verify_media(output, orientations):
    results = read(output/'render-results.json')
    checks = []
    for name in orientations:
        metadata = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries',
            'stream=codec_name,codec_type,width,height,nb_frames,duration:format=duration', '-of','json',
            str(output/(name+'.mp4'))], text=True))
        expected = next(row for row in results['renders'] if row['orientation']==name)
        assert len(metadata['streams'])==1 and metadata['streams'][0]['codec_type']=='video'
        video = metadata['streams'][0]
        assert video['codec_name']=='h264' and int(video['nb_frames'])==expected['frames']
        assert (video['width'],video['height'])==(expected['width'],expected['height'])
        fps = read(output/(name+'.props.json'))['plan']['fps']
        assert abs(float(video['duration'])-expected['frames']/fps)<.001
        checks.append({'orientation':name,'passed':True,'metadata':metadata})
    results['media_checks'] = checks
    write(output/'render-results.json',results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--orientation', choices=['landscape', 'portrait', 'both'], default='both')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--probe-missing', help='Request an unavailable object and stop before rendering')
    parser.add_argument('--no-render', action='store_true')
    args = parser.parse_args()
    default_name = 'gravity-animation-object-fault' if args.probe_missing else 'gravity-animation-seed'
    output = (args.output or ROOT/'artifacts/experiments'/default_name).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output/'public-report.json').unlink(missing_ok=True)
    plan = read(ROOT/'animation-seeds/gravity/scene.json'); registry = read(LIBRARY/'registry.json')
    if args.probe_missing:
        plan = copy.deepcopy(plan)
        plan['objects'].append({'id': 'fault-probe', 'asset_id': args.probe_missing, 'role': 'time-indicator',
            'required_capabilities': ['elapsed-time-display'], 'instance': {'width': 80, 'height': 80}})
    validate(plan)
    assets, faults = resolve(plan, registry)
    write(output/'scene-plan.json', plan)
    manifest = {'schema_version':'infofilm.animation-compiled.v1', 'scene_id':plan['id'],
        'visual_message':plan['visual_message'], 'library_revision':registry['revision'],
        'scene_plan_sha256':hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest(),
        'instance_count':len(plan['objects']), 'unique_asset_count':len(assets),
        'resolved_assets':{k:{key:value for key,value in asset.items() if key!='data_uri'} for k,asset in assets.items()},
        'object_page_faults':faults, 'ready_to_render':not faults}
    write(output/'compiled-manifest.json', manifest)
    if faults:
        write(output/'object-requests.json', faults)
        print(json.dumps({'state':'object_page_fault','count':len(faults),'requests':str(output/'object-requests.json')}),flush=True)
        return 3
    print(json.dumps({'state':'objects_resolved','instances':len(plan['objects']),'shared_assets':len(assets)}),flush=True)
    if args.no_render: return 0
    orientations = ['landscape','portrait'] if args.orientation=='both' else [args.orientation]
    props = []
    for orientation in orientations:
        path = output/(orientation+'.props.json')
        write(path, {'plan':{**plan,**plan['orientations'][orientation]},'assets':assets})
        props.append(str(path))
    browser = read(ROOT/'config.json')['browser']
    subprocess.run(['node',str(ROOT/'renderer/gravity-seed.cjs'), str(output),browser,*props],cwd=ROOT/'renderer',check=True)
    verify_media(output,orientations)
    report(output,orientations)
    print(json.dumps({'state':'complete','report':str(output/'report.html')}),flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
