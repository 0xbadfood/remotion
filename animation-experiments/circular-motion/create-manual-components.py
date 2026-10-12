"""Author C02's requested intrinsic SVG geometry; registration follows inspection."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infofilm.animation_objects import validate_svg
from infofilm.core import digest, read, write


PARTS = {
    'circular-orbit-guide': {
        'width': 400, 'height': 400,
        'body': '<circle cx="200" cy="200" r="180" fill="none" stroke="#9ECADA" stroke-width="3"/>'
                '<path d="M190 200H210M200 190V210" fill="none" stroke="#E6F2FA" stroke-width="2"/>'
                '<circle cx="200" cy="200" r="3" fill="#E6F2FA"/>',
        'landmarks': {'rotation_axis_center': [200, 200], 'circular_path_reference_point': [380, 200],
                      'orbit_radius_px': 180},
        'notes': 'Transparent unfilled circular guide. Center cross marks the axis; no ball, string or force arrow is baked in.'
    },
    'coriolis-qualitative-path': {
        'width': 320, 'height': 240,
        'body': '<path d="M0 120 C90 120 200 125 310 214" fill="none" stroke="#B7A5EF" '
                'stroke-width="4" stroke-linecap="round" stroke-dasharray="9 7"/>',
        'landmarks': {'initial_point': [0, 120], 'initial_outward_tangent_direction': [1, 0],
                      'right_curving_path_endpoint': [310, 214],
                      'cubic_control_points': [[0, 120], [90, 120], [200, 125], [310, 214]]},
        'notes': 'Qualitative guide only, not a computed physical trajectory. In SVG coordinates, +x is right and +y is down: '
                 'the initial outward tangent is +x and the curve bends toward +y, its right-hand side. '
                 'The initial point equals the requested left-center pivot. Scene rotation and ball motion belong to the planner.'
    },
    'rotating-earth-globe': {
        'width': 480, 'height': 480,
        'body': '<defs><radialGradient id="ocean" cx="35%" cy="30%" r="75%">'
                '<stop offset="0" stop-color="#257DA4"/><stop offset="1" stop-color="#103B5C"/>'
                '</radialGradient><clipPath id="globe"><ellipse cx="240" cy="240" rx="176" ry="173"/>'
                '</clipPath></defs><g transform="rotate(-18 240 240)">'
                '<ellipse cx="240" cy="240" rx="176" ry="173" fill="url(#ocean)" stroke="#A7D5E7" stroke-width="3"/>'
                '<g clip-path="url(#globe)" fill="#67B9A3" opacity="0.9">'
                '<path d="M84 128L118 107L153 111L173 128L169 151L195 169L184 190L160 196L153 220L128 227L111 209L96 180Z"/>'
                '<path d="M146 239L172 228L195 247L208 280L197 320L179 352L164 335L162 300L150 274Z"/>'
                '<path d="M239 122L275 107L307 115L342 104L381 126L407 157L378 181L351 171L330 188L300 181L287 162L260 169L246 152Z"/>'
                '<path d="M252 177L280 171L304 193L312 225L295 261L276 290L258 271L246 232L236 209Z"/>'
                '<path d="M348 286L381 279L404 305L388 326L357 320L340 305Z"/>'
                '</g><g fill="none" stroke="#9FD5E5" stroke-width="1.5" opacity="0.65">'
                '<ellipse cx="240" cy="240" rx="72" ry="173"/>'
                '<ellipse cx="240" cy="240" rx="132" ry="173"/>'
                '<path d="M82 172Q240 216 398 172M82 308Q240 352 398 308"/>'
                '</g><path d="M64 240A176 44 0 0 1 416 240" fill="none" stroke="#D6F4FA" '
                'stroke-width="3" stroke-dasharray="7 6"/>'
                '<path d="M64 240A176 44 0 0 0 416 240" fill="none" stroke="#D6F4FA" stroke-width="3"/>'
                '<path d="M240 18V462" fill="none" stroke="#F5C978" stroke-width="3" stroke-dasharray="7 5"/>'
                '<circle cx="240" cy="67" r="5" fill="#F5C978"/>'
                '<circle cx="240" cy="413" r="5" fill="#F5C978"/></g>',
        'landmarks': {'globe_center': [240, 240], 'unrotated_north_pole': [240, 67],
                      'unrotated_south_pole': [240, 413], 'unrotated_axis_endpoints': [[240, 18], [240, 462]],
                      'unrotated_equator_endpoints': [[64, 240], [416, 240]],
                      'intrinsic_tilt_degrees': -18, 'equatorial_radius_px': 176, 'polar_radius_px': 173},
        'notes': 'Static three-quarter diagram with schematic continent silhouettes, separate gold axis and pale equator. '
                 'Flattening is slightly exaggerated for legibility, not a quantitative Earth model. '
                 'Landmark coordinates are before the intrinsic -18-degree rotation around [240,240]; '
                 'the frontier owns scene placement, force arrows and animation.'
    }
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    requests = read(args.requests)['requests']
    rows = []
    for request_id, part in PARTS.items():
        matches = [row for row in requests if row['request_id'] == request_id]
        if not matches:
            raise ValueError('No matching demand for ' + request_id)
        asset_id = matches[0]['requested_asset_id']
        if any(row['requested_asset_id'] != asset_id for row in matches):
            raise ValueError('Conflicting requested identity: ' + request_id)
        capabilities = sorted(set().union(*(set(row['required_capabilities']) for row in matches)))
        width, height = part['width'], part['height']
        data = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                f'viewBox="0 0 {width} {height}"><title>{html.escape(asset_id)}</title>'
                f'<desc>{html.escape(part["notes"])}</desc>{part["body"]}</svg>\n').encode()
        validate_svg(data)
        path = args.output / (request_id + '.svg')
        if path.exists() and path.read_bytes() != data:
            raise ValueError('Existing candidate differs; choose another output directory')
        path.write_bytes(data)
        metadata = {'pivot': matches[0]['svg_requirements']['pivot'], 'capabilities': capabilities,
                    'semantic_landmarks': part['landmarks'],
                    'provenance': 'Original hand-authored SVG for the circular-motion animation pilot. ' +
                                  part['notes'] + ' Intrinsic SVG landmark coordinates: ' + json.dumps(part['landmarks'], sort_keys=True)}
        write(args.output / (request_id + '.metadata.json'), metadata)
        rows.append({'request_id': request_id, 'asset_id': asset_id, 'sha256': digest(data),
                     'landmarks': part['landmarks'], 'notes': part['notes']})
    write(args.output / 'authored-assets.json', {'objects': rows, 'inspection': 'pending', 'registration': 'pending'})
    cards = ''.join('<figure><div class="asset">' + (args.output / (r['request_id'] + '.svg')).read_text() +
                    '</div><figcaption><strong>' + html.escape(r['asset_id']) + '</strong><p>' +
                    html.escape(r['notes']) + '</p></figcaption></figure>' for r in rows)
    (args.output / 'report.html').write_text('<!doctype html><html><head><meta charset="utf-8"><title>Circular-motion assets</title>'
        '<style>body{background:#101820;color:#e6f2fa;font:16px system-ui;margin:28px}main{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}'
        'figure{margin:0;padding:20px;border:1px solid #476478;border-radius:12px}.asset{height:350px;display:flex;align-items:center;justify-content:center}'
        '.asset svg{max-width:100%;max-height:100%}figcaption{overflow-wrap:anywhere}p{line-height:1.5;color:#b9cbd5}</style></head>'
        '<body><h1>C02: reusable circular-motion components</h1><p>Intrinsic hand-authored SVG assets. Scene placement and motion remain model planned.</p><main>' + cards + '</main></body></html>')
    write(args.output / 'public-report.json', {'schema_version': 'infofilm.public-experiment-report.v1',
                                             'files': ['report.html', 'authored-assets.json', 'contact-sheet.png']})
    print(args.output / 'report.html')


if __name__ == '__main__':
    main()
