"""A prompt import must not revise accepted teaching or mutable source artifacts."""
from copy import deepcopy
import unittest

from infofilm.image_animation_repairs import (
    CANVAS_REPAIR_SCHEMA, MAX_CANVAS_PROMPT_CHARS, MAX_CANVAS_REPAIR_REASON_CHARS,
    apply_canvas_prompt_repair,
)


def fixture():
    return {
        "id": "K04", "beat_ids": ["B04"],
        "visual_message": "Earth's axial tilt explains the seasonal comparison.",
        "controller_type": "static_canvas", "prompt": "Four Earth positions around the Sun.",
        "component_roles": [], "limitations": ["Static comparison, not a live orbit."],
        "provenance": {"source": {"chapter": "C01", "accepted": True}},
    }


def repair():
    return {"schema_version": CANVAS_REPAIR_SCHEMA, "canvas_id": "K04",
            "prompt": "Exactly four distinct Earth positions around the central Sun.",
            "reason": "The reviewed canvas has five positions instead of the approved four."}


class CanvasPromptRepairTests(unittest.TestCase):
    def test_repair_preserves_frozen_fields_and_inputs_with_independent_audit_copy(self):
        canvas, document = fixture(), repair()
        original, original_document = deepcopy(canvas), deepcopy(document)
        result = apply_canvas_prompt_repair(canvas, document)
        self.assertEqual(canvas, original)
        self.assertEqual(document, original_document)
        self.assertEqual(result["prompt"], document["prompt"])
        self.assertEqual({key: value for key, value in result.items() if key not in {"prompt", "provenance"}},
                         {key: value for key, value in original.items() if key not in {"prompt", "provenance"}})
        self.assertEqual(result["provenance"]["source"], original["provenance"]["source"])
        self.assertEqual(result["provenance"]["canvas_prompt_repairs"], [{
            "schema_version": CANVAS_REPAIR_SCHEMA, "canvas_id": "K04", "reason": document["reason"],
            "original_prompt": original["prompt"], "replacement_prompt": document["prompt"],
        }])
        result["beat_ids"].append("B99")
        result["provenance"]["source"]["accepted"] = False
        self.assertEqual(canvas, original)

    def test_repeated_repairs_append_history_and_retain_each_original_prompt(self):
        first = apply_canvas_prompt_repair(fixture(), repair())
        prior = deepcopy(first)
        document = repair()
        document.update(prompt="Four Earth positions, one in each quadrant around the Sun.",
                        reason="Separate the four visible positions more clearly.")
        second = apply_canvas_prompt_repair(first, document)
        history = second["provenance"]["canvas_prompt_repairs"]
        self.assertEqual(first, prior)
        self.assertEqual(history[0], first["provenance"]["canvas_prompt_repairs"][0])
        self.assertEqual(history[1]["original_prompt"], first["prompt"])
        self.assertEqual(history[1]["replacement_prompt"], document["prompt"])
        self.assertEqual(apply_canvas_prompt_repair(first, document), second)

    def test_missing_provenance_is_created_without_mutating_the_canvas(self):
        canvas = fixture()
        del canvas["provenance"]
        result = apply_canvas_prompt_repair(canvas, repair())
        self.assertNotIn("provenance", canvas)
        self.assertEqual(len(result["provenance"]["canvas_prompt_repairs"]), 1)

    def test_identity_schema_and_extra_or_missing_structure_are_rejected(self):
        invalid = [None, [], {}, {**repair(), "canvas_id": "K03"},
                   {**repair(), "schema_version": "infofilm.image-animation-canvas-repair.v2"}]
        for field in repair():
            invalid.append({key: value for key, value in repair().items() if key != field})
        for key, value in (("beat_ids", ["B05"]), ("visual_message", "Changed lesson"),
                           ("component_roles", []), ("controller_type", "orbit_path"),
                           ("provenance", {"source": "replacement"}), ("canvas", fixture())):
            invalid.append({**repair(), key: value})
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(ValueError):
                apply_canvas_prompt_repair(fixture(), document)

    def test_text_type_emptiness_and_bounds_are_strict_but_exact_prompt_is_retained(self):
        for field, limit in (("prompt", MAX_CANVAS_PROMPT_CHARS),
                             ("reason", MAX_CANVAS_REPAIR_REASON_CHARS)):
            for value in (None, 123, [], {}, "", " \n\t", "x" * (limit + 1)):
                with self.subTest(field=field, value_type=type(value).__name__, length=len(value) if isinstance(value, str) else None), \
                     self.assertRaises(ValueError):
                    apply_canvas_prompt_repair(fixture(), {**repair(), field: value})
            document = {**repair(), field: "x" * limit}
            self.assertEqual(apply_canvas_prompt_repair(fixture(), document)["provenance"]["canvas_prompt_repairs"][-1]
                             ["replacement_prompt" if field == "prompt" else "reason"], document[field])
        document = {**repair(), "prompt": "  Four distinct Earth positions.\n"}
        self.assertEqual(apply_canvas_prompt_repair(fixture(), document)["prompt"], document["prompt"])

    def test_invalid_frozen_canvas_and_history_are_rejected_without_overwriting_them(self):
        invalid = [None, [], {}, {**fixture(), "id": "../K04"}, {**fixture(), "prompt": ""},
                   {**fixture(), "provenance": []},
                   {**fixture(), "provenance": {"canvas_prompt_repairs": {"reason": "old"}}},
                   {**fixture(), "provenance": {"canvas_prompt_repairs": ["old repair"]}}]
        for canvas in invalid:
            original = deepcopy(canvas)
            with self.subTest(canvas=canvas), self.assertRaises(ValueError):
                apply_canvas_prompt_repair(canvas, repair())
            self.assertEqual(canvas, original)


if __name__ == "__main__":
    unittest.main()
