"""Planner contracts cannot swap roles, invent anchors, or reset the MFA clock."""
import copy
import math
import unittest

from infofilm.image_animation_contracts import (
    CANVAS_SCHEMA, GROUNDING_SCHEMA, MOTION_SCHEMA, canvas_errors,
    compile_cues, grounding_errors, grounding_incomplete_flags,
)
from infofilm.image_animation_controllers import compile_scene, compile_timeline, value_at


def fixture():
    canvas = {'id': 'K01', 'beat_ids': ['B01'], 'visual_message': 'Follow a circle',
        'controller_type': 'orbit_path', 'prompt': 'One ball and a circular guide',
        'component_roles': [{'role': 'orbiter', 'description': 'A moving ball',
            'required_landmarks': ['centre_px']}]}
    part = {'id': 'ball', 'role': 'orbiter', 'prompt': 'Complete ball in its original source placement',
        'bbox_xyxy_px': [680, 330, 720, 370], 'landmarks': {'centre_px': [700, 350]}, 'z_index': 5}
    scene = {'schema_version': 'infofilm.image-animation-scene.v1', 'canvas': {'width': 1000, 'height': 700},
        'assets': {'background': {'path': 'background'}, 'ball': {'path': 'ball',
            'bbox_xyxy_px': copy.deepcopy(part['bbox_xyxy_px']), 'landmarks': copy.deepcopy(part['landmarks']), 'z_index': 5}},
        'controller': {'type': 'orbit_path', 'parameters': {'center_px': [500, 350], 'start_px': [700, 350],
            'radius_px': 200}, 'bindings': {'orbiter': 'ball'}}, 'incomplete_flags': []}
    grounding = {'schema_version': GROUNDING_SCHEMA['schema_version'], 'canvas_id': 'K01',
        'background_prompt': 'Remove the ball and preserve the guide', 'parts': [part], 'scene': scene,
        'uncertainties': []}
    return canvas, grounding


def cues(kind='move', **changes):
    action = {'id': 'motion', 'kind': kind, 'start_word_id': 'w01', 'end_word_id': 'w02',
        'value_end': 180, 'label': 'Half a revolution', 'purpose': 'Show the changing direction'}
    action.update(changes)
    return {'schema_version': MOTION_SCHEMA['schema_version'], 'beat_id': 'B01', 'actions': [action]}


WORDS = [{'id': 'w01', 'start': .2, 'end': .6}, {'id': 'w02', 'start': .8, 'end': 1.4}]


class GroundingContractTests(unittest.TestCase):
    def test_valid_grounding_and_validator_do_not_mutate_model_response(self):
        canvas, value = fixture(); original = copy.deepcopy(value)
        self.assertEqual(grounding_errors(value, canvas, (1000, 700)), [])
        self.assertEqual(value, original)

    def test_part_scene_bbox_landmarks_and_z_order_must_agree(self):
        for key, replacement in (('bbox_xyxy_px', [670, 330, 720, 370]),
                ('landmarks', {'centre_px': [701, 350]}), ('z_index', 6)):
            canvas, value = fixture(); value['scene']['assets']['ball'][key] = replacement
            with self.subTest(key=key):
                self.assertTrue(any('disagree' in error or 'z_index' in error for error in grounding_errors(value, canvas, (1000, 700))))

    def test_valid_geometry_cannot_swap_part_roles(self):
        canvas, value = fixture()
        canvas['controller_type'] = 'shared_projectile_drop'
        canvas['component_roles'] = [{'role': r, 'description': r + ' ball', 'required_landmarks': []} for r in ('fired', 'dropped')]
        value['parts'] = [{'id': r, 'role': r, 'prompt': 'Complete ' + r + ' ball',
            'bbox_xyxy_px': [80 if r == 'fired' else 30, 80, 120 if r == 'fired' else 70, 120],
            'landmarks': {}, 'z_index': 5} for r in ('fired', 'dropped')]
        value['scene']['assets'] = {'background': {'path': 'background'}, **{p['id']: {
            'path': p['id'], 'bbox_xyxy_px': p['bbox_xyxy_px'], 'landmarks': {}, 'z_index': 5} for p in value['parts']}}
        value['scene']['controller'] = {'type': 'shared_projectile_drop',
            'parameters': {'fired_start_px': [100, 100], 'dropped_start_px': [50, 100], 'ground_y_px': 650,
                'radius_px': 20, 'horizontal_range_px': 700}, 'bindings': {'fired': 'dropped', 'dropped': 'fired'}}
        self.assertTrue(all(compile_scene(value['scene'])['geometry_checks'].values()))
        self.assertIn('Controller bindings must associate each requested role with its own part ID',
            grounding_errors(value, canvas, (1000, 700)))

    def test_required_source_landmark_cannot_be_missing_or_outside_canvas(self):
        for landmarks in ({}, {'centre_px': [1001, 350]}, {'centre_px': [math.nan, 350]}):
            canvas, value = fixture()
            value['parts'][0]['landmarks'] = landmarks
            value['scene']['assets']['ball']['landmarks'] = copy.deepcopy(landmarks)
            with self.subTest(landmarks=landmarks):
                self.assertTrue(grounding_errors(value, canvas, (1000, 700)))

    def test_explicit_flag_is_valid_request_and_still_blocks_final_controller(self):
        canvas, value = fixture(); value['scene']['incomplete_flags'] = ['complete hidden ball silhouette needed']
        self.assertEqual(grounding_errors(value, canvas, (1000, 700)), [])
        self.assertEqual(grounding_incomplete_flags(value), ['complete hidden ball silhouette needed'])
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            compile_scene(value['scene'])

    def test_unresolved_geometry_flags_are_derived_without_mutation_and_caveats_are_allowed(self):
        canvas, value = fixture()
        value['uncertainties'] = [{'kind': 'caveat', 'description': 'Orbit speed is slowed for teaching'},
            {'kind': 'unresolved_geometry', 'field': 'assets.ball', 'description': 'Hidden source edge needs inspection'}]
        original = copy.deepcopy(value)
        self.assertEqual(grounding_errors(value, canvas, (1000, 700)), [])
        flags = grounding_incomplete_flags(value)
        self.assertEqual(flags, ['unresolved_geometry: assets.ball: Hidden source edge needs inspection'])
        self.assertEqual(value, original)
        value['uncertainties'] = [{'kind': 'caveat', 'description': 'Idealisation ignores air resistance'}]
        self.assertEqual(grounding_incomplete_flags(value), [])
        value['uncertainties'] = ['Unmeasured rotation anchor']
        self.assertTrue(grounding_incomplete_flags(value))

    def test_malformed_model_fields_return_errors_instead_of_attribute_errors(self):
        for field, bad in (('controller', []), ('controller', 'code'), ('assets', []), ('assets', {'background': None})):
            canvas, value = fixture(); value['scene'][field] = bad
            with self.subTest(field=field, bad=bad):
                self.assertTrue(grounding_errors(value, canvas, (1000, 700)))
        for bad in (None, {}, [{'kind': [], 'description': 'Invalid classification'}], ['']):
            canvas, value = fixture(); value['uncertainties'] = bad
            with self.subTest(uncertainties=bad):
                self.assertTrue(grounding_errors(value, canvas, (1000, 700)))

    def test_static_canvas_uses_no_part_bindings_and_preserves_nonessential_caveats(self):
        canvas, value = fixture(); canvas.update(controller_type='static_canvas', component_roles=[])
        value['parts'] = []
        value['scene']['assets'] = {'background': {'path': 'background'}}
        value['scene']['controller'] = {'type': 'static_canvas', 'parameters': {}, 'bindings': {}}
        value['uncertainties'] = [{'kind': 'caveat', 'description': 'This introductory map omits local roads'}]
        self.assertEqual(grounding_errors(value, canvas, (1000, 700)), [])


class CanvasContractTests(unittest.TestCase):
    def test_controller_type_rejects_prose_and_mixed_choices(self):
        for controller_type in ('orbit_path|static_canvas', 'orbit_path or static_canvas',
                'orbit_path for a surface-marker proxy; requested globe_rotation_daynight controller',
                'static_canvas for the illustrated comparison; requested fixed_axis_orbit controller'):
            canvas, _ = fixture(); canvas['controller_type'] = controller_type
            value = {'schema_version': CANVAS_SCHEMA['schema_version'], 'canvases': [canvas]}
            original = copy.deepcopy(value)
            with self.subTest(controller_type=controller_type):
                errors = canvas_errors(value, {'teaching_sequence': [{'id': 'B01'}]})
                self.assertTrue(any('controller_type needs one safe controller ID' in error for error in errors))
                self.assertEqual(value, original)

    def test_supported_controller_keeps_literal_roles_and_requires_its_role_set(self):
        canvas, _ = fixture()
        canvas['limitations'] = ['The path is diagrammatic; a future controller may model tilted-axis rotation.']
        value = {'schema_version': CANVAS_SCHEMA['schema_version'], 'canvases': [canvas]}
        teaching = {'teaching_sequence': [{'id': 'B01'}]}
        original = copy.deepcopy(value)
        self.assertEqual(canvas_errors(value, teaching), [])
        self.assertEqual(value, original)
        canvas['component_roles'] = []
        self.assertTrue(any('omits controller roles' in error for error in canvas_errors(value, teaching)))
        canvas['component_roles'] = [{'role': 'requested_orbiter', 'description': 'A moving ball',
                                     'required_landmarks': ['centre_px']}]
        errors = canvas_errors(value, teaching)
        self.assertTrue(any('omits controller roles' in error for error in errors))
        self.assertTrue(any('unsupported controller roles' in error for error in errors))

    def test_required_engine_landmarks_and_role_set_are_enforced_before_images(self):
        canvas, _ = fixture(); canvas['controller_type'] = 'ideal_slider_crank_four_stroke'
        canvas['component_roles'] = [{'role': role, 'description': role, 'required_landmarks': []}
            for role in ('piston', 'crankshaft', 'intake_valve', 'exhaust_valve')]
        value = {'schema_version': CANVAS_SCHEMA['schema_version'], 'canvases': [canvas]}
        teaching = {'teaching_sequence': [{'id': 'B01'}]}
        errors = canvas_errors(value, teaching)
        self.assertTrue(any('piston' in e and 'landmarks' in e for e in errors))
        self.assertTrue(any('crankshaft' in e and 'landmarks' in e for e in errors))
        canvas['component_roles'][0]['required_landmarks'] = ['wrist_pin_px']
        canvas['component_roles'][1]['required_landmarks'] = ['rotation_anchor_px']
        self.assertEqual(canvas_errors(value, teaching), [])
        canvas['component_roles'].append({'role': 'mystery', 'description': 'Unsupported moving shape', 'required_landmarks': []})
        self.assertTrue(any('unsupported' in e for e in canvas_errors(value, teaching)))

    def test_unsupported_controller_is_preserved_for_a_reviewable_request(self):
        canvas, _ = fixture(); canvas['controller_type'] = 'new_reviewed_mechanism'
        value = {'schema_version': CANVAS_SCHEMA['schema_version'], 'canvases': [canvas]}
        original = copy.deepcopy(value)
        self.assertEqual(canvas_errors(value, {'teaching_sequence': [{'id': 'B01'}]}), [])
        self.assertEqual(value, original)


class CueContractTests(unittest.TestCase):
    def setUp(self):
        _, value = fixture(); self.controller = compile_scene(value['scene'])

    def test_words_determine_seconds_and_accepted_title_labels_are_preserved(self):
        value = cues(); original = copy.deepcopy(value)
        result = compile_cues(value, 'B01', WORDS, 0, 2, self.controller,
            title='Why direction changes', display_labels=['Constant radius'])
        row = result['scenes'][0]
        self.assertEqual((row['actions'][0]['start'], row['actions'][0]['end']), (.2, 1.4))
        self.assertEqual(row['actions'][0]['word_ids'], ['w01', 'w02'])
        self.assertEqual(row['title'], 'Why direction changes')
        self.assertEqual(row['display_labels'], ['Constant radius'])
        self.assertEqual(value, original)

    def test_missing_nonfinite_string_and_boolean_movement_endpoints_are_rejected(self):
        for endpoint in (None, math.nan, math.inf, '180', True):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, 'finite value_end'):
                compile_cues(cues(value_end=endpoint), 'B01', WORDS, 0, 2, self.controller)
        value = cues(); value['actions'][0].pop('value_end')
        with self.assertRaisesRegex(ValueError, 'finite value_end'):
            compile_cues(value, 'B01', WORDS, 0, 2, self.controller)

    def test_null_optional_fields_are_removed_and_hold_inherits_state(self):
        controller = {**self.controller, 'initial_value': 180}
        result = compile_cues(cues('hold', value_start=None, value_end=None, loops=None), 'B01', WORDS, 0, 2, controller)
        action = result['scenes'][0]['actions'][0]
        self.assertNotIn('value_start', action); self.assertNotIn('value_end', action); self.assertNotIn('loops', action)
        compiled = compile_timeline(result, controller)
        self.assertEqual(compiled['scenes'][0]['final_value'], 180)
        self.assertEqual(value_at(1, compiled['scenes'][0]['segments']), 180)

    def test_separate_beats_carry_motion_without_injected_reset(self):
        first = compile_cues(cues(), 'B01', WORDS, 0, 2, self.controller)
        first_state = compile_timeline(first, self.controller)['scenes'][0]['final_value']
        self.assertEqual(first_state, 180)
        next_controller = {**self.controller, 'initial_value': first_state}
        second_words = [{'id': 'w03', 'start': 2.2, 'end': 2.6}, {'id': 'w04', 'start': 2.8, 'end': 3.4}]
        second_cues = {'schema_version': MOTION_SCHEMA['schema_version'], 'beat_id': 'B02', 'actions': [{
            **cues()['actions'][0], 'start_word_id': 'w03', 'end_word_id': 'w04', 'value_end': 360}]}
        second = compile_cues(second_cues, 'B02', second_words, 2, 4, next_controller)
        final = compile_timeline(second, next_controller)['scenes'][0]['final_value']
        self.assertEqual(final, 360)
        row = copy.deepcopy(second['scenes'][0]); row.update(start=2, end=4)
        for action in row['actions']:
            action['start'] += 2; action['end'] += 2
        combined = {'duration_seconds': 4, 'scenes': [first['scenes'][0], row]}
        compiled = compile_timeline(combined, self.controller)
        self.assertEqual(compiled['frames'], 120)
        self.assertEqual(compiled['scenes'][1]['initial_value'], 180)
        self.assertFalse(any(s['kind'] == 'replay' for r in compiled['scenes'] for s in r['segments']))
        self.assertEqual(self.controller['initial_value'], 0)

    def test_explicit_replay_and_periodic_loop_have_distinct_end_states(self):
        loop = compile_cues(cues('loop', value_end=360, loops=2), 'B01', WORDS, 0, 2, self.controller)
        self.assertEqual(compile_timeline(loop, self.controller)['scenes'][0]['final_value'], 720)
        carried = {**self.controller, 'initial_value': 180}
        replay = compile_cues(cues('replay', value_start=0, value_end=360, loops=2), 'B01', WORDS, 0, 2, carried)
        self.assertEqual(compile_timeline(replay, carried)['scenes'][0]['final_value'], 360)
        with self.assertRaisesRegex(ValueError, 'Unexplained motion reset'):
            compile_cues(cues(value_start=0), 'B01', WORDS, 0, 2, carried)

    def test_static_labels_and_empty_beat_need_no_motion_endpoint(self):
        _, grounding = fixture(); grounding['scene']['controller'] = {'type': 'static_canvas', 'parameters': {}, 'bindings': {}}
        controller = compile_scene(grounding['scene'])
        labels = compile_cues(cues('label', value_end=None), 'B01', WORDS, 0, 2, controller)
        self.assertEqual(compile_timeline(labels, controller)['scenes'][0]['final_value'], 0)
        empty = {'schema_version': MOTION_SCHEMA['schema_version'], 'beat_id': 'B01', 'actions': []}
        self.assertEqual(compile_timeline(compile_cues(empty, 'B01', WORDS, 0, 2, controller), controller)['frames'], 60)

    def test_unsafe_ids_unknown_words_reversed_windows_and_duplicate_words_rejected(self):
        for changes in ({'id': '../unsafe'}, {'kind': 'python'}, {'start_word_id': 'unknown'},
                {'start_word_id': 'w02', 'end_word_id': 'w01'}, {'end_word_id': []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                compile_cues(cues(**changes), 'B01', WORDS, 0, 2, self.controller)
        with self.assertRaisesRegex(ValueError, 'unique'):
            compile_cues(cues(), 'B01', WORDS+[WORDS[0]], 0, 2, self.controller)
        with self.assertRaisesRegex(ValueError, 'narration window'):
            compile_cues(cues(), 'B01', WORDS, .5, 2, self.controller)


if __name__ == '__main__':
    unittest.main()
