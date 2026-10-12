"""Failure boundaries, immutable artifacts and CLI resume ownership."""
import copy
import fcntl
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from infofilm.core import digest, read, write
from infofilm.image_animation_contracts import GROUNDING_SCHEMA, grounding_errors
from infofilm.image_animation_models import OutputBudgetError, load_registry
from infofilm.image_animation_workflow import Workflow, Waiting, file_pin, main, parser, resolve_args
from infofilm.llm import ModelOutputError
from test_image_animation_contracts import fixture as image_grounding


ACCEPT = {"pass": True, "score": 94, "issues": [], "strengths": ["Correct teaching meaning."]}
REJECT = {"pass": False, "score": 70, "issues": [{"severity": "major", "field": "facts[0]",
    "problem": "The explanation reverses the source relationship.", "correction": "Keep the supplied relationship."}],
    "strengths": []}


class ResumeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "run"
        self.facade = Mock()
        self.facade.fingerprint = "stable-routed-teaching"
        self.models = Mock()
        self.models.for_activity.return_value = self.facade
        self.models.describe_routes.return_value = {
            "teaching": {"provider": "local"}, "review": {"provider": "local-reviewer"}}

    def args(self, *extra):
        p = parser()
        return resolve_args(p.parse_args(["--subject", "Circular motion", "--no-subject-graph",
            "--output", str(self.output), *extra]), p)

    def workflow(self, *extra):
        workflow = Workflow(self.args(*extra), models_factory=lambda *a, **kw: self.models)
        self.addCleanup(workflow.store.db.close)
        return workflow

    def plan(self, workflow):
        return workflow.json_plan(workflow.root, "teaching", "teaching-review-trial", "Explain this relationship.",
            {"source_fact": "The radius remains constant."}, lambda candidate: [])

    def grounding_plan(self, workflow, canvas):
        return workflow.json_plan(workflow.root, "grounding", "grounding-schema-trial", "Ground the supplied image.",
            {"output_schema": GROUNDING_SCHEMA}, lambda candidate: grounding_errors(candidate, canvas, (1000, 700)))

    def test_grounded_scene_and_layer_review_preserve_accepted_teaching_context(self):
        workflow = self.workflow()
        canvas, ground = image_grounding()
        canvas["visual_message"] = "Earth's rotation causes day and night."
        canvas["limitations"] = ["A surface marker is a spin proxy; the globe and terminator stay fixed."]
        ground["scene"]["canvas"] = {"width": workflow.args.canvas_width, "height": workflow.args.canvas_height}
        ground["scene"]["provenance"] = {"source": "Accepted source-pixel geometry"}
        ground["provenance"] = {"repair": "Restore the measured marker position"}
        original = copy.deepcopy(ground)
        objective = {"id": "rotation-day-night", "visual_message": canvas["visual_message"]}
        directory = workflow.root / "canvases" / canvas["id"]
        write(directory / "grounding-import.json", ground)
        image = directory / "canvas.png"
        image.write_bytes(b"Declared image fixture; raster and model calls are mocked")
        manifest = {"assets": {identity: {"file": str(image), "sha256": digest(image.read_bytes()),
            "landmarks": ground["scene"]["assets"].get(identity, {}).get("landmarks", {})}
            for identity in ("canvas", "background", "ball")}}
        sheet = directory / "labeled-review-sheet.png"
        visualization = {"panel_order": ["source_canvas", "clean_background", "isolated:ball", "assembled_initial_state"],
                         "registration": {"coordinate_system": "source pixels; top-left origin"}}
        with patch.object(workflow, "assets", return_value=manifest), \
             patch.object(workflow, "review_images") as review, \
             patch("infofilm.image_animation_review.build_layer_review_sheet", return_value=(sheet, visualization)) as sheet_builder, \
             patch("httpx.Client", side_effect=AssertionError("No live model or service calls")):
            scene = workflow.grounded_layers(directory, canvas, image, objective)
        self.assertEqual(scene["provenance"], {"source": "Accepted source-pixel geometry",
            "canvas_id": canvas["id"], "visual_message": canvas["visual_message"],
            "limitations": canvas["limitations"], "grounding_revision": ground["provenance"]})
        self.assertEqual(ground, original)
        review.assert_called_once()
        context = review.call_args.args[2]
        self.assertEqual(context["canvas_plan"], canvas)
        self.assertEqual(context["learning_objective"], objective)
        self.assertEqual(context["grounding"], original)
        self.assertEqual(context["visualization"], visualization)
        self.assertEqual(context["image_order"], ["labeled layer contact sheet; transparent pixels composited for inspection"])
        self.assertEqual(review.call_args.args[3], [sheet])
        sheet_builder.assert_called_once()
        self.assertEqual(sheet_builder.call_args.args[:3], (scene, directory / "scene.json", image))

    def test_rendered_frame_review_receives_nonempty_teaching_context_and_mechanical_states(self):
        workflow = self.workflow("--manim-python", sys.executable)
        _, ground = image_grounding()
        scene = ground["scene"]
        timing = {"duration_seconds": 22.6, "scenes": [{"id": "B01", "title": "Earth's rotation causes day and night.",
            "start": 0, "end": 22.6, "actions": [{"id": "spin", "kind": "orbit", "start": 11.8167,
            "end": 17.2367, "value_start": 0, "value_end": 360}]}]}
        teaching_context = {"canvas_id": "K02", "visual_message": timing["scenes"][0]["title"],
            "limitations": ["A surface marker is a spin proxy; the globe and terminator stay fixed."],
            "grounding_revision": {"repair": "Restore the measured marker position"}}
        directory = workflow.root / "canvases" / "K02"
        target = directory / "render" / "landscape"
        write(target / "render-results.json", {"status": "passed"})
        for index in range(5):
            (target / f"review-frame-{index}.png").write_bytes(b"Declared rendered frame fixture")
        sheet = target / "frame-review-visualization/rendered-review-sheet.png"
        visualization = {"panel_order": [f"frame-{index + 1:02d}" for index in range(5)]}
        for provenance in (teaching_context, {}):
            scene["provenance"] = provenance
            with self.subTest(provenance=provenance), \
                 patch("infofilm.image_animation_workflow.subprocess.run"), \
                 patch.object(workflow, "review_images") as review, \
                 patch("infofilm.image_animation_review.build_rendered_review_sheet", return_value=(sheet, visualization)) as sheet_builder, \
                 patch("httpx.Client", side_effect=AssertionError("No live model or service calls")):
                workflow.render_canvas(directory, scene, timing, ["landscape"])
                review.assert_called_once()
                context = review.call_args.args[2]
                if provenance:
                    self.assertEqual(context["teaching_context"], teaching_context)
                else:
                    self.assertNotIn("teaching_context", context)
                self.assertEqual(context["controller"]["type"], "orbit_path")
                self.assertEqual(len(context["samples"]), 5)
                motion_samples = [sample for sample in context["samples"] if 11.8167 < sample["seconds"] < 17.2367]
                self.assertEqual(len(motion_samples), 3)
                self.assertEqual(len({sample["controller_value"] for sample in motion_samples}), 3)
                self.assertTrue(all(0 < sample["controller_value"] < 360 for sample in motion_samples))
                self.assertEqual(context["samples"][0]["controller_value"], 0)
                self.assertEqual(context["samples"][-1]["controller_value"], 360)
                self.assertEqual(context["samples"][1]["teaching"], timing["scenes"][0]["title"])
                self.assertIn("orbiter", context["samples"][1]["expected_state"]["positions_px"])
                self.assertEqual(context["visualization"], visualization)
                self.assertEqual(review.call_args.args[3], [sheet])
                sheet_builder.assert_called_once_with([target / f"review-frame-{i}.png" for i in range(5)],
                    context["samples"], target / "frame-review-visualization")

    def test_nested_scene_schema_copy_repair_preserves_geometry_and_still_requires_review(self):
        canvas, accepted = image_grounding()
        candidate = copy.deepcopy(accepted)
        candidate["scene"]["schema_version"] = "infofilm.animation-scene.v1"
        self.facade.call_json.side_effect = [candidate, ACCEPT]
        self.facade.call.return_value = {"patches": [{"op": "copy", "path": "/scene/schema_version",
            "from": "/frozen_values/F001"}], "prose_checks": [], "explanation": "Copy the trusted scene schema ID."}
        workflow = self.workflow()
        result = self.grounding_plan(workflow, canvas)
        self.assertEqual(result, accepted)
        self.assertEqual(candidate["scene"]["schema_version"], "infofilm.animation-scene.v1")
        self.assertEqual(self.facade.call.call_count, 1)
        self.assertEqual([call.args[0] for call in self.facade.call_json.call_args_list], ["planner", "reviewer"])
        repair_input = read(next(workflow.root.rglob("local-schema-repair/*/input.json")))
        self.assertEqual(repair_input["failed_candidate"], candidate)
        self.assertEqual(repair_input["allowed_copies"], [
            {"path": "/scene/schema_version", "from": "/frozen_values/F001"}])
        self.assertEqual(read(next(workflow.root.rglob("selected.json"))), accepted)

    def test_scene_schema_copy_repair_cannot_override_semantic_rejection(self):
        canvas, accepted = image_grounding()
        candidate = copy.deepcopy(accepted)
        candidate["scene"]["schema_version"] = "infofilm.animation-scene.v1"
        self.facade.call_json.side_effect = [candidate, REJECT, accepted, ACCEPT]
        self.facade.call.return_value = {"patches": [{"op": "copy", "path": "/scene/schema_version",
            "from": "/frozen_values/F001"}], "prose_checks": [], "explanation": "Copy the trusted scene schema ID."}
        workflow = self.workflow()
        self.assertEqual(self.grounding_plan(workflow, canvas), accepted)
        self.assertEqual(self.facade.call.call_count, 1)
        self.assertEqual(self.facade.call_json.call_count, 4)
        feedback = self.facade.call_json.call_args_list[2].args[2]
        self.assertIn(REJECT["issues"][0]["problem"], feedback)
        evaluation = read(next(workflow.root.glob("planning/grounding-schema-trial/*/evaluation.json")))
        self.assertEqual(len(evaluation["history"]), 2)
        self.assertIs(evaluation["history"][0]["review"]["pass"], False)

    def test_schema_copy_repair_is_skipped_for_valid_or_unrepairable_geometry(self):
        canvas, accepted = image_grounding()
        workflow = self.workflow()
        self.facade.call.side_effect = AssertionError("No schema copy should be requested")
        self.facade.call_json.side_effect = [accepted, ACCEPT]
        self.assertEqual(self.grounding_plan(workflow, canvas), accepted)
        self.facade.call.assert_not_called()
        candidate = copy.deepcopy(accepted)
        candidate["scene"]["controller"]["parameters"]["radius_px"] = -200
        self.facade.call_json.side_effect = [candidate, accepted, ACCEPT]
        self.assertEqual(self.grounding_plan(workflow, canvas), accepted)
        self.facade.call.assert_not_called()
        self.assertEqual(list(workflow.root.rglob("local-schema-repair")), [])

    def test_semantic_rejection_is_repaired_without_review_metadata_in_plan(self):
        initial = {"explanation": "A reversed relationship."}
        accepted = {"explanation": "The radius remains constant."}
        self.facade.call_json.side_effect = [initial, REJECT, accepted, ACCEPT]
        workflow = self.workflow()
        result = self.plan(workflow)
        self.assertEqual(result, accepted)
        self.assertEqual(self.facade.call_json.call_count, 4)
        repair = self.facade.call_json.call_args_list[2].args[2]
        self.assertIn(REJECT["issues"][0]["problem"], repair)
        self.assertIn(initial["explanation"], repair)
        selected = next((workflow.root / "planning").rglob("selected.json"))
        self.assertEqual(read(selected), accepted)
        self.assertNotIn("review", result)
        self.assertNotIn("history", result)
        self.assertNotIn("provider", result)
        evaluation = read(selected.parent / "evaluation.json")
        self.assertTrue(evaluation["accepted"])
        self.assertEqual(len(evaluation["history"]), 2)

    def test_repeated_semantic_failure_preserves_candidates_and_pauses(self):
        self.facade.call_json.side_effect = [{"candidate": 1}, REJECT, {"candidate": 2}, REJECT]
        workflow = self.workflow()
        with self.assertRaises(Waiting) as caught:
            self.plan(workflow)
        self.assertEqual(caught.exception.state, "needs_review")
        evaluation = read(caught.exception.details["evaluation"])
        self.assertFalse(evaluation["accepted"])
        self.assertEqual(len(evaluation["history"]), 2)
        self.assertEqual(len(list((workflow.root / "planning").rglob("candidate-*.json"))), 2)
        self.assertEqual(list((workflow.root / "planning").rglob("selected.json")), [])
        self.assertEqual(self.facade.call_json.call_count, 4)

    def test_reviewer_token_ceiling_pauses_before_redrafting(self):
        self.facade.call_json.side_effect = [{"candidate": "complete"}, OutputBudgetError(2200)]
        workflow = self.workflow()
        with self.assertRaises(Waiting) as caught:
            self.plan(workflow)
        self.assertEqual(caught.exception.state, "needs_review")
        self.assertIn("reviewer exceeded", str(caught.exception))
        self.assertEqual(self.facade.call_json.call_count, 2)
        self.assertEqual(list((workflow.root / "planning").rglob("selected.json")), [])
        self.assertFalse(read(caught.exception.details["evaluation"])["accepted"])

    def test_malformed_reviewer_output_enters_bounded_repair_feedback(self):
        candidate = {"candidate": "Keep these correct facts."}
        self.facade.call_json.side_effect = [candidate, ModelOutputError("Invalid review JSON"), candidate, ACCEPT]
        workflow = self.workflow()
        self.assertEqual(self.plan(workflow), candidate)
        repair = self.facade.call_json.call_args_list[2].args[2]
        self.assertIn("Reviewer output: Invalid review JSON", repair)
        self.assertIn("Keep these correct facts.", repair)
        self.assertEqual(self.facade.call_json.call_count, 4)

    def test_major_issue_overrides_a_contradictory_passing_verdict(self):
        verdict = {**copy.deepcopy(REJECT), "pass": True, "score": 95}
        self.facade.call_json.side_effect = [{"candidate": 1}, verdict, {"candidate": 2}, verdict]
        workflow = self.workflow()
        with self.assertRaises(Waiting):
            self.plan(workflow)
        self.assertEqual(list((workflow.root / "planning").rglob("selected.json")), [])

    def test_checkpoint_pins_are_sidecar_metadata_and_detect_changed_files(self):
        workflow = self.workflow()
        artifact = self.root / "verified-audio.wav"
        artifact.write_bytes(b"verified audio bytes")
        value = {"spoken_text": "The radius remains constant.", "_file_pins": [file_pin(artifact)]}
        work = Mock(return_value=value)
        result = workflow.checkpoint(workflow.root, "verified-audio", {"script": "fixed"}, work)
        self.assertEqual(result, {"spoken_text": "The radius remains constant."})
        checkpoint = read(workflow.root / "checkpoints/verified-audio.json")
        self.assertEqual(checkpoint["files"], [file_pin(artifact)])
        self.assertNotIn("_file_pins", read(workflow.root / "verified-audio.json"))
        self.assertNotIn("routes", result)
        artifact.write_bytes(b"changed audio bytes")
        with self.assertRaisesRegex(ValueError, "output changed"):
            workflow.checkpoint(workflow.root, "verified-audio", {"script": "fixed"}, work)
        self.assertEqual(work.call_count, 1)

    def test_unfinished_checkpoint_can_retry_without_an_accepted_result(self):
        workflow = self.workflow()
        work = Mock(side_effect=[Waiting("waiting_execution", "Fill the asset cache."), {"asset": "ready"}])
        with self.assertRaises(Waiting):
            workflow.checkpoint(workflow.root, "asset-stage", {"request": "frozen"}, work)
        self.assertEqual(read(workflow.root / "checkpoints/asset-stage.json")["state"], "running")
        self.assertEqual(workflow.checkpoint(workflow.root, "asset-stage", {"request": "frozen"}, work), {"asset": "ready"})
        self.assertEqual(work.call_count, 2)

    def test_live_run_lock_rejects_second_process_before_model_or_store_creation(self):
        self.output.mkdir()
        with (self.output / ".workflow.lock").open("a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("infofilm.image_animation_workflow.Workflow") as factory, patch("sys.stderr", new=io.StringIO()), \
                 self.assertRaises(SystemExit) as caught:
                main(["--subject", "Circular motion", "--output", str(self.output)])
            factory.assert_not_called()
            self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.output / "jobs.sqlite3").exists())

    def test_main_failure_releases_run_lock_for_resume(self):
        fake = Mock()
        fake.run.side_effect = RuntimeError("Transient renderer failure")
        with patch("infofilm.image_animation_workflow.Workflow", return_value=fake):
            self.assertEqual(main(["--subject", "Circular motion", "--output", str(self.output)]), 1)
        fake.store.db.close.assert_called_once()
        with (self.output / ".workflow.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)

    def test_resume_dry_run_reports_saved_registry_plus_explicit_overrides(self):
        registry = load_registry(overrides={"teaching": "local", "motion": "local-reviewer"})
        write(self.output / "provider-registry.json", registry)
        with patch("sys.stdout", new=io.StringIO()) as captured, patch("httpx.Client", side_effect=AssertionError("No network")):
            self.assertEqual(main(["--subject", "Circular motion", "--output", str(self.output), "--resume", "--dry-run",
                "--review-model", "luna"]), 0)
        routes = json.loads(captured.getvalue())["routes"]
        self.assertEqual(routes["teaching"]["provider"], "local")
        self.assertEqual(routes["motion"]["provider"], "local-reviewer")
        self.assertEqual(routes["review"]["provider"], "luna")
        self.assertFalse((self.output / "jobs.sqlite3").exists())

    def test_imported_handoff_cannot_escape_output_with_unsafe_chapter_id(self):
        outside = self.root / "outside-output"
        source = write(self.root / "sources.json", {"results": [{"url": "https://institution.test/path",
            "raw_content": "A circle has a constant radius. " * 20}]})
        handoff = write(self.root / "handoff.json", {"film": {"subject": "Circular motion"},
            "chapter": {"id": str(outside), "title": "The radius", "objective": "Follow constant radius.",
                        "scope": {"description": "Describe the circle."}}})
        p = parser()
        args = resolve_args(p.parse_args(["--chapter-input", str(handoff), "--sources", str(source),
            "--output", str(self.output), "--no-subject-graph"]), p)
        workflow = Workflow(args, models_factory=lambda *a, **kw: self.models)
        self.addCleanup(workflow.store.db.close)
        with patch.object(workflow, "chapter", return_value={"id": str(outside), "state": "succeeded"}) as chapter, \
             self.assertRaisesRegex(ValueError, "[Ii][Dd]|safe"):
            workflow.run()
        chapter.assert_not_called()
        self.assertFalse(outside.exists())

    def test_actual_image_failure_requests_artifact_repair_without_seeking_another_score(self):
        workflow = self.workflow()
        image = self.root / "review-frame.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nvisible frame")
        self.facade.call_json.side_effect = [REJECT, ACCEPT]
        with self.assertRaises(Waiting) as caught:
            workflow.review_images(workflow.root, "actual-frame", {"message": "Keep constant radius."}, [image])
        self.assertEqual(caught.exception.state, "needs_review")
        self.assertEqual(self.facade.call_json.call_count, 1)
        self.assertFalse(read(caught.exception.details["verdict"])["pass"])
        self.assertIn("Repair the responsible", caught.exception.details["instruction"])

    def test_actual_image_review_reuses_accepted_verdict_until_image_contents_change(self):
        workflow = self.workflow()
        image = self.root / "review-frame.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nvisible frame")
        self.facade.call_json.return_value = ACCEPT
        context = {"message": "Keep constant radius."}
        first = workflow.review_images(workflow.root, "actual-frame", context, [image])
        second = workflow.review_images(workflow.root, "actual-frame", context, [image])
        self.assertEqual(first, second)
        self.assertEqual(self.facade.call_json.call_count, 1)
        image.write_bytes(image.read_bytes() + b"changed geometry")
        workflow.review_images(workflow.root, "actual-frame", context, [image])
        self.assertEqual(self.facade.call_json.call_count, 2)


if __name__ == "__main__":
    unittest.main()
