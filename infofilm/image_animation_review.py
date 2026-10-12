"""CPU review visualizations of native layers, without changing source pixels."""
from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
import textwrap

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .image_animation_controllers import MOTION_KINDS, PRESENTATION_KINDS, compile_scene, finite, point, resolve_asset, state_at
from .image_animation_renderer import prepare_assets

SHEET_SCHEMA = "infofilm.image-animation-layer-review-sheet.v1"


def _rgba(path):
    with Image.open(path) as image:
        return image.convert("RGBA")


def _checkerboard(size, tile=16):
    image = Image.new("RGBA", size, (224, 228, 232, 255))
    draw = ImageDraw.Draw(image)
    for y in range(0, size[1], tile):
        for x in range(0, size[0], tile):
            if (x // tile + y // tile) % 2:
                draw.rectangle((x, y, x + tile - 1, y + tile - 1), fill=(190, 198, 206, 255))
    return image


def _rotate(vector, angle):
    c, s = math.cos(angle), math.sin(angle)
    return [c * vector[0] - s * vector[1], s * vector[0] + c * vector[1]]


def _registration(controller, sprites):
    """Apply the same initial transforms as the allowlisted CPU renderer."""
    state = state_at(controller, controller["initial_value"])
    bindings = controller["bindings"]
    registered = {identity: {"centre_px": list(sprite["centre_px"]),
        "size_px": list(sprite["size_px"]), "rotation_rad": 0.0,
        "z_index": float(sprite.get("z_index", -20 if identity == "background" else 5))}
        for identity, sprite in sprites.items()}
    for role, xy in state.get("positions_px", {}).items():
        registered[bindings[role]]["centre_px"] = list(xy)
    for role, offset in state.get("offsets_px", {}).items():
        identity = bindings[role]
        registered[identity]["centre_px"] = [sprites[identity]["centre_px"][i] + offset[i] for i in range(2)]
    for role, angle in state.get("rotation_rad", {}).items():
        identity = bindings[role]
        sprite = sprites[identity]
        anchor = point(sprite.get("rotation_anchor_px", controller["parameters"].get("crank_center_px")),
                       "review rotation anchor")
        relative = _rotate([sprite["centre_px"][i] - anchor[i] for i in range(2)], angle)
        registered[identity].update(centre_px=[anchor[i] + relative[i] for i in range(2)], rotation_rad=angle)
    if controller["type"] == "ideal_slider_crank_four_stroke" and "connecting_rod" in bindings:
        identity = bindings["connecting_rod"]
        sprite = sprites[identity]
        source_a, source_b = point(sprite["endpoint_a_px"], "rod endpoint a"), point(sprite["endpoint_b_px"], "rod endpoint b")
        target_a, target_b = state["wrist_pin_px"], state["crank_pin_px"]
        source_vector = [source_b[i] - source_a[i] for i in range(2)]
        target_vector = [target_b[i] - target_a[i] for i in range(2)]
        ratio = math.hypot(*target_vector) / math.hypot(*source_vector)
        angle = math.atan2(target_vector[1], target_vector[0]) - math.atan2(source_vector[1], source_vector[0])
        relative = _rotate([sprite["centre_px"][i] - source_a[i] for i in range(2)], angle)
        registered[identity].update(centre_px=[target_a[i] + ratio * relative[i] for i in range(2)],
            size_px=[ratio * v for v in sprite["size_px"]], rotation_rad=angle)
    return state, registered


def _registered_layer(image, placement, canvas_size):
    """Inverse affine sampling maps native alpha support into source pixels."""
    cx, cy = placement["centre_px"]
    width, height = placement["size_px"]
    if min(width, height) <= 0:
        raise ValueError("Review registration needs positive sprite dimensions")
    sx, sy = width / image.width, height / image.height
    c, s = math.cos(placement["rotation_rad"]), math.sin(placement["rotation_rad"])
    a, b, d, e = c / sx, s / sx, -s / sy, c / sy
    coefficients = (a, b, image.width / 2 - a * cx - b * cy,
                    d, e, image.height / 2 - d * cx - e * cy)
    return image.transform(canvas_size, Image.Transform.AFFINE, coefficients,
                           resample=Image.Resampling.BICUBIC)


def build_layer_review_sheet(scene, scene_path, canvas_path, output_dir):
    """Return (PNG path, metadata) for one labeled, alpha-aware layer review.

    The source canvas and clean background keep full source registration. Parts
    are shown separately as zoomed native alpha crops over checkerboard, then
    composited at the controller's initial state using renderer-prepared geometry.
    Only review artifacts are written; input PNGs and scene objects are preserved.
    """
    controller = compile_scene(scene)
    scene_path, canvas_path, output = Path(scene_path).resolve(), Path(canvas_path).resolve(), Path(output_dir).resolve()
    canvas_size = (controller["canvas"]["width"], controller["canvas"]["height"])
    source_paths = {identity: resolve_asset(asset["path"], scene_path.parent)
                    for identity, asset in scene["assets"].items()}
    if not canvas_path.is_file():
        raise ValueError("Layer review needs the original canvas image")
    original = _rgba(canvas_path)
    if original.size != canvas_size:
        raise ValueError("Original canvas dimensions must match the source scene")
    prepared = output / "prepared-assets"
    destinations = {output / "layer-review-sheet.png", output / "assembled-initial.png",
                    *(prepared / f"{identity}.png" for identity in source_paths if identity != "background")}
    originals = {canvas_path, *source_paths.values()}
    if {path.resolve() for path in destinations} & originals:
        raise ValueError("Review artifacts must not overwrite original images")
    sprites = prepare_assets(scene, scene_path, prepared)
    state, registered = _registration(controller, sprites)
    assembled = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    for identity in sorted(sprites, key=lambda i: registered[i]["z_index"]):
        layer = _registered_layer(_rgba(sprites[identity]["path"]), registered[identity], canvas_size)
        assembled = Image.alpha_composite(assembled, layer)
    assembled_path = output / "assembled-initial.png"
    assembled.save(assembled_path)

    roles = {identity: role for role, identity in controller["bindings"].items()}
    panels = [("source_canvas", "1. ORIGINAL CANVAS", "Source composition and original registration", original,
               {"source_path": str(canvas_path)}),
              ("clean_background", "2. CLEAN BACKGROUND", "Fixed layer after the moving parts were removed",
               _rgba(sprites["background"]["path"]), {"asset_id": "background"})]
    for identity, sprite in sprites.items():
        if identity == "background":
            continue
        label = f"{len(panels) + 1}. ISOLATED PART: {identity}"
        if identity in roles:
            label += f" [{roles[identity]}]"
        panels.append(("isolated:" + identity, label,
            "Native alpha crop, enlarged over checkerboard for inspection", _rgba(sprite["path"]),
            {"asset_id": identity, "alpha_support_xyxy_px": sprite["alpha_crop_xyxy"]}))
    panels.append(("assembled_initial_state", f"{len(panels) + 1}. ASSEMBLED INITIAL STATE",
        "Parts at runtime size and position, over the clean background", assembled, {}))

    panel_width, panel_height, header_height, padding = 640, 420, 72, 12
    columns = 2
    sheet = Image.new("RGB", (columns * panel_width, math.ceil(len(panels) / columns) * panel_height), "#101820")
    draw = ImageDraw.Draw(sheet)
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font = ImageFont.truetype(str(font_path), 18) if font_path.is_file() else ImageFont.load_default()
    caption_font = ImageFont.truetype(str(font_path), 14) if font_path.is_file() else ImageFont.load_default()
    descriptions = []
    for index, (identity, label, caption, image, info) in enumerate(panels):
        x, y = (index % columns) * panel_width, (index // columns) * panel_height
        draw.rectangle((x, y, x + panel_width - 1, y + panel_height - 1), outline="#526575", width=2)
        lines = textwrap.wrap(label, width=53)
        for line_index, line in enumerate(lines[:2]):
            draw.text((x + padding, y + 9 + 21 * line_index), line, font=font, fill="white")
        draw.text((x + padding, y + 52), caption, font=caption_font, fill="#C1CCD6")
        area = (panel_width - 2 * padding, panel_height - header_height - padding)
        display = ImageOps.contain(image, area, method=Image.Resampling.LANCZOS)
        tile = _checkerboard(area)
        dx, dy = (area[0] - display.width) // 2, (area[1] - display.height) // 2
        tile.alpha_composite(display, (dx, dy))
        sheet.paste(tile.convert("RGB"), (x + padding, y + header_height))
        descriptions.append({"id": identity, "label": label, "caption": caption, **info,
            "panel_xyxy_px": [x, y, x + panel_width, y + panel_height],
            "display_xyxy_px": [x + padding + dx, y + header_height + dy,
                                x + padding + dx + display.width, y + header_height + dy + display.height]})
    path = output / "layer-review-sheet.png"
    sheet.save(path)
    metadata = {"schema_version": SHEET_SCHEMA, "sheet": str(path), "panel_order": [p["id"] for p in descriptions],
        "panels": descriptions, "canvas_dimensions": list(canvas_size), "controller_type": controller["type"],
        "initial_value": controller["initial_value"], "initial_state": state,
        "assembled_initial_image": str(assembled_path),
        "registration": {"coordinate_system": "source pixels; top-left origin; clockwise positive angles",
            "policy": "renderer prepare_assets plus compiled controller initial state",
            "sprites": {identity: {"source_path": sprite["source_path"], "source_sha256": sprite["sha256"],
                "source_centre_px": copy.deepcopy(sprite["centre_px"]), "source_size_px": copy.deepcopy(sprite["size_px"]),
                "runtime": copy.deepcopy(registered[identity])} for identity, sprite in sprites.items()}},
        "visualization_policy": "Native alpha composited over checkerboard; original RGB under zero alpha is invisible; source files unchanged",
        "source_canvas_sha256": hashlib.sha256(canvas_path.read_bytes()).hexdigest(),
        "excluded_code_overlays": "Code label cards, guides, relation arrows, cavity repairs and synthesized mechanism strokes"}
    return path, metadata


def select_review_times(clock, fps, *, max_samples=7):
    """Choose real video frames that reveal explicit motion, including closed loops.

    Initial/final frames are retained. Each moving segment contributes interior
    phase samples. Visible label and highlight intervals contribute midpoints,
    even when the controller holds still. Midpoint coverage takes priority over
    extra motion phases within the seven-frame budget. Clocks without visible
    cues use three evenly spaced frames, or fewer for very short clips.
    """
    if type(fps) is not int or fps <= 0 or type(max_samples) is not int or not 3 <= max_samples <= 7:
        raise ValueError("Review sampling needs positive integer FPS and a 3..7 frame budget")
    duration = finite(clock.get("duration_seconds"), "review duration")
    frames = clock.get("frames")
    if duration <= 0 or type(frames) is not int or frames < 1 or clock.get("fps", fps) != fps:
        raise ValueError("Review sampling needs a compiled, positive frame clock with matching FPS")
    if abs(duration - frames / fps) > 1e-6:
        raise ValueError("Review frame count and duration must agree")
    source_duration = finite(clock.get("source_duration_seconds", duration), "source review duration")
    final_frame = frames - 1
    selected = {0, final_frame}
    middle_frames, phase_frames = [], []
    for row in clock.get("scenes", []):
        for segment in row.get("segments", []):
            kind = segment.get("kind")
            if kind not in MOTION_KINDS:
                raise ValueError("Review sampling requires compiled allowlisted motion segments")
            if kind == "hold":
                continue
            start, end = finite(segment.get("start"), "motion start"), finite(segment.get("end"), "motion end")
            a, b = finite(segment.get("value_start"), "motion value_start"), finite(segment.get("value_end"), "motion value_end")
            loops = segment.get("loops", 1)
            if type(loops) is not int or not 1 <= loops <= 10000 or not 0 <= start < end <= source_duration + 1e-6:
                raise ValueError("Review motion must have bounded loops and fit the compiled clock")
            if abs(a - b) < 1e-9:
                continue
            first, last = max(0, math.floor(start * fps) + 1), min(final_frame, math.ceil(end * fps) - 1)
            if first > last:
                continue  # This subframe cue has no interior frame to inspect.

            def sample(fraction):
                return min(last, max(first, round((start + (end - start) * fraction) * fps)))

            # Sampling a whole multi-loop segment at quarters can land on three
            # identical cycle boundaries. Inspect first-cycle phases and a late
            # final-cycle phase instead.
            middle_frames.append(sample(.5 / loops))
            phase_frames.extend((sample(.25 / loops), sample((loops - .25) / loops)))
        actions = row.get("actions", [])
        if not isinstance(actions, list):
            raise ValueError("Review sampling requires compiled allowlisted actions")
        for action in actions:
            if not isinstance(action, dict) or action.get("kind") not in MOTION_KINDS | PRESENTATION_KINDS:
                raise ValueError("Review sampling requires compiled allowlisted actions")
            label = action.get("label")
            if action["kind"] not in {"highlight", "component_highlight"} and not (isinstance(label, str) and label.strip()):
                continue
            start, end = finite(action.get("start"), "visible cue start"), finite(action.get("end"), "visible cue end")
            if not 0 <= start < end <= source_duration + 1e-6:
                raise ValueError("Visible review cues must fit the compiled clock")
            first, last = max(0, math.floor(start * fps) + 1), min(final_frame, math.ceil(end * fps) - 1)
            if first <= last:
                middle_frames.append(min(last, max(first, round((start + end) * fps / 2))))
    middle_frames = sorted(set(middle_frames) - selected)
    remaining = max_samples - len(selected)
    if len(middle_frames) > remaining:
        indices = ([len(middle_frames) // 2] if remaining == 1 else
                   [round(index * (len(middle_frames) - 1) / (remaining - 1)) for index in range(remaining)])
        selected.update(middle_frames[index] for index in indices)
    else:
        selected.update(middle_frames)
        available = set(phase_frames) - selected
        while available and len(selected) < max_samples:
            # Prefer phases furthest from frames already selected, keeping the
            # initial/final and visible cue midpoint coverage intact.
            chosen = max(available, key=lambda frame: (min(abs(frame - prior) for prior in selected), -frame))
            selected.add(chosen)
            available.remove(chosen)
    if not middle_frames and not phase_frames:
        selected.add(final_frame // 2)
    return [frame / fps for frame in sorted(selected)]


def build_rendered_review_sheet(frames, samples, output_dir):
    """Return one opaque labeled sheet; frame times/values stay explicit data."""
    if (not isinstance(frames, (list, tuple)) or not isinstance(samples, (list, tuple))
            or not 1 <= len(frames) <= 7 or len(frames) != len(samples)
            or any(not isinstance(sample, dict) for sample in samples)):
        raise ValueError("Rendered review needs 1..7 frames with matching sample metadata")
    output = Path(output_dir).resolve()
    path = output / "rendered-review-sheet.png"
    source_paths = [Path(frame).resolve() for frame in frames]
    if path.resolve() in source_paths:
        raise ValueError("Review sheet must not overwrite an original frame")
    images, values = [], []
    for frame, sample in zip(source_paths, samples):
        seconds = finite(sample.get("seconds"), "review sample time")
        value = finite(sample.get("controller_value"), "review controller value")
        if seconds < 0:
            raise ValueError("Review sample time must be nonnegative")
        images.append(_rgba(frame))
        values.append((seconds, value))
    panel_width, panel_height, header_height, padding, columns = 640, 420, 90, 12, 2
    sheet = Image.new("RGB", (columns * panel_width, math.ceil(len(images) / columns) * panel_height), "#101820")
    draw = ImageDraw.Draw(sheet)
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font = ImageFont.truetype(str(font_path), 18) if font_path.is_file() else ImageFont.load_default()
    caption_font = ImageFont.truetype(str(font_path), 14) if font_path.is_file() else ImageFont.load_default()
    panels = []
    for index, (frame, sample, image, (seconds, value)) in enumerate(zip(source_paths, samples, images, values)):
        x, y = (index % columns) * panel_width, (index // columns) * panel_height
        identity = f"frame-{index + 1:02d}"
        label = f"{index + 1}. TIME {seconds:.3f}s | CONTROLLER VALUE {value:.3f}"
        draw.rectangle((x, y, x + panel_width - 1, y + panel_height - 1), outline="#526575", width=2)
        draw.text((x + padding, y + 9), label, font=font, fill="white")
        for line_index, line in enumerate(textwrap.wrap(str(sample.get("teaching", "")), width=76)[:3]):
            draw.text((x + padding, y + 35 + 17 * line_index), line, font=caption_font, fill="#C1CCD6")
        area = (panel_width - 2 * padding, panel_height - header_height - padding)
        display = ImageOps.contain(image, area, method=Image.Resampling.LANCZOS)
        tile = _checkerboard(area)
        dx, dy = (area[0] - display.width) // 2, (area[1] - display.height) // 2
        tile.alpha_composite(display, (dx, dy))
        sheet.paste(tile.convert("RGB"), (x + padding, y + header_height))
        panels.append({"id": identity, "label": label, "seconds": seconds, "controller_value": value,
            "teaching": sample.get("teaching", ""), "frame_path": str(frame),
            "frame_sha256": hashlib.sha256(frame.read_bytes()).hexdigest(),
            "panel_xyxy_px": [x, y, x + panel_width, y + panel_height],
            "display_xyxy_px": [x + padding + dx, y + header_height + dy,
                                x + padding + dx + display.width, y + header_height + dy + display.height]})
    output.mkdir(parents=True, exist_ok=True)
    sheet.save(path)
    return path, {"schema_version": "infofilm.image-animation-rendered-review-sheet.v1", "sheet": str(path),
        "panel_order": [panel["id"] for panel in panels], "panels": panels, "samples": copy.deepcopy(samples),
        "visualization_policy": "Ordered actual rendered frames, labeled with narration time and controller value; originals unchanged"}
