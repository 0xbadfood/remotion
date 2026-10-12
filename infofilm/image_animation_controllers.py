"""Deterministic, allowlisted controllers for image-backed educational scenes.

All coordinates are source pixels with a top-left origin. Model responses may
select these controllers and provide inspected geometry/cues; they cannot supply
Python, Manim code, expressions, or new controller implementations.
"""
from __future__ import annotations

import copy
import importlib.util
import math
from pathlib import Path
import re
from typing import Any

SCENE_SCHEMA = "infofilm.image-animation-scene.v1"
TIMING_SCHEMA = "infofilm.image-animation-timing.v1"
COMPILED_SCHEMA = "infofilm.image-animation-compiled.v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")

# Public planner/grounding contract. Parameters are geometry, never executable
# snippets. Bindings associate the controller's roles with actual raster assets.
CONTROLLER_CAPABILITIES = {
    "static_canvas": {
        "value_unit": "constant", "period": None, "roles": [], "required_parameters": {},
        "optional_parameters": {"initial_value": "number"},
        "limitations": ["Static source illustration with code label cards"],
    },
    "orbit_path": {
        "value_unit": "degrees", "period": 360,
        "roles": ["orbiter"],
        "required_parameters": {"center_px": "point", "start_px": "point"},
        "optional_parameters": {"radius_px": "positive number (or radius_xy_px); circle radius inferred from center/start when both omitted",
            "radius_xy_px": "two positive numbers", "direction": "clockwise|counterclockwise",
            "draw_guide": "boolean", "show_relations": "boolean", "initial_value": "number"},
        "limitations": ["Measured circle or ellipse only; no arbitrary model-written paths"],
    },
    "shared_projectile_drop": {
        "value_unit": "flight progress in [0,1]", "period": None,
        "roles": ["fired", "dropped"],
        "required_parameters": {"fired_start_px": "point", "dropped_start_px": "point",
            "ground_y_px": "number", "radius_px": "positive number", "horizontal_range_px": "positive number"},
        "optional_parameters": {"launch_y_px": "number", "initial_value": "number"},
        "limitations": ["Horizontal release from equal height, uniform gravity, no air resistance",
            "Display time does not assert a physical metre/second scale"],
    },
    "ideal_slider_crank_four_stroke": {
        "value_unit": "degrees", "period": 720,
        "roles": ["piston", "crankshaft", "intake_valve", "exhaust_valve"],
        "optional_roles": ["connecting_rod"],
        "required_parameters": {"crank_center_px": "point", "crank_radius_px": "positive number",
            "rod_length_px": "number greater than crank_radius_px"},
        "required_landmarks": {"piston": ["wrist_pin_px"], "crankshaft": ["rotation_anchor_px"],
            "connecting_rod": ["endpoint_a_px", "endpoint_b_px"]},
        "optional_parameters": {"axis_x_px": "number (must equal crank center x)",
            "valve_lift_px": "nonnegative number", "piston_reference_theta_deg": "number",
            "piston_crown_offset_px": "number", "bore_xyxy_px": "box",
            "spark_tip_px": "point", "initial_value": "number"},
        "limitations": ["Ideal in-line slider crank; no thermodynamics or fluid simulation",
            "Ideal valve timing without advance, delay, or overlap"],
    },
}

SCENE_CONTRACT_EXAMPLE = {
    "schema_version": SCENE_SCHEMA, "canvas": {"width": 1600, "height": 900},
    "assets": {"background": {"path": "assets/background.png"},
        "ball": {"path": "assets/ball.png", "bbox_xyxy_px": [1120, 420, 1200, 500],
            "z_index": 5, "landmarks": {"centre_px": [1160, 460]}}},
    "controller": {"type": "orbit_path", "parameters": {"center_px": [800, 460],
        "start_px": [1160, 460], "radius_px": 360, "direction": "counterclockwise"},
        "bindings": {"orbiter": "ball"}},
    "landmarks": {}, "incomplete_flags": [], "provenance": {},
}
TIMING_CONTRACT_EXAMPLE = {
    "schema_version": TIMING_SCHEMA, "duration_seconds": 5, "audio_path": "narration/narration.wav",
    "scenes": [{"id": "S01", "title": "Follow the path", "start": 0, "end": 5,
        "display_labels": ["Constant radius"], "actions": [
            {"id": "circle", "kind": "move", "start": 1, "end": 4,
                "value_start": 0, "value_end": 360, "label": "One revolution", "word_ids": ["w001"]}]}],
}


class MissingControllerError(ValueError):
    """Unknown semantics need a new, reviewed controller rather than LLM code."""
    def __init__(self, name: str):
        self.request = {"schema_version": "infofilm.image-animation-controller-request.v1",
            "status": "missing_controller", "controller": name,
            "supported_controllers": sorted(CONTROLLER_CAPABILITIES),
            "required_work": "Implement and verify a scoped deterministic controller before rendering"}
        super().__init__(f"Missing controller {name!r}; supported: {', '.join(sorted(CONTROLLER_CAPABILITIES))}")


def controller_capabilities() -> dict:
    return copy.deepcopy(CONTROLLER_CAPABILITIES)


def finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def point(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} needs two source-pixel coordinates")
    return [finite(v, name) for v in value]


def box(value: Any, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{name} needs [left, top, right, bottom]")
    result = [finite(v, name) for v in value]
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError(f"{name} must have positive width and height")
    return result


def safe_id(value: Any, name: str = "id") -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f"Unsafe {name}: use letters, digits, hyphens and underscores")
    return value


def resolve_asset(value: Any, base: Path) -> Path:
    """Relative references remain inside their bundle; absolute imports are explicit."""
    if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
        raise ValueError("An asset needs a filesystem path")
    base = base.resolve()
    path = Path(value)
    if not path.is_absolute():
        path = (base / path).resolve()
        if not path.is_relative_to(base):
            raise ValueError("Relative asset path escapes its scene/narration bundle")
    else:
        path = path.resolve()
    if not path.is_file():
        raise ValueError(f"Missing asset: {path}")
    return path


def slider_crank(theta: float, mechanism: dict) -> dict:
    """Ideal in-line geometry, extracted from the frozen four-stroke pilot."""
    cx, cy = point(mechanism["crank_center_px"], "crank center")
    radius = finite(mechanism["crank_radius_px"], "crank radius")
    length = finite(mechanism["rod_length_px"], "rod length")
    if not 0 < radius < length:
        raise ValueError("Slider crank requires 0 < crank radius < rod length")
    axis = finite(mechanism.get("axis_x_px", cx), "slider axis")
    if abs(axis - cx) > 1e-6:
        raise ValueError("The ideal slider crank requires an in-line axis")
    theta = finite(theta, "theta")
    pin = [cx + radius * math.sin(theta), cy - radius * math.cos(theta)]
    wrist = [axis, pin[1] - math.sqrt(length**2 - (radius * math.sin(theta))**2)]
    return {"crank_pin_px": pin, "wrist_pin_px": wrist, "rod_length_px": math.dist(pin, wrist)}


def cycle_state(theta: float, mechanism: dict) -> dict:
    phase = theta % (4 * math.pi)
    index = min(3, int(phase / math.pi))
    stroke = ("intake", "compression", "power", "exhaust")[index]
    progress = (phase - index * math.pi) / math.pi
    lift = finite(mechanism.get("valve_lift_px", 12), "valve lift")
    if lift < 0:
        raise ValueError("Valve lift must be nonnegative")
    lifted = lift * max(0, math.sin(math.pi * progress))
    return {**slider_crank(theta, mechanism), "theta_rad": theta, "stroke": stroke,
        "stroke_progress": progress, "intake_lift_px": lifted if stroke == "intake" else 0,
        "exhaust_lift_px": lifted if stroke == "exhaust" else 0,
        "spark": stroke == "power" and progress < .075}


def compile_scene(scene: dict, *, allow_incomplete_preview: bool = False) -> dict:
    scene = copy.deepcopy(scene)
    if scene.get("schema_version") != SCENE_SCHEMA:
        raise ValueError(f"Expected {SCENE_SCHEMA}; adapt frozen pilots explicitly")
    flags = scene.get("incomplete_flags", [])
    if flags and not allow_incomplete_preview:
        raise ValueError(f"Incomplete geometry/assets block final rendering: {flags}")
    provenance = scene.get("provenance", {})
    if provenance.get("known_crank_occlusion") and not flags:
        completion = provenance.get("manual_completion")
        if not isinstance(completion, dict) or not str(completion.get("method", "")).strip() or not str(completion.get("reason", "")).strip():
            raise ValueError("Clearing the known crank occlusion requires manual_completion provenance with method and reason")
    canvas = scene.get("canvas", {})
    width, height = finite(canvas.get("width"), "source width"), finite(canvas.get("height"), "source height")
    if width <= 0 or height <= 0 or not width.is_integer() or not height.is_integer():
        raise ValueError("Source canvas dimensions must be positive integers")
    assets = scene.get("assets", {})
    if not isinstance(assets, dict) or "background" not in assets:
        raise ValueError("A reconstructed image background is required")
    for identity, asset in assets.items():
        safe_id(identity, "asset id")
        if not isinstance(asset, dict) or not asset.get("path"):
            raise ValueError(f"Asset {identity} needs a path")
        finite(asset.get("z_index", -20 if identity == "background" else 5), "z index")
        if asset.get("bbox_xyxy_px") is not None:
            bounds = box(asset["bbox_xyxy_px"], identity + " bbox")
            if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > width or bounds[3] > height:
                raise ValueError(f"{identity}: source bounding box leaves canvas")
        for key in ("centre_px", "size_px"):
            if key in asset:
                xy = point(asset[key], identity + " " + key)
                if key == "size_px" and min(xy) <= 0:
                    raise ValueError(f"{identity}: source size must be positive")
        for key, xy in asset.get("landmarks", {}).items():
            point(xy, identity + " landmark " + key)
    for cavity in scene.get("cavity_masks", []):
        if cavity.get("type") == "rectangle":
            x1, y1, x2, y2 = box(cavity.get("bbox_xyxy_px"), "cavity bbox")
            if x1 < 0 or y1 < 0 or x2 > width or y2 > height:
                raise ValueError("Cavity repair leaves the source canvas")
        elif cavity.get("type") == "circle":
            xy = point(cavity.get("center_px"), "cavity center")
            radius = finite(cavity.get("radius_px"), "cavity radius")
            if radius <= 0 or xy[0]-radius < 0 or xy[0]+radius > width or xy[1]-radius < 0 or xy[1]+radius > height:
                raise ValueError("Cavity repair needs a positive visible source radius")
        else:
            raise ValueError("Only rectangle/circle cavity repairs are supported")
    controller = scene.get("controller", {})
    name = controller.get("type")
    if name not in CONTROLLER_CAPABILITIES:
        raise MissingControllerError(str(name))
    parameters = copy.deepcopy(controller.get("parameters", {}))
    bindings = copy.deepcopy(controller.get("bindings", {}))
    capability = CONTROLLER_CAPABILITIES[name]
    for role in capability["roles"]:
        if role not in bindings or bindings[role] not in assets:
            raise ValueError(f"Controller {name} requires a raster binding for {role}")
    allowed_roles = set(capability["roles"] + capability.get("optional_roles", []))
    if set(bindings) - allowed_roles or len(set(bindings.values())) != len(bindings):
        raise ValueError("Controller bindings must use distinct assets and supported roles")
    for role, asset_id in bindings.items():
        if asset_id not in assets or asset_id == "background":
            raise ValueError(f"Missing moving raster binding: {role}")
    initial = finite(parameters.get("initial_value", 0), "initial value")
    if name == "static_canvas":
        if bindings:
            raise ValueError("Static canvas has no moving bindings")
    elif name == "orbit_path":
        center = point(parameters.get("center_px"), "orbit center")
        start = point(parameters.get("start_px"), "orbit start")
        if "radius_xy_px" not in parameters and "radius_px" not in parameters:
            # Grounding supplies measured anchors; the controller owns their distance.
            radius = math.dist(center, start)
            radii = [radius, radius]
        else:
            radii = point(parameters.get("radius_xy_px", [parameters.get("radius_px")] * 2), "orbit radii")
        if min(radii) <= 0:
            raise ValueError("Orbit radii must be positive")
        direction = parameters.get("direction", "counterclockwise")
        if direction not in {"clockwise", "counterclockwise"}:
            raise ValueError("Orbit direction must be clockwise or counterclockwise")
        norm = math.hypot((start[0]-center[0])/radii[0], (start[1]-center[1])/radii[1])
        if abs(norm-1) > .02:
            raise ValueError("Orbit start must lie on the measured circle/ellipse")
        if center[0]-radii[0] < 0 or center[0]+radii[0] > width or center[1]-radii[1] < 0 or center[1]+radii[1] > height:
            raise ValueError("Orbit leaves the source canvas")
        parameters.update(center_px=center, start_px=start, radius_xy_px=radii,
            direction=direction, start_angle_rad=math.atan2(-(start[1]-center[1])/radii[1], (start[0]-center[0])/radii[0]))
    elif name == "shared_projectile_drop":
        fired = point(parameters.get("fired_start_px"), "fired start")
        dropped = point(parameters.get("dropped_start_px"), "dropped start")
        ground = finite(parameters.get("ground_y_px"), "ground")
        radius = finite(parameters.get("radius_px"), "radius")
        distance = finite(parameters.get("horizontal_range_px"), "horizontal range")
        launch = finite(parameters.get("launch_y_px", (fired[1]+dropped[1])/2), "launch height")
        if abs(fired[1]-launch) > .5 or abs(dropped[1]-launch) > .5:
            raise ValueError("Shared projectile/drop requires explicitly normalised equal launch heights")
        if not (radius > 0 and distance > 0 and 0 < ground <= height and launch + radius < ground):
            raise ValueError("Projectile geometry needs elevated release, positive radius/range and visible ground")
        if fired[0]+distance+radius > width or min(fired[0], dropped[0])-radius < 0 or launch-radius < 0:
            raise ValueError("Projectile/drop would leave the source canvas")
        parameters.update(fired_start_px=fired, dropped_start_px=dropped, ground_y_px=ground,
            radius_px=radius, horizontal_range_px=distance, launch_y_px=launch, fall_px=ground-radius-launch)
        if not 0 <= initial <= 1:
            raise ValueError("Projectile progress must lie in [0,1]")
    else:
        parameters["crank_center_px"] = point(parameters.get("crank_center_px"), "crank center")
        for key in ("crank_radius_px", "rod_length_px"):
            parameters[key] = finite(parameters.get(key), key)
        for key in ("axis_x_px", "piston_reference_theta_deg", "piston_crown_offset_px", "valve_lift_px", "rod_width_px"):
            if key in parameters:
                parameters[key] = finite(parameters[key], key)
        slider_crank(0, parameters)
        for degree in range(0, 721, 5):
            state = cycle_state(math.radians(degree), parameters)
            for key in ("crank_pin_px", "wrist_pin_px"):
                xy = state[key]
                if not 0 <= xy[0] <= width or not 0 <= xy[1] <= height:
                    raise ValueError(f"Slider-crank {key} leaves the source canvas")
        for role, keys in capability.get("required_landmarks", {}).items():
            if role not in bindings:
                continue
            asset = assets[bindings[role]]
            landmarks = {**asset, **asset.get("landmarks", {})}
            for key in keys:
                if key not in landmarks:
                    raise ValueError(f"{role}: missing required source landmark {key}")
                point(landmarks[key], role + " " + key)
        if finite(parameters.get("valve_lift_px", 12), "valve lift") < 0:
            raise ValueError("Valve lift must be nonnegative")
        for key in ("spark_tip_px",):
            if key in parameters:
                point(parameters[key], key)
        if "bore_xyxy_px" in parameters:
            bore = box(parameters["bore_xyxy_px"], "bore")
            if "piston_crown_offset_px" in parameters:
                crown = slider_crank(0, parameters)["wrist_pin_px"][1] + finite(parameters["piston_crown_offset_px"], "crown offset")
                if crown <= bore[1]+1:
                    raise ValueError("Piston crown collides with the chamber head at top dead centre")
        rod = assets.get(bindings.get("connecting_rod"), {})
        if rod:
            landmarks = {**rod, **rod.get("landmarks", {})}
            if "endpoint_a_px" not in landmarks or "endpoint_b_px" not in landmarks:
                raise ValueError("A connecting-rod raster needs both source endpoint landmarks")
            a, b = point(landmarks["endpoint_a_px"], "rod endpoint a"), point(landmarks["endpoint_b_px"], "rod endpoint b")
            if math.dist(a, b) < 1:
                raise ValueError("Connecting-rod source endpoints must be distinct")
    compiled = {"schema_version": COMPILED_SCHEMA, "type": name, "parameters": parameters,
        "bindings": bindings, "initial_value": initial, "canvas": {"width": int(width), "height": int(height)},
        "preview_incomplete": bool(flags), "limitations": capability["limitations"]}
    compiled["geometry_checks"] = geometry_checks(compiled)
    if not all(compiled["geometry_checks"].values()):
        raise ValueError(f"Controller geometry failed: {compiled['geometry_checks']}")
    return compiled


def state_at(controller: dict, value: float) -> dict:
    value = finite(value, "controller value")
    name, p = controller["type"], controller["parameters"]
    if name == "static_canvas":
        return {"value": value, "positions_px": {}}
    if name == "orbit_path":
        sign = 1 if p["direction"] == "counterclockwise" else -1
        angle = p["start_angle_rad"] + sign * math.radians(value)
        center, radii = p["center_px"], p["radius_xy_px"]
        position = [center[0]+radii[0]*math.cos(angle), center[1]-radii[1]*math.sin(angle)]
        tangent = [-sign*radii[0]*math.sin(angle), -sign*radii[1]*math.cos(angle)]
        return {"value": value, "positions_px": {"orbiter": position}, "angle_rad": angle,
            "center_px": center, "tangent_px": tangent}
    if name == "shared_projectile_drop":
        if not 0 <= value <= 1:
            raise ValueError("Projectile progress must lie in [0,1]")
        y = p["launch_y_px"] + p["fall_px"] * value**2
        return {"value": value, "positions_px": {
            "fired": [p["fired_start_px"][0]+p["horizontal_range_px"]*value, y],
            "dropped": [p["dropped_start_px"][0], y]}, "landed": value >= 1}
    if name != "ideal_slider_crank_four_stroke":
        raise MissingControllerError(name)
    state = cycle_state(math.radians(value), p)
    reference = slider_crank(math.radians(float(p.get("piston_reference_theta_deg", 0))), p)
    return {"value": value, **state, "positions_px": {}, "offsets_px": {
        "piston": [state["wrist_pin_px"][0]-reference["wrist_pin_px"][0], state["wrist_pin_px"][1]-reference["wrist_pin_px"][1]],
        "intake_valve": [0, state["intake_lift_px"]], "exhaust_valve": [0, state["exhaust_lift_px"]]},
        "rotation_rad": {"crankshaft": math.radians(value-float(p.get("piston_reference_theta_deg", 0)))}}


def geometry_checks(controller: dict) -> dict[str, bool]:
    name, p = controller["type"], controller["parameters"]
    if name == "static_canvas":
        return {"static_canvas": True}
    if name == "orbit_path":
        states = [state_at(controller, degree) for degree in range(0, 361, 3)]
        center, radii = p["center_px"], p["radius_xy_px"]
        return {"ellipse_equation_preserved": all(abs(((s["positions_px"]["orbiter"][0]-center[0])/radii[0])**2+
            ((s["positions_px"]["orbiter"][1]-center[1])/radii[1])**2-1) < 1e-9 for s in states),
            "orbit_closes": math.dist(states[0]["positions_px"]["orbiter"], states[-1]["positions_px"]["orbiter"]) < 1e-7}
    if name == "shared_projectile_drop":
        states = [state_at(controller, i/100) for i in range(101)]
        return {"shared_vertical_clock": all(s["positions_px"]["fired"][1] == s["positions_px"]["dropped"][1] for s in states),
            "simultaneous_ground_contact": all(abs(xy[1]+p["radius_px"]-p["ground_y_px"]) < 1e-7 for xy in states[-1]["positions_px"].values()),
            "dropped_has_no_horizontal_motion": all(s["positions_px"]["dropped"][0] == p["dropped_start_px"][0] for s in states)}
    states = [cycle_state(math.radians(degree), p) for degree in range(721)]
    return {"rod_length_preserved": all(abs(s["rod_length_px"]-p["rod_length_px"]) < 1e-7 for s in states),
        "stroke_equals_twice_crank_radius": abs(slider_crank(math.pi, p)["wrist_pin_px"][1]-slider_crank(0, p)["wrist_pin_px"][1]-2*p["crank_radius_px"]) < 1e-7,
        "two_revolution_cycle_closes": math.dist(states[0]["wrist_pin_px"], states[-1]["wrist_pin_px"]) < 1e-7,
        "valves_closed_on_compression_and_power": all(s["intake_lift_px"] == s["exhaust_lift_px"] == 0 for s in states if s["stroke"] in {"compression", "power"}),
        "intake_opens_only_on_intake": all(not s["intake_lift_px"] or s["stroke"] == "intake" for s in states),
        "exhaust_opens_only_on_exhaust": all(not s["exhaust_lift_px"] or s["stroke"] == "exhaust" for s in states)}


MOTION_KINDS = {"move", "orbit", "flight", "stroke", "stroke_demo", "loop", "replay", "hold"}
PRESENTATION_KINDS = {"label", "highlight", "component_highlight", "stage_label", "spark", "ignition", "valve", "flow", "note"}


def _equivalent(a: float, b: float, controller: dict) -> bool:
    period = CONTROLLER_CAPABILITIES[controller["type"]]["period"]
    return abs(a-b) < 1e-7 or (period is not None and abs((a-b)/period-round((a-b)/period)) < 1e-7)


def compile_timeline(timings: dict, controller: dict, *, fps: int = 30) -> dict:
    """Own the complete MFA clock once, with automatic holds between explicit cues."""
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise ValueError("FPS must be a positive integer")
    rows = timings.get("scenes")
    if not isinstance(rows, list) or not rows:
        raise ValueError("An MFA-derived scene timeline is required")
    scenes, previous_end, previous_frame = [], 0.0, 0
    carried = controller["initial_value"]
    identities = set()
    for source in rows:
        row = copy.deepcopy(source)
        identity = safe_id(row.get("id"), "scene id")
        if identity in identities:
            raise ValueError("Scene IDs must be unique")
        identities.add(identity)
        start, end = finite(row.get("start"), "scene start"), finite(row.get("end"), "scene end")
        if abs(start-previous_end) > 1e-6 or end <= start:
            raise ValueError("Scenes must cover the narration clock contiguously from zero")
        start_frame, end_frame = previous_frame, round(end*fps)
        if end_frame <= start_frame:
            raise ValueError("Every scene must own at least one frame")
        actions = row.get("actions", [])
        if not isinstance(actions, list):
            raise ValueError("Scene actions must be a list")
        action_ids = set()
        for index, action in enumerate(actions):
            action.setdefault("id", f"cue-{index+1:03d}")
            safe_id(action["id"], "action id")
            if action["id"] in action_ids:
                raise ValueError("Action IDs must be unique within a scene")
            action_ids.add(action["id"])
            if action.get("kind") not in MOTION_KINDS | PRESENTATION_KINDS:
                raise ValueError(f"Unsupported scene action: {action.get('kind')!r}; use an allowlisted action")
            a, b = finite(action.get("start"), "action start"), finite(action.get("end"), "action end")
            if a < start-1e-6 or b > end+1e-6 or b <= a:
                raise ValueError(f"{identity}: action window must fit its MFA scene")
        supplied = row.get("motion", row.get("motion_segments"))
        motions = copy.deepcopy(supplied) if supplied is not None else [copy.deepcopy(a) for a in actions if a["kind"] in MOTION_KINDS]
        if not isinstance(motions, list):
            raise ValueError("Scene motion must be a list")
        motions.sort(key=lambda s: finite(s.get("start"), "motion start"))
        segments, cursor, first_value = [], start, carried
        for segment in motions:
            a, b = finite(segment.get("start"), "motion start"), finite(segment.get("end"), "motion end")
            if a < cursor-1e-6 or b > end+1e-6 or b <= a:
                raise ValueError("Motion windows must be nonoverlapping and inside their MFA scene")
            kind = segment.get("kind", "move")
            if kind not in MOTION_KINDS:
                raise ValueError(f"Unsupported motion kind: {kind}")
            v0 = finite(segment.get("value_start", segment.get("theta_start_deg", carried)), "motion value_start")
            v1 = finite(segment.get("value_end", segment.get("theta_end_deg", v0)), "motion value_end")
            loops = segment.get("loops", 1)
            if isinstance(loops, bool) or not isinstance(loops, int) or loops < 1 or loops > 10000:
                raise ValueError("Motion loops must be a positive bounded integer")
            if kind == "hold" and not _equivalent(v0, v1, controller):
                raise ValueError("Hold actions cannot move")
            if not _equivalent(carried, v0, controller) and kind != "replay":
                raise ValueError("Unexplained motion reset; use an explicit replay cue")
            state_at(controller, v0); state_at(controller, v1)
            if controller["type"] == "static_canvas" and (v0 != v1 or kind != "hold"):
                raise ValueError("Static canvas supports hold motion only")
            if a > cursor:
                segments.append({"kind": "hold", "start": cursor, "end": a, "value_start": carried, "value_end": carried, "loops": 1})
            segments.append({**segment, "kind": kind, "start": a, "end": b, "value_start": v0, "value_end": v1, "loops": loops})
            carried = v1 if kind == "replay" else v0+(v1-v0)*loops
            if controller["type"] == "shared_projectile_drop" and loops > 1 and kind != "replay":
                raise ValueError("Repeat a projectile flight with an explicit replay cue")
            state_at(controller, carried)
            cursor = b
        if cursor < end:
            segments.append({"kind": "hold", "start": cursor, "end": end, "value_start": carried, "value_end": carried, "loops": 1})
        row.update(start=start, end=end, start_frame=start_frame, end_frame=end_frame,
            frames=end_frame-start_frame, render_duration=(end_frame-start_frame)/fps,
            render_start=start_frame/fps, render_end=end_frame/fps,
            initial_value=first_value, final_value=carried, segments=segments)
        scenes.append(row)
        previous_end, previous_frame = end, end_frame
    duration = finite(timings.get("duration_seconds", previous_end), "narration duration")
    if abs(duration-previous_end) > 1e-6:
        raise ValueError("Final scene must include the complete narration and trailing silence")
    return {"schema_version": COMPILED_SCHEMA, "fps": fps, "scenes": scenes,
        "frames": previous_frame, "source_duration_seconds": duration, "duration_seconds": previous_frame/fps,
        "checks": {"contiguous_frame_ownership": True, "complete_narration_clock": True,
            "explicit_motion_cues": True}}


def value_at(time_seconds: float, segments: list[dict], initial_value: float = 0) -> float:
    value = initial_value
    for segment in segments:
        if time_seconds < segment["start"]:
            return value
        a, b = segment["value_start"], segment["value_end"]
        loops = segment.get("loops", 1)
        if time_seconds < segment["end"]:
            progress = max(0.0, (time_seconds-segment["start"])/(segment["end"]-segment["start"]))
            progress *= loops
            if segment["kind"] == "replay":
                progress %= 1
            return a+(b-a)*progress
        value = b if segment["kind"] == "replay" else a+(b-a)*loops
    return value


def normalise_pilot_scene(scene: dict, *, background: str | None = None,
                          layers: dict[str, str] | None = None) -> dict:
    """Explicit adapters preserve calibration; they never pretend it was automatic."""
    layers = layers or {}
    result = {"schema_version": SCENE_SCHEMA, "canvas": copy.deepcopy(scene["canvas"]),
        "assets": {}, "landmarks": {}, "incomplete_flags": copy.deepcopy(scene.get("incomplete_flags", [])),
        "provenance": {**copy.deepcopy(scene.get("provenance", {})), "adapter": "frozen pilot source-pixel rig",
            "manual_calibration": True, "automatic_grounding": False}}
    if "mechanism" in scene:
        result["assets"] = copy.deepcopy(scene["assets"])
        result["controller"] = {"type": "ideal_slider_crank_four_stroke", "parameters": copy.deepcopy(scene["mechanism"]),
            "bindings": {role: role for role in CONTROLLER_CAPABILITIES["ideal_slider_crank_four_stroke"]["roles"]}}
        if "connecting_rod" in result["assets"]:
            result["controller"]["bindings"]["connecting_rod"] = "connecting_rod"
        result["cavity_masks"] = copy.deepcopy(scene.get("cavity_masks", []))
        result["provenance"]["known_crank_occlusion"] = bool(scene.get("assets", {}).get("crankshaft", {}).get("occluded_by"))
        result["provenance"]["source_rod_occlusion"] = bool(scene.get("assets", {}).get("connecting_rod", {}).get("visible_top_occluded"))
        if result["provenance"]["known_crank_occlusion"]:
            result["incomplete_flags"].append("crankshaft source silhouette is partially occluded; complete source part before final rotation")
    elif "path" in scene:
        if not background or not layers.get("orbiter"):
            raise ValueError("Circular pilot adapter needs background and orbiter image paths")
        spec = copy.deepcopy(scene.get("ball", {}))
        spec["path"] = layers["orbiter"]
        if "bounding_box_xyxy_px" in spec:
            spec["bbox_xyxy_px"] = spec.pop("bounding_box_xyxy_px")
        result["assets"] = {"background": {"path": background}, "ball": spec}
        result["controller"] = {"type": "orbit_path", "parameters": copy.deepcopy(scene["path"]), "bindings": {"orbiter": "ball"}}
    elif "balls" in scene:
        if not background:
            raise ValueError("Gravity pilot adapter needs a reconstructed background path")
        balls = scene["balls"]
        radius = float(scene.get("physics", {}).get("radius_px", sum(b["size_px"][1] for b in balls.values())/4))
        launch = sum(b["centre_px"][1] for b in balls.values())/2
        result["assets"] = {"background": {"path": background}}
        for role in ("fired", "dropped"):
            result["assets"][role] = {**copy.deepcopy(balls[role]), "path": layers.get(role, balls[role].get("layer")),
                "size_px": [2*radius, 2*radius], "centre_px": [balls[role]["centre_px"][0], launch]}
        result["controller"] = {"type": "shared_projectile_drop", "bindings": {"fired": "fired", "dropped": "dropped"},
            "parameters": {**copy.deepcopy(scene.get("physics", {})), "ground_y_px": scene["ground_y_px"],
                "radius_px": radius, "launch_y_px": launch,
                "fired_start_px": [balls["fired"]["centre_px"][0], launch], "dropped_start_px": [balls["dropped"]["centre_px"][0], launch]}}
        result["provenance"]["normalisation"] = "Frozen pilot explicitly equalises tiny launch-height and radius differences"
    else:
        raise ValueError("Unrecognised frozen pilot; provide the normalised scene contract")
    return result


def normalise_pilot_timings(timings: dict, controller: dict) -> dict:
    """Read only the explicit frozen engine MFA adapter; no new timing fallback."""
    if timings.get("schema_version") != "engine-narration-scenes.v1" or controller["type"] != "ideal_slider_crank_four_stroke":
        raise ValueError("Only the frozen engine MFA scene manifest has a pilot timing adapter")
    pilot = Path(__file__).resolve().parent.parent / "scripts" / "experiment-four-stroke-image-manim.py"
    spec = importlib.util.spec_from_file_location("_frozen_image_engine_pilot", pilot)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = copy.deepcopy(timings)
    result["schema_version"] = TIMING_SCHEMA
    result["provenance"] = {"adapter": "frozen manually calibrated engine MFA cues", "manual_calibration": True}
    carried = controller["initial_value"]
    for row in result["scenes"]:
        row["motion"] = [{"kind": "move", "start": s["start"], "end": s["end"],
            "value_start": math.degrees(s["theta_start"]), "value_end": math.degrees(s["theta_end"]), "label": s["label"]}
            for s in module.build_motion(row)]
        for segment in row["motion"]:
            if not _equivalent(carried, segment["value_start"], controller):
                segment["kind"] = "replay"
                segment["reset_provenance"] = "Frozen pilot explicitly resets the diagram at this narrated demonstration"
            carried = segment["value_end"]
        for action in row.get("actions", []):
            # Motion derives from the frozen adapter; all original phrase cues
            # remain presentational here, preventing their duplicate execution.
            action["pilot_kind"] = action.get("kind")
            action["kind"] = "spark" if action.get("kind") in {"spark", "ignition"} else "label"
    return result
