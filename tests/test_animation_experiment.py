"""Timing, declarative compilation, and resumable local animation planning."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from infofilm.animation_experiment import (
    anchor_time, captions, compile_motion, component_errors, load_narration, phase, preview_draft, render_reusable, review_render, run,
)
from infofilm.animation_objects import register_object
from infofilm.core import Store, digest, read, write


def anchor(identity, edge="start", offset=0):
    return {"word_id": identity, "edge": edge, "offset_seconds": offset}


def narration():
    words = ["First,", "release", "both", "balls.", "They", "land", "together."]
    return {"duration_seconds": 8, "transcript": " ".join(words),
            "words": [{"id": "W" + str(index + 1), "word": word, "start": index, "end": index + 1}
                      for index, word in enumerate(words)]}


def requirement():
    return {"schema_version": "infofilm.animation-requirement.v1", "id": "gravity-test", "subject": "Gravity",
            "visual_message": "Gravity gives simultaneously released balls equal vertical motion.",
            "scene_requirement": "Compare a horizontally launched ball and a dropped ball.",
            "facts": ["Uniform gravity; air resistance ignored."], "constraints": []}


def components():
    req = requirement()
    objects = []
    for object_id in ("launched", "dropped"):
        objects.append({"id": object_id, "kind": "svg", "role": "falling ball", "physics_role": "projectile",
            "conveys": "Equal vertical acceleration", "asset_id": "geometry.ball.v1", "request_id": None,
            "required_capabilities": ["circular"]})
    for object_id, kind, role in (("ground", "rect", "landing surface"), ("label", "text", "assumption label")):
        objects.append({"id": object_id, "kind": kind, "role": role, "physics_role": "annotation" if kind == "text" else "static",
            "conveys": "Context for the comparison", "asset_id": None, "request_id": None, "required_capabilities": []})
    return {"schema_version": "infofilm.animation-components.v1", "requirement_id": req["id"], "scene_id": req["id"],
            "visual_message": req["visual_message"], "teaching_strategy": "Release together and compare vertical positions",
            "assumptions": ["Uniform gravity", "Air resistance ignored"], "objects": objects, "component_requests": []}


ASSETS = {"geometry.ball.v1": {"pivot": [.5, .5], "sha256": "a" * 64,
                               "capabilities": ["circular"], "intrinsic_dimensions": {"width": 100, "height": 100}}}


def motion():
    component_plan = components()
    objects = []
    for object_id, x, vx in (("launched", .2, 6), ("dropped", .65, 0)):
        objects.append({"id": object_id, "width": .035, "height": .035 * 1280 / 720, "z_index": 2,
            "tracks": [{"kind": "hold", "start_anchor": anchor("audio_start"), "end_anchor": anchor("W2"), "x": x, "y": .2},
                       {"kind": "projectile", "start_anchor": anchor("W2"), "end_anchor": anchor("W6", "end"),
                        "origin": [x, .2], "fraction_per_meter": [.03, .02], "gravity": 9.81, "vx": vx, "vy": 0,
                        "physical_seconds": 2}]})
    objects.extend([
        {"id": "ground", "width": .75, "height": .04, "color": "#789abc", "z_index": 0,
         "tracks": [{"kind": "hold", "start_anchor": anchor("audio_start"), "end_anchor": anchor("audio_end"), "x": .5, "y": .65}]},
        {"id": "label", "width": .8, "height": .08, "text": "Air resistance ignored", "font_size": 24, "color": "#ffffff", "z_index": 3,
         "tracks": [{"kind": "hold", "start_anchor": anchor("audio_start"), "end_anchor": anchor("audio_end"), "x": .5, "y": .72}]},
    ])
    return {"schema_version": "infofilm.animation-motion-draft.v1", "scene_id": component_plan["scene_id"],
        "visual_message": component_plan["visual_message"], "explanation": "Compare the downward motion after simultaneous release",
        "background": "#071b29", "objects": objects,
        "checkpoints": [{"id": checkpoint, "anchor": timing, "reason": reason, "object_ids": ["launched", "dropped"]}
            for checkpoint, timing, reason in (("setup", anchor("audio_start"), "Both balls start at the same height"),
                ("release", anchor("W2"), "Both are released together"),
                ("landing", anchor("W6", "end"), "Both reach the ground together"))]}


def good_review():
    return {"pass": True, "score": 95, "issues": [], "strengths": ["Meets the requirement"]}


class FakeModels:
    def __init__(self, candidates=(), reviews=()):
        self.config = {"planner_url": "http://planner.invalid/v1", "reviewer_url": "http://reviewer.invalid/v1"}
        self.candidates, self.reviews = iter(candidates), iter(reviews)
        self.calls, self.review_calls = [], []

    def call(self, role, name, prompt, **kwargs):
        self.calls.append({"role": role, "name": name, "prompt": prompt, "kwargs": kwargs})
        return copy.deepcopy(next(self.candidates))

    def call_json(self, role, name, prompt, **kwargs):
        self.review_calls.append({"role": role, "name": name, "prompt": prompt})
        return copy.deepcopy(next(self.reviews))


class AnimationExperimentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.work = Path(self.folder.name)

    def compile(self, draft):
        return compile_motion(draft, components(), ASSETS, narration(), 1280, 720)

    def store(self, name="phase"):
        store = Store(self.work / name)
        self.addCleanup(store.db.close)
        return store

    def test_mfa_anchors_use_exact_word_edges_and_bounded_offsets(self):
        audio = narration()
        self.assertEqual(anchor_time(anchor("W2"), audio), 1)
        self.assertEqual(anchor_time(anchor("W2", "end", .25), audio), 2.25)
        self.assertEqual(anchor_time(anchor("audio_start"), audio), 0)
        self.assertEqual(anchor_time(anchor("audio_end", "end"), audio), 8)
        for bad in (None, {"word_id": "W2", "edge": "middle"}, anchor("missing"), anchor("W2", offset=True),
                    anchor("W2", offset=float("nan")), anchor("W2", offset=6), anchor("audio_start", offset=-.01),
                    anchor("audio_end", offset=.01)):
            with self.subTest(anchor=bad), self.assertRaises(ValueError):
                anchor_time(bad, audio)

    def test_compilation_preserves_release_anchors_physical_clock_and_vertical_equality(self):
        compiled, errors = self.compile(motion())
        self.assertEqual(errors, [])
        objects = {obj["id"]: obj for obj in compiled["objects"]}
        first, second = objects["launched"]["tracks"][1], objects["dropped"]["tracks"][1]
        self.assertEqual((first["start"], first["end"]), (1, 6))
        self.assertEqual((second["start"], second["end"]), (1, 6))
        self.assertEqual(first["physical_seconds"], 2)
        self.assertEqual(second["vx"], 0)
        self.assertEqual(first["vx"], 6)
        for fraction in (0, .25, .5, .75, 1):
            values = []
            for track in (first, second):
                t = fraction * track["physical_seconds"]
                values.append(track["origin"][1] - track["fraction_per_meter"][1] * (track["vy"] * t - .5 * track["gravity"] * t * t))
            self.assertAlmostEqual(values[0], values[1])
        self.assertAlmostEqual(values[0], .5924)
        self.assertEqual(compiled["duration_seconds"], 8)

    def test_entire_projectile_path_and_rotated_object_bounds_are_checked(self):
        draft = motion()
        track = draft["objects"][0]["tracks"][1]
        # Both endpoints fit, but the upward arc crosses the frame at its apex.
        track.update(vx=0, vy=9.81, physical_seconds=2, fraction_per_meter=[.03, .05])
        compiled, errors = self.compile(draft)
        self.assertTrue(any("visual area" in error for error in errors), errors)
        draft = motion()
        draft["objects"][2]["tracks"][0].update(x=.1, rotation=90)
        _, errors = self.compile(draft)
        self.assertTrue(any("visual area" in error for error in errors), errors)

    def test_source_captions_and_checkpoint_times_cannot_be_replaced_by_generated_text(self):
        draft = motion()
        draft["captions"] = [{"text": "Invented narration", "start": 0, "end": 8}]
        compiled, errors = self.compile(draft)
        self.assertEqual(errors, [])
        self.assertEqual(compiled["captions"], [
            {"start": 0, "end": 4, "text": "First, release both balls."},
            {"start": 4, "end": 7, "text": "They land together."}])
        self.assertEqual([row["time"] for row in compiled["checkpoints"]], [0, 1, 6])
        long = [{"id": str(i), "word": "word" + str(i), "start": i, "end": i + 1} for i in range(20)]
        self.assertEqual([len(row["text"].split()) for row in captions(long)], [9, 9, 2])
        self.assertEqual(" ".join(row["text"] for row in captions(long)), " ".join(w["word"] for w in long))

    def test_malformed_component_drafts_return_errors_without_crashing(self):
        cases = [None, [], {}, {**components(), "objects": None}, {**components(), "objects": [None]}]
        malformed = components(); malformed["objects"][0]["physics_role"] = []
        cases.append(malformed)
        for case in cases:
            with self.subTest(candidate=case):
                errors = component_errors(case, requirement())
                self.assertIsInstance(errors, list)
                self.assertTrue(errors)

    def test_malformed_motion_drafts_return_errors_without_crashing(self):
        cases = [None, [], {}, {**motion(), "objects": None}, {**motion(), "objects": [None]}]
        for key, value in (("id", []), ("tracks", None), ("tracks", {}), ("tracks", [None])):
            draft = motion(); draft["objects"][0][key] = value; cases.append(draft)
        for key, value in (("kind", []), ("start_anchor", None), ("origin", None), ("gravity", True)):
            draft = motion(); draft["objects"][0]["tracks"][1][key] = value; cases.append(draft)
        for value in (None, [None, None, None], [{"id": [], "anchor": anchor("W2")}]*3):
            draft = motion(); draft["checkpoints"] = value; cases.append(draft)
        for draft in cases:
            with self.subTest(candidate=draft):
                _, errors = self.compile(draft)
                self.assertIsInstance(errors, list)
                self.assertTrue(errors)

    def test_duplicate_instances_missing_projectile_and_overlapping_tracks_are_rejected(self):
        draft = motion(); draft["objects"][1]["id"] = "launched"
        self.assertTrue(self.compile(draft)[1])
        draft = motion(); draft["objects"][0]["tracks"][1]["start_anchor"] = anchor("audio_start")
        self.assertTrue(any("overlap" in error for error in self.compile(draft)[1]))
        draft = motion(); draft["objects"][0]["tracks"] = [{"kind": "hold", "start_anchor": anchor("audio_start"),
            "end_anchor": anchor("audio_end"), "x": .2, "y": .2}]
        self.assertTrue(any("projectile" in error for error in self.compile(draft)[1]))

    def test_phase_cache_reuses_accepted_plan_without_model_calls_and_revalidates_it(self):
        store = self.store()
        model = FakeModels([components()], [good_review()])
        validator = lambda candidate: component_errors(candidate, requirement())
        args = (store, "components", "Choose objects", {"requirement": requirement()}, validator, 3000)
        selected = phase(model, *args)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(model.review_calls), 1)
        fresh = FakeModels()
        self.assertEqual(phase(fresh, *args), selected)
        self.assertEqual(fresh.calls, [])
        self.assertEqual(fresh.review_calls, [])
        selected_file = next((store.root / "planning" / "components").glob("*/selected.json"))
        changed = read(selected_file); changed["scene_id"] = "different"; write(selected_file, changed)
        with self.assertRaisesRegex(ValueError, "no longer validates"):
            phase(fresh, *args)

    def test_phase_repairs_invalid_draft_before_reviewing_it(self):
        store = self.store()
        model = FakeModels([{}, components()], [good_review()])
        validator = lambda candidate: component_errors(candidate, requirement())
        phase(model, store, "components", "Choose objects", {"requirement": requirement()}, validator, 3000, max_attempts=2)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(model.review_calls), 1)
        self.assertIn("Repair the candidate", model.calls[-1]["prompt"])

    def test_invalid_motion_review_receives_code_errors_but_cannot_override_them(self):
        store = self.store()
        invalid = motion()
        invalid["objects"][0]["tracks"][1]["origin"] = [.95, .2]
        expected_errors = self.compile(invalid)[1]
        self.assertTrue(any("visual area" in error for error in expected_errors))
        repaired = motion()
        # The reviewer incorrectly passes the first image plan. Authoritative
        # geometry errors must still trigger repair rather than freeze it.
        model = FakeModels([invalid, repaired], [good_review(), good_review()])
        selected = phase(model, store, "motion-landscape", "Plan motion", {"requirement": requirement()},
            lambda candidate: self.compile(candidate)[1], 3000, max_attempts=2, review_invalid=True)
        self.assertEqual(selected, repaired)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(model.review_calls), 2)
        supplied_errors = json.loads(model.review_calls[0]["prompt"].split("\nCODE_ERRORS:\n", 1)[1])
        self.assertEqual(supplied_errors, expected_errors)
        self.assertIn("supplied code errors are authoritative", model.review_calls[0]["prompt"])
        self.assertIn(expected_errors[0], model.calls[1]["prompt"])
        evaluation = read(next((store.root / "planning").glob("**/evaluation.json")))
        self.assertFalse(evaluation["attempts"][0]["accepted"])
        self.assertEqual(evaluation["attempts"][0]["review"]["score"], 95)
        self.assertTrue(evaluation["attempts"][1]["accepted"])

    def test_malformed_reviewer_verdicts_are_format_repaired_without_replanning_components(self):
        invalid = [None, {**good_review(), "score": 200}, {**good_review(), "issues": [None]},
                   {**good_review(), "issues": "none"}, {**good_review(), "score": True}]
        for index, verdict in enumerate(invalid):
            with self.subTest(verdict=verdict):
                store = self.store("review-" + str(index))
                model = FakeModels([components(), components()], [verdict, good_review()])
                validator = lambda candidate: component_errors(candidate, requirement())
                phase(model, store, "components", "Choose objects", {"requirement": requirement()}, validator, 3000, max_attempts=2)
                self.assertEqual(len(model.calls), 1)
                self.assertEqual(len(model.review_calls), 2)
                self.assertIn("exact review schema", model.review_calls[-1]["prompt"])

    def test_repeated_invalid_reviews_exhaust_bounded_budget_without_freezing_plan(self):
        store = self.store()
        invalid = {**good_review(), "issues": [None]}
        model = FakeModels([components()], [invalid, invalid])
        validator = lambda candidate: component_errors(candidate, requirement())
        with self.assertRaisesRegex(RuntimeError, "bounded attempts"):
            phase(model, store, "components", "Choose objects", {"requirement": requirement()}, validator, 3000, max_attempts=1)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(model.review_calls), 2)
        self.assertFalse(any((store.root / "planning").glob("**/selected.json")))
        evaluation = read(next((store.root / "planning").glob("**/evaluation.json")))
        self.assertFalse(evaluation["accepted"])
        self.assertTrue(evaluation["attempts"][0]["code_errors"])

    def test_semantic_review_failure_triggers_plan_repair(self):
        store = self.store()
        rejected = {"pass": False, "score": 65, "issues": [{"severity": "major", "field": "teaching_strategy",
            "problem": "Comparison releases are not explained", "correction": "Explain simultaneous release"}], "strengths": []}
        model = FakeModels([components(), components()], [rejected, good_review()])
        validator = lambda candidate: component_errors(candidate, requirement())
        phase(model, store, "components", "Choose objects", {"requirement": requirement()}, validator, 3000, max_attempts=2)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(model.review_calls), 2)
        self.assertIn("simultaneous release", model.calls[-1]["prompt"])

    def test_circular_assets_and_accepted_component_identities_are_preserved(self):
        draft = motion(); draft["objects"][0]["height"] = draft["objects"][0]["width"]
        self.assertTrue(any("circular SVG" in error for error in self.compile(draft)[1]))
        draft = motion(); draft["objects"][0]["asset_id"] = "unapproved.asset.v1"
        self.assertTrue(any("accepted asset" in error for error in self.compile(draft)[1]))
        draft = motion(); draft["objects"][0]["kind"] = "line"
        self.assertTrue(any("component kind" in error for error in self.compile(draft)[1]))

    def test_render_cache_requires_same_sources_props_video_and_checkpoint_files(self):
        project = self.work / "project"
        source_names = ["src/animation-experiment/index.tsx", "src/animation-experiment/AnimationExperiment.tsx",
                        "src/animation-experiment/motion.ts", "src/animation-experiment/assets.ts",
                        "src/animation-experiment/chapters.ts", "src/animation-experiment/LayoutMeasurement.tsx", "animation-experiment.cjs"]
        for name in source_names:
            path = project / "renderer" / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("source " + name)
        folder = self.work / "render"; folder.mkdir()
        props = write(folder / "props.json", {"scene": "original"})
        original_props = props.read_bytes()
        (folder / "animation.mp4").write_bytes(b"rendered video fixture")
        checkpoint = folder / "checkpoint.png"; checkpoint.write_bytes(b"frame fixture")
        sources = {name: hashlib.sha256((project / "renderer" / name).read_bytes()).hexdigest() for name in source_names}
        result = {"complete": True, "props_sha256": hashlib.sha256(original_props).hexdigest(), "source_sha256": sources,
                  "video": {"file": "animation.mp4"}, "checkpoints": [{"file": "checkpoint.png"}]}
        write(folder / "render-results.json", result)
        with patch("infofilm.animation_experiment.ROOT", project):
            self.assertTrue(render_reusable(folder, props))
            changed = project / "renderer" / source_names[0]
            before = changed.read_bytes(); changed.write_bytes(b"changed renderer")
            self.assertFalse(render_reusable(folder, props))
            changed.write_bytes(before)
            props.write_text("changed props")
            self.assertFalse(render_reusable(folder, props))
            props.write_bytes(original_props)
            checkpoint.unlink()
            self.assertFalse(render_reusable(folder, props))
            checkpoint.write_bytes(b"frame fixture")
            (folder / "animation.mp4").unlink()
            self.assertFalse(render_reusable(folder, props))

    def test_frame_review_receives_actual_checkpoint_order_and_hash_sensitive_cache(self):
        store = self.store()
        folder = store.root / "renders" / "landscape"; folder.mkdir(parents=True)
        compiled, errors = self.compile(motion()); self.assertEqual(errors, [])
        write(folder / "compiled-plan.json", compiled)
        checkpoints = []
        for index, checkpoint in enumerate(compiled["checkpoints"]):
            filename = "frame-" + str(index) + ".png"
            (folder / filename).write_bytes(("frame bytes " + str(index)).encode())
            checkpoints.append({**checkpoint, "file": filename})
        write(folder / "render-results.json", {"checkpoints": checkpoints})
        models = FakeModels([{**good_review(), "limitations": ["No audio listening performed"]}])
        result = review_render(models, store, folder, requirement(), narration())
        self.assertTrue(result["accepted"])
        self.assertEqual(result["human_review"], "pending")
        self.assertEqual(models.calls[0]["role"], "reviewer")
        self.assertEqual(models.calls[0]["kwargs"]["reference_image_paths"], [folder / row["file"] for row in checkpoints])
        self.assertIn("not an audio listening review", models.calls[0]["prompt"])
        cached = FakeModels()
        self.assertEqual(review_render(cached, store, folder, requirement(), narration()), result)
        self.assertEqual(cached.calls, [])
        (folder / checkpoints[0]["file"]).write_bytes(b"new checkpoint bytes")
        rerun = FakeModels([good_review()])
        changed = review_render(rerun, store, folder, requirement(), narration())
        self.assertNotEqual(changed["input_sha256"], result["input_sha256"])
        self.assertEqual(len(rerun.calls), 1)

    def frozen_narration(self):
        audio = self.work / "narration.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(16000)
            handle.writeframes(b"\0\0" * (8 * 16000))
        value = narration()
        audio_hash = digest(audio.read_bytes())
        timing = write(self.work / "timing.json", {"audio_sha256": audio_hash, "words": value["words"]})
        return write(self.work / "narration-input.json", {"schema_version": "infofilm.animation-narration.v1", **value,
            "audio": str(audio), "audio_sha256": audio_hash, "timing": str(timing), "timing_sha256": digest(timing.read_bytes()),
            "transcript_sha256": digest(value["transcript"])})

    def test_narration_inputs_remain_hash_bound_to_audio_map_and_transcript(self):
        input_path = self.frozen_narration()
        self.assertEqual(load_narration(input_path)["words"], narration()["words"])
        bundle = read(input_path); bundle["transcript"] += " Extra."; write(input_path, bundle)
        with self.assertRaisesRegex(ValueError, "transcript hash changed"):
            load_narration(input_path)
        input_path = self.frozen_narration()
        bundle = read(input_path); Path(bundle["audio"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "audio changed"):
            load_narration(input_path)
        input_path = self.frozen_narration()
        bundle = read(input_path); Path(bundle["timing"]).write_text("{}")
        with self.assertRaisesRegex(ValueError, "timing changed"):
            load_narration(input_path)

    def test_missing_component_pause_replenishment_and_resume_preserves_original_plan(self):
        library = self.work / "library"; library.mkdir()
        write(library / "registry.json", {"schema_version": "infofilm.animation-library.v1", "revision": "empty", "objects": {}})
        req_path = write(self.work / "requirement.json", requirement())
        narration_input = self.frozen_narration()
        cfg = FakeModels().config
        config_path = write(self.work / "config.json", cfg)
        candidate = components(); candidate["objects"] = candidate["objects"][:1]
        candidate["objects"][0].update(asset_id=None, request_id="R01")
        candidate["component_requests"] = [{"id": "R01", "requested_asset_id": "geometry.requested-ball.v1",
            "description": "A circular ball", "required_capabilities": ["circular"], "svg_requirements": {"pivot": [.5, .5]}}]
        args = argparse.Namespace(output=self.work / "experiment", requirement=req_path, narration_input=narration_input,
            config=config_path, library=library, orientation="landscape", until="components", resume=False, bindings=None, max_attempts=2)
        model = FakeModels([candidate], [good_review()])
        with patch("infofilm.animation_experiment.Models", return_value=model):
            self.assertEqual(run(args), 3)
        self.assertEqual(read(args.output / "state.json")["state"], "waiting_for_components")
        original = (args.output / "components-plan.json").read_bytes()
        requests = read(args.output / "object-requests.json")
        svg = self.work / "created-ball.svg"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><circle cx="10" cy="10" r="9"/></svg>')
        register_object(requests, "R01", svg, {"pivot": [.5, .5], "capabilities": ["circular"], "provenance": "Unit test created object"},
            {"pass": True, "asset_sha256": hashlib.sha256(svg.read_bytes()).hexdigest(), "assessed_capabilities": ["circular"]}, library=library)
        args.resume = True
        resumed = FakeModels()
        with patch("infofilm.animation_experiment.Models", return_value=resumed):
            self.assertEqual(run(args), 0)
        self.assertEqual(resumed.calls, [])
        self.assertEqual(resumed.review_calls, [])
        self.assertEqual((args.output / "components-plan.json").read_bytes(), original)
        self.assertEqual(read(args.output / "state.json")["state"], "components_ready")
        resolution = read(args.output / "object-resolution.json")
        self.assertTrue(resolution["ready"])
        self.assertEqual(set(resolution["assets"]), {"geometry.requested-ball.v1"})

    def test_swapped_model_routes_are_experiment_only_and_frozen_for_resume(self):
        library = self.work / "library"; (library / "objects").mkdir(parents=True)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><circle cx="10" cy="10" r="9"/></svg>'
        (library / "objects" / "ball.svg").write_bytes(svg)
        write(library / "registry.json", {"schema_version": "infofilm.animation-library.v1", "revision": "test",
            "objects": {"geometry.ball.v1": {"file": "objects/ball.svg", "sha256": hashlib.sha256(svg).hexdigest(),
                "pivot": [.5, .5], "capabilities": ["circular"], "provenance": "Test object"}}})
        req_path = write(self.work / "requirement.json", requirement())
        narration_input = self.frozen_narration()
        cfg = FakeModels().config
        config_path = write(self.work / "config.json", cfg)
        original_config = config_path.read_bytes()
        args = argparse.Namespace(output=self.work / "swapped-experiment", requirement=req_path, narration_input=narration_input,
            config=config_path, library=library, orientation="landscape", until="components", resume=False, bindings=None,
            max_attempts=2, swap_models=True)
        instances = []

        def model_factory(config, store):
            model = FakeModels([components()], [good_review()])
            model.config = copy.deepcopy(config)
            instances.append(model)
            return model

        with patch("infofilm.animation_experiment.Models", side_effect=model_factory):
            self.assertEqual(run(args), 0)
        expected_routes = {"planner_url": cfg["reviewer_url"], "reviewer_url": cfg["planner_url"]}
        self.assertEqual({key: instances[0].config[key] for key in expected_routes}, expected_routes)
        self.assertEqual(instances[0].calls[0]["role"], "planner")
        self.assertEqual(instances[0].review_calls[0]["role"], "reviewer")
        self.assertEqual(read(args.output / "inputs.json")["models"], expected_routes)
        self.assertEqual(config_path.read_bytes(), original_config)
        frozen_inputs = (args.output / "inputs.json").read_bytes()
        args.resume, args.swap_models = True, False
        with patch("infofilm.animation_experiment.Models") as model_class:
            with self.assertRaisesRegex(ValueError, "model routes changed"):
                run(args)
            model_class.assert_not_called()
        self.assertEqual((args.output / "inputs.json").read_bytes(), frozen_inputs)
        self.assertEqual(config_path.read_bytes(), original_config)

    def preview_fixture(self, output):
        candidate = write(output / "planning" / "motion-landscape" / "trial" / "candidate-4.json", motion())
        result = write(output / "requests" / "animation-motion-landscape-fixture.result.json", motion())
        request = write(result.with_name(result.name.replace(".result.json", ".request.json")),
            {"role": "planner", "url": FakeModels().config["planner_url"], "model": "saved-test-model"})
        return candidate, result, request

    def test_unapproved_preview_is_exact_model_response_and_does_not_select_motion(self):
        store = self.store()
        candidate, result, request = self.preview_fixture(store.root)
        before = candidate.read_bytes()
        models = FakeModels()
        value = preview_draft(candidate, store, models, "landscape")
        self.assertEqual(value, read(result))
        self.assertEqual(candidate.read_bytes(), before)
        provenance = read(store.root / "preview-source.json")
        self.assertIs(provenance["approved"], False)
        self.assertEqual(provenance["candidate_sha256"], digest(read(result)))
        self.assertEqual(provenance["model_request"], str(request))
        self.assertIn("approval remains pending", provenance["notice"])
        self.assertFalse(any((store.root / "planning" / "motion-landscape").glob("**/selected.json")))
        self.assertEqual(models.calls, [])
        self.assertEqual(models.review_calls, [])

    def test_preview_rejects_edited_candidate_or_wrong_model_provenance(self):
        for name in ("edited", "reviewer", "route"):
            with self.subTest(reason=name):
                store = self.store(name)
                candidate, _, request = self.preview_fixture(store.root)
                if name == "edited":
                    edited = read(candidate); edited["explanation"] = "An operator changed this draft"; write(candidate, edited)
                else:
                    metadata = read(request)
                    metadata["role" if name == "reviewer" else "url"] = "reviewer" if name == "reviewer" else "http://other-planner.invalid/v1"
                    write(request, metadata)
                with self.assertRaisesRegex(ValueError, "saved planner response"):
                    preview_draft(candidate, store, FakeModels(), "landscape")
                self.assertFalse((store.root / "preview-source.json").exists())
                self.assertFalse(any((store.root / "planning").glob("**/selected.json")))

    def test_preview_rejects_other_orientation_and_paths_outside_experiment(self):
        store = self.store()
        self.preview_fixture(store.root)
        other_orientation = write(store.root / "planning" / "motion-portrait" / "trial" / "candidate-4.json", motion())
        outside = write(self.work / "outside.json", motion())
        for path in (other_orientation, outside):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "experiment and orientation"):
                preview_draft(path, store, FakeModels(), "landscape")
        # resolve() must prevent a symlink inside planning from bypassing scope.
        symlink = store.root / "planning" / "motion-landscape" / "linked.json"
        symlink.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "experiment and orientation"):
            preview_draft(symlink, store, FakeModels(), "landscape")
        self.assertFalse((store.root / "preview-source.json").exists())

    def test_preview_run_stays_unapproved_even_when_component_phase_is_accepted(self):
        library = self.work / "library"; (library / "objects").mkdir(parents=True)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><circle cx="10" cy="10" r="9"/></svg>'
        (library / "objects" / "ball.svg").write_bytes(svg)
        write(library / "registry.json", {"schema_version": "infofilm.animation-library.v1", "revision": "test",
            "objects": {"geometry.ball.v1": {"file": "objects/ball.svg", "sha256": hashlib.sha256(svg).hexdigest(),
                "pivot": [.5, .5], "capabilities": ["circular"], "provenance": "Test object"}}})
        output = self.work / "preview-experiment"
        candidate, _, _ = self.preview_fixture(output)
        config_path = write(self.work / "config.json", FakeModels().config)
        args = argparse.Namespace(output=output, requirement=write(self.work / "requirement.json", requirement()),
            narration_input=self.frozen_narration(), config=config_path, library=library, orientation="landscape",
            until="render", resume=False, bindings=None, max_attempts=2, preview_candidate=candidate)
        models = FakeModels([components()], [good_review()])
        with patch("infofilm.animation_experiment.Models", return_value=models), \
                patch("infofilm.animation_experiment.render_reusable", return_value=True), \
                patch("infofilm.animation_experiment.publish") as publish:
            self.assertEqual(run(args), 2)
        self.assertEqual(len(models.calls), 1)  # Components only; preview does not invoke or accept motion phase.
        self.assertEqual(len(models.review_calls), 1)
        self.assertEqual(read(output / "state.json")["state"], "preview_needs_review")
        self.assertIs(read(output / "preview-source.json")["approved"], False)
        self.assertFalse(any((output / "planning" / "motion-landscape").glob("**/selected.json")))
        self.assertEqual(read(output / "motion-landscape-plan.json"), read(candidate))
        publish.assert_called_once()
        self.assertIs(publish.call_args.kwargs["preview"], True)


if __name__ == "__main__":
    unittest.main()
