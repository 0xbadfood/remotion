"""One review sheet shows native alpha and source/runtime registration honestly."""
import copy
import math
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from infofilm.image_animation_controllers import MissingControllerError, compile_scene, compile_timeline, state_at, value_at
from infofilm.image_animation_review import build_layer_review_sheet, build_rendered_review_sheet, select_review_times


def fixture(root):
    background = root / "background.png"
    canvas = root / "canvas.png"
    part = root / "planet.png"
    Image.new("RGB", (128, 96), (10, 20, 30)).save(background)
    Image.new("RGB", (128, 96), (20, 30, 40)).save(canvas)
    image = Image.new("RGBA", (128, 96), (160, 0, 180, 0))
    ImageDraw.Draw(image).rectangle((90, 40, 99, 49), fill=(10, 100, 240, 255))
    image.save(part)
    scene = {"schema_version": "infofilm.image-animation-scene.v1", "canvas": {"width": 128, "height": 96},
        "assets": {"background": {"path": background.name}, "planet": {"path": part.name,
            "normalised_size_px": [12, 8], "normalised_centre_px": [32, 32], "z_index": 5}},
        "controller": {"type": "orbit_path", "bindings": {"orbiter": "planet"},
            "parameters": {"center_px": [64, 48], "start_px": [80, 48], "radius_px": 16}}, "incomplete_flags": []}
    return scene, canvas, [background, canvas, part]


class LayerReviewSheetTests(unittest.TestCase):
    def test_hidden_rgb_is_invisible_and_runtime_registration_matches_normalised_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scene, canvas, originals = fixture(root)
            frozen = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals}
            original_scene = copy.deepcopy(scene)
            path, metadata = build_layer_review_sheet(scene, root / "scene.json", canvas, root / "review")
            self.assertEqual(metadata["panel_order"], ["source_canvas", "clean_background", "isolated:planet", "assembled_initial_state"])
            self.assertTrue(all(panel["label"] for panel in metadata["panels"]))
            self.assertEqual(metadata["panels"][2]["label"], "3. ISOLATED PART: planet [orbiter]")
            registration = metadata["registration"]["sprites"]["planet"]
            self.assertEqual(registration["source_centre_px"], [32, 32])
            self.assertEqual(registration["source_size_px"], [12, 8])
            self.assertEqual(registration["runtime"]["centre_px"], [80, 48])
            with Image.open(metadata["assembled_initial_image"]) as assembled:
                self.assertEqual(assembled.getpixel((80, 48)), (10, 100, 240, 255))
                self.assertEqual(assembled.getpixel((74, 44)), (10, 100, 240, 255))
                self.assertEqual(assembled.getpixel((73, 44)), (10, 20, 30, 255))
                self.assertEqual(assembled.getpixel((95, 45)), (10, 20, 30, 255))
            with Image.open(path) as sheet:
                self.assertEqual(sheet.mode, "RGB")
                self.assertNotIn((160, 0, 180), {colour for _, colour in sheet.getcolors(sheet.width * sheet.height)})
                panel = metadata["panels"][2]
                x1, y1, x2, y2 = panel["display_xyxy_px"]
                self.assertEqual(sheet.getpixel(((x1 + x2) // 2, (y1 + y2) // 2)), (10, 100, 240))
                self.assertEqual(sheet.getpixel((12, panel["panel_xyxy_px"][1] + 72)), (224, 228, 232))
                header = sheet.crop((0, 0, 640, 72))
                self.assertGreater(len(header.getcolors(header.width * header.height)), 2)
            self.assertEqual(scene, original_scene)
            self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals}, frozen)

    def test_partial_native_alpha_is_composited_instead_of_flattened_or_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scene, canvas, _ = fixture(root)
            part = Image.new("RGBA", (128, 96), (160, 0, 180, 0))
            ImageDraw.Draw(part).rectangle((90, 40, 99, 49), fill=(220, 40, 80, 128))
            part.save(root / "planet.png")
            _, metadata = build_layer_review_sheet(scene, root / "scene.json", canvas, root / "review")
            expected = Image.alpha_composite(Image.new("RGBA", (1, 1), (10, 20, 30, 255)),
                                            Image.new("RGBA", (1, 1), (220, 40, 80, 128))).getpixel((0, 0))
            with Image.open(metadata["assembled_initial_image"]) as assembled:
                self.assertEqual(assembled.getpixel((80, 48)), expected)
            with Image.open(root / "planet.png") as original:
                self.assertEqual(original.getpixel((95, 45)), (220, 40, 80, 128))
                self.assertEqual(original.getpixel((0, 0)), (160, 0, 180, 0))

    def test_unknown_controllers_and_expression_geometry_are_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scene, canvas, _ = fixture(root)
            scene["controller"]["type"] = "arbitrary_model_code"
            with self.assertRaises(MissingControllerError):
                build_layer_review_sheet(scene, root / "scene.json", canvas, root / "review")
            self.assertFalse((root / "review").exists())
            scene["controller"]["type"] = "orbit_path"
            scene["controller"]["parameters"]["radius_px"] = "1 + 2"
            with self.assertRaisesRegex(ValueError, "finite number"):
                build_layer_review_sheet(scene, root / "scene.json", canvas, root / "review")
            self.assertFalse((root / "review").exists())

    def test_review_destination_cannot_overwrite_an_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scene, canvas, _ = fixture(root)
            output = root / "review"
            prepared = output / "prepared-assets"
            prepared.mkdir(parents=True)
            original = prepared / "planet.png"
            original.write_bytes((root / "planet.png").read_bytes())
            scene["assets"]["planet"]["path"] = str(original)
            frozen = original.read_bytes()
            with self.assertRaisesRegex(ValueError, "overwrite original"):
                build_layer_review_sheet(scene, root / "scene.json", canvas, output)
            self.assertEqual(original.read_bytes(), frozen)


class RenderedReviewTests(unittest.TestCase):
    def controller(self):
        return compile_scene({"schema_version": "infofilm.image-animation-scene.v1", "canvas": {"width": 128, "height": 96},
            "assets": {"background": {"path": "background.png"}, "planet": {"path": "planet.png"}},
            "controller": {"type": "orbit_path", "bindings": {"orbiter": "planet"},
                "parameters": {"center_px": [64, 48], "start_px": [80, 48], "radius_px": 16}}, "incomplete_flags": []})

    def clock(self, actions, duration=22.6, fps=30):
        return compile_timeline({"duration_seconds": duration, "scenes": [
            {"id": "B01", "title": "Earth rotation causes day and night", "start": 0, "end": duration, "actions": actions}]},
            self.controller(), fps=fps)

    def test_closed_orbit_after_halfway_gets_three_actual_interior_motion_samples(self):
        clock = self.clock([{"id": "spin", "kind": "orbit", "start": 11.8167, "end": 17.2367,
                             "value_start": 0, "value_end": 360}])
        original = copy.deepcopy(clock)
        times = select_review_times(clock, 30)
        self.assertEqual(times[0], 0)
        self.assertEqual(times[-1], (clock["frames"] - 1) / 30)
        inside = [time for time in times if 11.8167 < time < 17.2367]
        self.assertEqual(len(inside), 3)
        controller = self.controller()
        initial = state_at(controller, 0)["positions_px"]["orbiter"]
        for time in inside:
            value = value_at(time, clock["scenes"][0]["segments"])
            self.assertGreater(math.dist(initial, state_at(controller, value)["positions_px"]["orbiter"]), 10)
        self.assertEqual(clock, original)

    def test_many_segments_have_bounded_valid_frames_and_multi_loops_do_not_alias(self):
        actions = [{"id": f"orbit-{i}", "kind": "orbit", "start": i, "end": i + .5,
                    "value_start": 360 * i, "value_end": 360 * (i + 1)} for i in range(20)]
        clock = self.clock(actions, duration=20)
        times = select_review_times(clock, 30)
        self.assertLessEqual(len(times), 7)
        self.assertEqual(len(times), len(set(times)))
        self.assertTrue(all(0 <= time <= (clock["frames"] - 1) / 30 for time in times))
        self.assertTrue(all(abs(time * 30 - round(time * 30)) < 1e-8 for time in times))
        self.assertTrue(any(0 < time < .5 for time in times))
        self.assertTrue(any(19 < time < 19.5 for time in times))
        clock = self.clock([{"id": "loop", "kind": "loop", "start": 1, "end": 9,
                             "value_start": 0, "value_end": 360, "loops": 4}], duration=10)
        values = [value_at(time, clock["scenes"][0]["segments"]) % 360
                  for time in select_review_times(clock, 30) if 1 < time < 9]
        self.assertEqual(len(values), 3)
        self.assertTrue(all(60 <= value <= 300 for value in values))

    def test_static_fallback_and_fractional_clocks_stay_within_available_video_frames(self):
        clock = self.clock([], duration=2)
        self.assertEqual(select_review_times(clock, 30), [0, 29 / 30, 59 / 30])
        clock = self.clock([], duration=1 / 30)
        self.assertEqual(select_review_times(clock, 30), [0])
        clock = self.clock([{"id": "spin", "kind": "orbit", "start": .01, "end": .45,
                             "value_start": 0, "value_end": 360}], duration=.45, fps=10)
        self.assertTrue(all(0 <= time <= .3 for time in select_review_times(clock, 10)))
        with self.assertRaises(ValueError):
            select_review_times(clock, 30)

    def test_static_canvas_review_samples_each_actual_label_interval(self):
        controller = compile_scene({"schema_version": "infofilm.image-animation-scene.v1",
            "canvas": {"width": 128, "height": 96}, "assets": {"background": {"path": "background.png"}},
            "controller": {"type": "static_canvas", "parameters": {}, "bindings": {}}, "incomplete_flags": []})
        actions = [{"id": f"label-{index}", "kind": "label", "start": start, "end": end,
                    "label": f"Comparison {index}"} for index, (start, end) in enumerate([
                        (1.8795, 3.959), (13.2566, 19.865), (23.184, 26.333)])]
        clock = compile_timeline({"duration_seconds": 26.433, "scenes": [{"id": "B04", "start": 0,
            "end": 26.433, "actions": actions}]}, controller, fps=30)
        original = copy.deepcopy(clock)
        times = select_review_times(clock, 30)
        self.assertEqual(len(times), 5)
        self.assertEqual(times[0], 0)
        self.assertEqual(times[-1], (clock["frames"] - 1) / 30)
        for action in actions:
            self.assertTrue(any(action["start"] < time < action["end"] for time in times), action["id"])
        self.assertTrue(all(value_at(time, clock["scenes"][0]["segments"]) == 0 for time in times))
        self.assertEqual(clock, original)

    def test_highlight_and_labeled_actions_get_bounded_interior_coverage(self):
        actions = [{"id": "highlight", "kind": "highlight", "start": 1, "end": 2},
                   {"id": "component", "kind": "component_highlight", "start": 4, "end": 5},
                   {"id": "stage", "kind": "stage_label", "start": 8, "end": 9, "label": "Observe the comparison"},
                   {"id": "note", "kind": "note", "start": 12, "end": 13, "label": "Constant radius"}]
        clock = self.clock(actions, duration=15)
        times = select_review_times(clock, 30)
        self.assertEqual(len(times), 6)
        for action in actions:
            self.assertTrue(any(action["start"] < time < action["end"] for time in times), action["id"])
        actions = [{"id": f"label-{index}", "kind": "label", "start": index, "end": index + .5,
                    "label": "Visible code label"} for index in range(20)]
        clock = self.clock(actions, duration=20)
        times = select_review_times(clock, 30)
        self.assertEqual(len(times), 7)
        self.assertEqual(len(times), len(set(times)))
        self.assertTrue(all(0 <= time <= (clock["frames"] - 1) / 30 for time in times))
        self.assertTrue(all(abs(time * 30 - round(time * 30)) < 1e-8 for time in times))
        self.assertTrue(any(0 < time < .5 for time in times))
        self.assertTrue(any(19 < time < 19.5 for time in times))
        clock = self.clock([{"id": "subframe", "kind": "label", "start": .001, "end": .009,
                             "label": "No available interior video frame"}], duration=2)
        self.assertEqual(select_review_times(clock, 30), [0, 29 / 30, 59 / 30])
        clock["scenes"][0]["actions"][0]["kind"] = "arbitrary_expression"
        with self.assertRaisesRegex(ValueError, "allowlisted actions"):
            select_review_times(clock, 30)

    def test_one_sheet_labels_times_values_and_preserves_frame_files_and_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            frames = [root / "first.png", root / "moving.png"]
            for frame, colour in zip(frames, ((20, 30, 40), (10, 100, 240))):
                Image.new("RGB", (128, 96), colour).save(frame)
            frozen = {frame: (frame.read_bytes(), frame.stat().st_mtime_ns) for frame in frames}
            samples = [{"seconds": 0, "controller_value": 0, "teaching": "Earth's rotation causes day and night."},
                       {"seconds": 13.1667, "controller_value": 90, "teaching": "Earth's rotation causes day and night."}]
            original = copy.deepcopy(samples)
            path, metadata = build_rendered_review_sheet(frames, samples, root / "review")
            self.assertEqual(metadata["panel_order"], ["frame-01", "frame-02"])
            self.assertIn("TIME 13.167s", metadata["panels"][1]["label"])
            self.assertIn("VALUE 90.000", metadata["panels"][1]["label"])
            self.assertEqual(metadata["samples"], samples)
            with Image.open(path) as sheet:
                self.assertEqual(sheet.mode, "RGB")
                for panel, colour in zip(metadata["panels"], ((20, 30, 40), (10, 100, 240))):
                    x1, y1, x2, y2 = panel["display_xyxy_px"]
                    self.assertEqual(sheet.getpixel(((x1 + x2) // 2, (y1 + y2) // 2)), colour)
            self.assertEqual(samples, original)
            self.assertEqual({frame: (frame.read_bytes(), frame.stat().st_mtime_ns) for frame in frames}, frozen)
            collision = root / "rendered-review-sheet.png"
            collision.write_bytes(frames[0].read_bytes())
            with self.assertRaisesRegex(ValueError, "overwrite"):
                build_rendered_review_sheet([collision], samples[:1], root)

    def test_rendered_sheet_rejects_mismatched_metadata_and_expression_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            frame = root / "frame.png"
            Image.new("RGB", (16, 16), "blue").save(frame)
            with self.assertRaises(ValueError):
                build_rendered_review_sheet([frame], [], root / "review")
            with self.assertRaisesRegex(ValueError, "finite number"):
                build_rendered_review_sheet([frame], [{"seconds": 0, "controller_value": "1 + 2"}], root / "review")
            self.assertFalse((root / "review").exists())


if __name__ == "__main__":
    unittest.main()
