import unittest

from infofilm.animation_review_geometry import placement_geometry


def context(kind='line', pivot=None):
    return {'output_schema': {'schema_version': 'infofilm.animation-placement.v1'},
            'canvas_plan': {'viewport': {'width': 1280, 'height': 720}},
            'accepted_components': {'objects': [{'id': 'example', 'kind': kind, 'asset_id': 'asset'}]},
            'resolved_assets': {'asset': {'pivot': pivot or [.5, .5]}}}


class PlacementGeometryTests(unittest.TestCase):
    def test_vertical_line_uses_pixel_width_after_rotation(self):
        value = {'objects': [{'id': 'example', 'x': .323219, 'y': .243333,
                             'width': .075, 'height': .002, 'rotation': 90}]}
        row = placement_geometry(value, context())['objects'][0]
        self.assertEqual(row['unrotated_size_px'], [96, 1.44])
        self.assertAlmostEqual(row['native_line_endpoints_px'][0][1], .180833 * 720, places=3)
        self.assertAlmostEqual(row['native_line_endpoints_px'][1][1], .305833 * 720, places=3)
        # The nominal box reaches the release-ball center at y=.31.
        self.assertAlmostEqual(max(p[1] for p in row['rotated_box_corners_px']), .31 * 720, places=3)

    def test_tail_pivot_and_arrow_shaft_match_renderer(self):
        value = {'objects': [{'id': 'example', 'x': .5, 'y': .5,
                             'width': .1, 'height': .02, 'rotation': -90,
                             'pivot': [0, .5], 'stroke_width': 4, 'arrow_end': True}]}
        row = placement_geometry(value, context())['objects'][0]
        self.assertEqual(row['native_line_endpoints_px'], [[640, 356], [640, 252]])
        self.assertIn('arrow_marker_note', row)

    def test_svg_uses_asset_pivot_and_reports_box_not_artwork(self):
        value = {'objects': [{'id': 'example', 'x': .5, 'y': .5,
                             'width': .1, 'height': .1, 'rotation': 0}]}
        row = placement_geometry(value, context('svg', [0, 0]))['objects'][0]
        self.assertEqual(row['rotated_box_corners_px'], [[640, 360], [768, 360], [768, 432], [640, 432]])
        self.assertNotIn('native_line_endpoints_px', row)

    def test_other_phases_do_not_get_placement_measurements(self):
        self.assertIsNone(placement_geometry({}, {'output_schema': {'schema_version': 'another-phase'}}))


if __name__ == '__main__':
    unittest.main()
