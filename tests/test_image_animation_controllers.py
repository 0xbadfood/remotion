"""Physical invariants, MFA clock ownership, rig readiness, and bundle safety."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from infofilm.image_animation_controllers import (
    SCENE_SCHEMA, MissingControllerError, compile_scene, compile_timeline,
    controller_capabilities, cycle_state, normalise_pilot_scene,
    normalise_pilot_timings, resolve_asset, slider_crank, state_at, value_at,
)
from infofilm.image_animation_renderer import canvas_layout, prepare_assets


def scene(controller_type="orbit_path"):
    assets = {"background": {"path": "background.png"}, "ball": {"path": "ball.png"}}
    controller = {"type": controller_type, "bindings": {"orbiter": "ball"},
        "parameters": {"center_px": [500, 350], "start_px": [700, 350], "radius_px": 200}}
    if controller_type == "shared_projectile_drop":
        assets.update(fired={"path": "fired.png"}, dropped={"path": "dropped.png"})
        controller.update(bindings={"fired": "fired", "dropped": "dropped"},
            parameters={"fired_start_px": [100, 100], "dropped_start_px": [50, 100],
                "ground_y_px": 650, "radius_px": 20, "horizontal_range_px": 700})
    elif controller_type == "ideal_slider_crank_four_stroke":
        assets.update(piston={"path": "piston.png", "landmarks": {"wrist_pin_px": [500, 350]}},
            crankshaft={"path": "crank.png", "landmarks": {"rotation_anchor_px": [500, 550]}},
            intake_valve={"path": "intake.png"}, exhaust_valve={"path": "exhaust.png"})
        controller.update(bindings={r: r for r in ("piston", "crankshaft", "intake_valve", "exhaust_valve")},
            parameters={"crank_center_px": [500, 550], "crank_radius_px": 60,
                "rod_length_px": 200, "valve_lift_px": 12})
    elif controller_type == "static_canvas":
        controller.update(parameters={}, bindings={})
    return {"schema_version": SCENE_SCHEMA, "canvas": {"width": 1000, "height": 700},
        "assets": assets, "controller": controller, "incomplete_flags": []}


def timeline(actions=None, duration=5):
    return {"duration_seconds": duration, "scenes": [{"id": "S01", "title": "A scene",
        "start": 0, "end": duration, "actions": actions or []}]}


class ControllerGeometryTests(unittest.TestCase):
    def test_orbit_radius_closure_and_actual_direction(self):
        compiled = compile_scene(scene())
        center, radius = [500, 350], 200
        for degree in range(0, 1081, 7):
            self.assertAlmostEqual(math.dist(state_at(compiled, degree)["positions_px"]["orbiter"], center), radius)
        self.assertLess(state_at(compiled, 90)["positions_px"]["orbiter"][1], center[1])
        self.assertLess(math.dist(state_at(compiled, 0)["positions_px"]["orbiter"], state_at(compiled, 360)["positions_px"]["orbiter"]), 1e-7)
        source = scene(); source["controller"]["parameters"]["direction"] = "clockwise"
        self.assertGreater(state_at(compile_scene(source), 90)["positions_px"]["orbiter"][1], center[1])

    def test_orbit_infers_circle_radius_from_measured_center_and_start(self):
        source = scene()
        source["controller"]["parameters"].pop("radius_px")
        source["controller"]["parameters"]["start_px"] = [620, 510]
        compiled = compile_scene(source)
        self.assertEqual(compiled["parameters"]["radius_xy_px"], [200, 200])
        for degree in range(0, 361, 15):
            self.assertAlmostEqual(math.dist(state_at(compiled, degree)["positions_px"]["orbiter"], [500, 350]), 200)
        self.assertLess(math.dist(state_at(compiled, 0)["positions_px"]["orbiter"],
                                 state_at(compiled, 360)["positions_px"]["orbiter"]), 1e-7)

    def test_orbit_inferred_radius_rejects_coincident_anchors(self):
        source = scene()
        source["controller"]["parameters"].pop("radius_px")
        source["controller"]["parameters"]["start_px"] = [500, 350]
        with self.assertRaisesRegex(ValueError, "radii must be positive"):
            compile_scene(source)

    def test_orbit_explicit_wrong_radius_is_not_replaced_by_inference(self):
        for explicit in ({"radius_px": 160}, {"radius_xy_px": [180, 90]}):
            source = scene()
            source["controller"]["parameters"].pop("radius_px")
            source["controller"]["parameters"].update(explicit)
            with self.subTest(explicit=explicit), self.assertRaisesRegex(ValueError, "start must lie"):
                compile_scene(source)

    def test_ellipse_equation_and_tangent_orthogonality_to_gradient(self):
        source = scene(); source["controller"]["parameters"]["radius_xy_px"] = [200, 100]
        compiled = compile_scene(source)
        for degree in range(0, 361, 9):
            state = state_at(compiled, degree)
            x, y = state["positions_px"]["orbiter"]
            self.assertAlmostEqual(((x-500)/200)**2+((y-350)/100)**2, 1)
            tangent = state["tangent_px"]
            self.assertAlmostEqual((x-500)/200**2*tangent[0]+(y-350)/100**2*tangent[1], 0)

    def test_orbit_off_path_start_invalid_radii_and_canvas_escape_rejected(self):
        for change in ({"start_px": [700, 450]}, {"radius_px": 0}, {"radius_px": math.inf},
                       {"center_px": [100, 100]}, {"direction": "random"}):
            source = scene(); source["controller"]["parameters"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                compile_scene(source)

    def test_shared_drop_contact_quadratic_fall_and_horizontal_independence(self):
        compiled = compile_scene(scene("shared_projectile_drop"))
        for i in range(101):
            fired, dropped = state_at(compiled, i/100)["positions_px"].values()
            self.assertEqual(fired[1], dropped[1]); self.assertEqual(dropped[0], 50)
            self.assertAlmostEqual(fired[0], 100+700*i/100)
            self.assertAlmostEqual(fired[1], 100+530*(i/100)**2)
        self.assertEqual(state_at(compiled, 1)["positions_px"]["fired"][1]+20, 650)
        with self.assertRaisesRegex(ValueError, "progress"):
            state_at(compiled, 1.01)

    def test_unequal_launch_heights_and_outside_landing_rejected(self):
        for update in ({"dropped_start_px": [50, 120]}, {"horizontal_range_px": 1000}, {"ground_y_px": 110}):
            source = scene("shared_projectile_drop"); source["controller"]["parameters"].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                compile_scene(source)

    def test_slider_crank_length_stroke_and_four_stroke_valves(self):
        compiled = compile_scene(scene("ideal_slider_crank_four_stroke"))
        p = compiled["parameters"]
        for degree in range(0, 1441, 2):
            state = cycle_state(math.radians(degree), p)
            self.assertAlmostEqual(math.dist(state["wrist_pin_px"], state["crank_pin_px"]), 200)
            self.assertEqual(state["wrist_pin_px"][0], 500)
            if state["stroke"] in {"compression", "power"}:
                self.assertEqual(state["intake_lift_px"], 0); self.assertEqual(state["exhaust_lift_px"], 0)
        self.assertAlmostEqual(slider_crank(math.pi, p)["wrist_pin_px"][1]-slider_crank(0, p)["wrist_pin_px"][1], 120)
        self.assertTrue(all(compiled["geometry_checks"].values()))

    def test_in_line_axis_impossible_rod_and_missing_landmarks_stop(self):
        for update in ({"axis_x_px": 490}, {"rod_length_px": 50}, {"valve_lift_px": -1}):
            source = scene("ideal_slider_crank_four_stroke"); source["controller"]["parameters"].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                compile_scene(source)
        source = scene("ideal_slider_crank_four_stroke"); source["assets"]["piston"].pop("landmarks")
        with self.assertRaisesRegex(ValueError, "missing required source landmark"):
            compile_scene(source)

    def test_missing_controller_is_a_reviewable_request(self):
        source = scene("unrestricted_model_code")
        with self.assertRaises(MissingControllerError) as context:
            compile_scene(source)
        self.assertEqual(context.exception.request["status"], "missing_controller")
        self.assertIn("orbit_path", context.exception.request["supported_controllers"])

    def test_incomplete_flags_block_final_and_only_explicit_preview_compiles(self):
        source = scene(); source["incomplete_flags"] = ["missing hidden silhouette"]
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            compile_scene(source)
        self.assertTrue(compile_scene(source, allow_incomplete_preview=True)["preview_incomplete"])

    def test_capabilities_are_copied_and_compile_does_not_mutate_scene(self):
        capability = controller_capabilities(); capability["orbit_path"]["roles"].append("wrong")
        self.assertNotIn("wrong", controller_capabilities()["orbit_path"]["roles"])
        source = scene(); saved = copy.deepcopy(source); compile_scene(source)
        self.assertEqual(source, saved)


class NarrationClockTests(unittest.TestCase):
    def test_explicit_motion_inserts_holds_and_has_exact_frame_ownership(self):
        data = {"duration_seconds": 3.12, "scenes": [
            {"id": "S01", "start": 0, "end": 1.56, "actions": [{"id": "orbit", "kind": "move",
                "start": .2, "end": 1.2, "value_start": 0, "value_end": 360}]},
            {"id": "S02", "start": 1.56, "end": 3.12, "actions": []}]}
        compiled = compile_timeline(data, compile_scene(scene()), fps=10)
        frames = [i for row in compiled["scenes"] for i in range(row["start_frame"], row["end_frame"])]
        self.assertEqual(frames, list(range(31)))
        self.assertEqual([s["kind"] for s in compiled["scenes"][0]["segments"]], ["hold", "move", "hold"])
        self.assertEqual(value_at(2, compiled["scenes"][1]["segments"]), 360)
        self.assertEqual(data["scenes"][0]["actions"][0]["kind"], "move")

    def test_loops_are_continuous_and_replay_resets_explicitly(self):
        data = timeline([{ "id": "loop", "kind": "loop", "start": 0, "end": 4,
            "value_start": 0, "value_end": 360, "loops": 2}])
        segments = compile_timeline(data, compile_scene(scene()))["scenes"][0]["segments"]
        self.assertEqual(value_at(1, segments), 180)
        self.assertEqual(value_at(3, segments), 540)
        self.assertEqual(value_at(5, segments), 720)
        projectile = compile_scene(scene("shared_projectile_drop"))
        data = timeline([{ "id": "replay", "kind": "replay", "start": 0, "end": 4,
            "value_start": 0, "value_end": 1, "loops": 2}])
        segments = compile_timeline(data, projectile)["scenes"][0]["segments"]
        self.assertAlmostEqual(value_at(1.5, segments), .75)
        self.assertAlmostEqual(value_at(2.5, segments), .25)
        self.assertEqual(value_at(4, segments), 1)

    def test_overlaps_unexplained_resets_and_missing_narration_tail_rejected(self):
        for data in (timeline([{ "id": "move", "kind": "move", "start": 0, "end": 4,
                "value_start": 90, "value_end": 360}]),
            timeline([{ "id": "one", "kind": "move", "start": 0, "end": 4,
                "value_start": 0, "value_end": 360}, {"id": "two", "kind": "move", "start": 3, "end": 5,
                "value_start": 360, "value_end": 720}]),
            {"duration_seconds": 6, "scenes": timeline()["scenes"]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                compile_timeline(data, compile_scene(scene()))

    def test_unknown_action_cannot_execute_code(self):
        for kind in ("python", "lambda", "eval", "arbitrary_path"):
            data = timeline([{ "id": "cue", "kind": kind, "start": 1, "end": 2}])
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "Unsupported"):
                compile_timeline(data, compile_scene(scene()))

    def test_path_traversal_scene_ids_gaps_and_duplicate_ids_rejected(self):
        for field, value in (("id", "../escape"), ("start", 1)):
            data = timeline(); data["scenes"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                compile_timeline(data, compile_scene(scene()))
        data = timeline(); data["scenes"].append({"id": "S01", "start": 5, "end": 6})
        with self.assertRaisesRegex(ValueError, "unique"):
            compile_timeline(data, compile_scene(scene()))

    def test_static_canvas_can_hold_and_label(self):
        static = compile_scene(scene("static_canvas"))
        data = timeline([{ "id": "cue", "kind": "label", "start": 1, "end": 3, "label": "Introduction"}])
        self.assertEqual(compile_timeline(data, static)["frames"], 150)
        data["scenes"][0]["actions"][0].update(kind="move", value_end=1)
        with self.assertRaisesRegex(ValueError, "Static canvas"):
            compile_timeline(data, static)


class AssetAndLayoutTests(unittest.TestCase):
    def test_both_canvas_orientations_fit_diagram_and_labels_without_overlap(self):
        for width, height in ((1280, 720), (720, 1280), (320, 180), (180, 320)):
            layout = canvas_layout(1672, 941, width, height)
            self.assertTrue(all(layout["checks"].values()))
            x1, y1, x2, y2 = layout["diagram_bounds"]
            self.assertAlmostEqual((x2-x1)/(y2-y1), 1672/941)

    def test_relative_asset_escape_and_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); nested = base / "bundle"; nested.mkdir()
            outside = base / "outside.png"; outside.write_bytes(b"test")
            (nested / "escape.png").symlink_to(outside)
            for value in ("../outside.png", "escape.png"):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "escapes"):
                    resolve_asset(value, nested)
            self.assertEqual(resolve_asset(outside, nested), outside)

    def test_cropped_alpha_padding_preserves_source_scale_and_placement(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            Image.new("RGB", (1000, 700), "white").save(base / "background.png")
            image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
            image.paste((255, 0, 0, 255), (20, 30, 80, 70)); image.save(base / "ball.png")
            source = scene(); source["assets"]["ball"]["bbox_xyxy_px"] = [600, 250, 800, 450]
            assets = prepare_assets(source, base / "scene.json", base / "prepared")
            self.assertEqual(assets["ball"]["size_px"], [120, 80])
            self.assertEqual(assets["ball"]["centre_px"], [700, 350])
            with Image.open(assets["ball"]["path"]) as cropped:
                self.assertEqual(cropped.size, (60, 40))

    def test_opaque_layer_and_wrong_background_dimensions_stop_before_render(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            Image.new("RGB", (1000, 700), "white").save(base / "background.png")
            Image.new("RGBA", (100, 100), "red").save(base / "ball.png")
            source = scene(); source["assets"]["ball"]["bbox_xyxy_px"] = [600, 250, 800, 450]
            with self.assertRaisesRegex(ValueError, "transparent RGBA"):
                prepare_assets(source, base / "scene.json", base / "prepared")
            Image.new("RGB", (800, 600), "white").save(base / "background.png")
            with self.assertRaisesRegex(ValueError, "Background dimensions"):
                prepare_assets(source, base / "scene.json", base / "prepared")

    def test_frozen_engine_adaptation_preserves_manual_and_occlusion_provenance(self):
        root = Path(__file__).resolve().parent.parent
        pilot_path = root / "artifacts/experiments/four-stroke-image-mggen-manim/scene.json"
        if not pilot_path.exists():
            self.skipTest("Frozen local pilot assets are absent")
        source = normalise_pilot_scene(json.loads(pilot_path.read_text()))
        self.assertTrue(source["provenance"]["manual_calibration"])
        self.assertTrue(source["provenance"]["known_crank_occlusion"])
        self.assertTrue(source["incomplete_flags"])
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            compile_scene(source)
        controller = compile_scene(source, allow_incomplete_preview=True)
        timings = json.loads(pilot_path.with_name("narration-scenes.json").read_text())
        compiled = compile_timeline(normalise_pilot_timings(timings, controller), controller)
        self.assertEqual(len(compiled["scenes"]), 7)
        self.assertEqual(compiled["frames"], round(134.28*30))
        source["incomplete_flags"] = []
        with self.assertRaisesRegex(ValueError, "manual_completion"):
            compile_scene(source)
        source["provenance"]["manual_completion"] = {"method": "reviewed complete source raster", "reason": "Occluded crank silhouette supplied explicitly"}
        self.assertFalse(compile_scene(source)["preview_incomplete"])


if __name__ == "__main__":
    unittest.main()
