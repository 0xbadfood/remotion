"""Renderer-derived placement measurements for semantic reviewers.

These measurements describe geometry, not whether a teaching plan is correct.
Keep native-line endpoints consistent with AnimationExperiment.tsx.
"""
import math


def placement_geometry(value, context):
    if context.get('output_schema', {}).get('schema_version') != 'infofilm.animation-placement.v1':
        return None
    viewport = context['canvas_plan']['viewport']
    width, height = viewport['width'], viewport['height']
    definitions = {o['id']: o for o in context['accepted_components']['objects']}
    assets = context.get('resolved_assets', {})
    rows = []
    for obj in value['objects']:
        definition = definitions[obj['id']]
        asset = assets.get(definition.get('asset_id'), {})
        pivot = obj.get('pivot', asset.get('pivot', [.5, .5]))
        w, h = obj['width'] * width, obj['height'] * height
        x, y = obj['x'] * width, obj['y'] * height
        angle = math.radians(obj.get('rotation', 0))
        c, s = math.cos(angle), math.sin(angle)

        def point(local_x, local_y):
            dx, dy = local_x - pivot[0] * w, local_y - pivot[1] * h
            return [round(x + c * dx - s * dy, 5), round(y + s * dx + c * dy, 5)]

        corners = [point(0, 0), point(w, 0), point(w, h), point(0, h)]
        row = {'id': obj['id'], 'kind': definition['kind'], 'pivot_px': [round(x, 5), round(y, 5)],
               'unrotated_size_px': [round(w, 5), round(h, 5)],
               'rotated_box_corners_px': corners}
        if definition['kind'] == 'line':
            stroke = obj.get('stroke_width', 3)
            row['native_line_endpoints_px'] = [point(stroke, h / 2),
                point(max(stroke, w - stroke * (5 if obj.get('arrow_end') else 1)), h / 2)]
            if obj.get('arrow_end'):
                row['arrow_marker_note'] = 'Endpoints measure the shaft; the SVG marker extends beyond the second endpoint.'
        rows.append(row)
    return {'schema_version': 'infofilm.animation-placement-geometry.v1',
            'viewport_px': [width, height],
            'conventions': [
                'All measurements here are pixels, not normalized frame fractions.',
                'Multiply x and width fractions by canvas WIDTH; y and height fractions by canvas HEIGHT.',
                'Rotate in pixel space around the specified pivot; positive rotation is clockwise on screen.',
                'Native lines run horizontally across their unrotated WIDTH, then rotate. Their length does not become width_fraction * canvas HEIGHT.',
                'Boxes describe element geometry; SVG artwork can be inset within its box. Consult SVG landmarks/provenance for visible artwork.',
                'These are initial placement measurements, not evidence that all objects are visible simultaneously. Motion planning controls visibility.'
            ], 'objects': rows}
