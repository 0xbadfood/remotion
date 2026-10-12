"""Narrow, deterministic repairs of frozen image-animation planning artifacts."""
from __future__ import annotations

from copy import deepcopy

from .image_animation_contracts import safe


CANVAS_REPAIR_SCHEMA = "infofilm.image-animation-canvas-repair.v1"
MAX_CANVAS_PROMPT_CHARS = 3700
MAX_CANVAS_REPAIR_REASON_CHARS = 2000
_REPAIR_KEYS = frozenset({"schema_version", "canvas_id", "prompt", "reason"})


def apply_canvas_prompt_repair(canvas: dict, document: dict) -> dict:
    """Copy a frozen canvas, replacing only its prompt and appending provenance.

    The import cannot change teaching ownership, the visual message, roles,
    controller or limitations. The exact replacement prompt is retained, and
    deterministic audit history allows the caller to pin a new asset namespace
    without mutating the previous plan or generated assets. No I/O is performed.
    """
    if not isinstance(canvas, dict) or not safe(canvas.get("id")):
        raise ValueError("Canvas prompt repair requires a canvas with a safe ID")
    original_prompt = canvas.get("prompt")
    if not isinstance(original_prompt, str) or not original_prompt.strip():
        raise ValueError("Canvas prompt repair requires the original frozen prompt")
    if not isinstance(document, dict) or set(document) != _REPAIR_KEYS:
        raise ValueError("Canvas prompt repair must contain exactly schema_version, canvas_id, prompt and reason")
    if document["schema_version"] != CANVAS_REPAIR_SCHEMA:
        raise ValueError("Wrong canvas prompt repair schema_version")
    if document["canvas_id"] != canvas["id"]:
        raise ValueError("Canvas prompt repair must retain the supplied canvas ID")
    for field, limit in (("prompt", MAX_CANVAS_PROMPT_CHARS), ("reason", MAX_CANVAS_REPAIR_REASON_CHARS)):
        value = document[field]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"Canvas prompt repair {field} must be nonempty text of at most {limit} characters")

    provenance = canvas.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ValueError("Frozen canvas provenance must be an object")
    history = provenance.get("canvas_prompt_repairs", [])
    if not isinstance(history, list) or any(not isinstance(entry, dict) for entry in history):
        raise ValueError("Frozen canvas prompt repair history must be a list of objects")

    repaired = deepcopy(canvas)
    repaired["prompt"] = document["prompt"]
    repaired.setdefault("provenance", {}).setdefault("canvas_prompt_repairs", []).append({
        "schema_version": CANVAS_REPAIR_SCHEMA,
        "canvas_id": canvas["id"],
        "reason": document["reason"],
        "original_prompt": original_prompt,
        "replacement_prompt": document["prompt"],
    })
    return repaired
