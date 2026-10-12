#!/usr/bin/env python3
"""Animate two extracted ball layers over an image using one physical fall clock.

Coordinates use source-image pixels, with their origin at the top left. The
background is a cleaned colourful illustration; the two ball images are RGBA
layers from MG-Gen. This CPU-only pilot does not change production services.

  .venv-manim-pilot/bin/python scripts/experiment-gravity-image-manim.py \
    --scene path/to/scene.json --background path/to/background.png \
    --fired-ball path/to/fired.png --dropped-ball path/to/dropped.png \
    --output-dir path/to/manim

Minimal scene contract:
  {"canvas":{"width":1672,"height":941}, "ground_y_px":790,
   "balls":{"fired":{"centre_px":[470,260],"size_px":[60,60],
                      "bbox_xyxy_px":[440,230,500,290]},
            "dropped":{"centre_px":[570,260],"size_px":[60,60],
                        "bbox_xyxy_px":[540,230,600,290]}},
   "physics":{"horizontal_range_px":800},
   "animation":{"intro_seconds":2,"flight_seconds":4,
                 "landing_hold_seconds":2,"fps":30}}

The equal-height comparison is idealised: horizontal launch, simultaneous
release, uniform gravity and no air resistance. Flight duration is a display
choice, rather than a claim about metres or physical seconds in the source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import time

import numpy as np
from PIL import Image
import manim
from manim import (
    DashedLine, Ellipse, Group, ImageMobject, Line,
    ParametricFunction, RoundedRectangle, Scene, Text, UpdateFromAlphaFunc,
    VGroup, VMobject, linear, tempconfig,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_geometry(path: Path) -> dict:
    scene = json.loads(path.read_text())
    width = int(scene["canvas"]["width"])
    height = int(scene["canvas"]["height"])
    ground = float(scene.get("ground_y_px", scene.get("physics", {}).get("ground_y_px", math.nan)))
    physics = scene.get("physics", {})
    specs = scene["balls"]
    centres = {name: np.asarray(specs[name]["centre_px"], dtype=float)
               for name in ("fired", "dropped")}
    sizes = {name: np.asarray(specs[name]["size_px"], dtype=float)
             for name in ("fired", "dropped")}
    if min(width, height) <= 0 or any(value.shape != (2,) for value in (*centres.values(), *sizes.values())):
        raise ValueError("Positive source canvas and two-coordinate ball centres/sizes are required")
    values = np.r_[ground, *centres.values(), *sizes.values()]
    if not np.all(np.isfinite(values)) or any(np.any(size <= 0) for size in sizes.values()):
        raise ValueError("Source ball geometry and ground must be finite, with positive sizes")
    radius = float(physics.get("radius_px", np.mean([size[1] for size in sizes.values()]) / 2))
    launch_y = float(physics.get("launch_y_px", np.mean([point[1] for point in centres.values()])))
    fall = ground - radius - launch_y
    horizontal_range = float(physics["horizontal_range_px"])
    if not (0 < ground <= height and radius > 0 and fall > 0 and horizontal_range > 0):
        raise ValueError("Require an elevated launch, visible ground and a positive horizontal range")
    if max(abs(point[1] - launch_y) for point in centres.values()) > height * 0.04:
        raise ValueError("Source balls differ substantially in height; redraw or explicitly correct source geometry")
    if centres["fired"][0] + horizontal_range + radius > width:
        raise ValueError("The fired ball would leave the source canvas before touching the ground")
    animation = scene.get("animation", {})
    durations = {name: float(animation.get(name, default)) for name, default in
                 (("intro_seconds", 2), ("flight_seconds", 4), ("landing_hold_seconds", 2))}
    fps = int(animation.get("fps", 30))
    if fps <= 0 or durations["flight_seconds"] <= 0 or min(durations.values()) < 0:
        raise ValueError("Require a positive flight duration/fps and nonnegative pauses")
    if durations["landing_hold_seconds"] < 1 / fps:
        raise ValueError("Hold the landed balls for at least one frame so ground contact is visible")
    if any(abs(value * fps - round(value * fps)) > 1e-6 for value in durations.values()):
        raise ValueError("Every phase duration must own an integer number of video frames")
    return {"scene": scene, "width": width, "height": height, "ground": ground,
            "centres": centres, "sizes": sizes, "radius": radius, "launch_y": launch_y,
            "fall": fall, "range": horizontal_range, "fps": fps, "durations": durations}


def prepare_sprite(path: Path, name: str, geometry: dict, directory: Path) -> tuple[Path, dict]:
    image = Image.open(path).convert("RGBA")
    alpha = image.getchannel("A")
    if alpha.getextrema() == (255, 255):
        raise ValueError(f"{name}: the moving sprite needs alpha transparency")
    bounds = alpha.point(lambda value: 255 if value >= 8 else 0).getbbox()
    if not bounds:
        raise ValueError(f"{name}: the extracted layer is empty")
    crop = image.crop(bounds)
    result = directory / f"{name}-render-layer.png"
    crop.save(result)
    # Both balls share the same rendered vertical diameter, so simultaneous
    # centre-height contact also means simultaneous visible-surface contact.
    source_height = float(geometry["sizes"][name][1])
    target_height = geometry["radius"] * 2
    target_width = target_height * crop.width / crop.height
    return result, {"source": str(path.resolve()), "sha256": sha256(path),
                    "input_dimensions": list(image.size), "alpha_crop_xyxy": list(bounds),
                    "render_size_px": [target_width, target_height],
                    "source_declared_size_px": geometry["sizes"][name].tolist(),
                    "height_scale_factor": target_height / source_height,
                    "height_normalised": abs(target_height - source_height) > 1e-6,
                    "source_launch_y_px": float(geometry["centres"][name][1]),
                    "launch_y_adjustment_px": geometry["launch_y"] - float(geometry["centres"][name][1])}


def probe_video(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                             "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames,nb_frames,duration",
                             "-of", "json", str(path)], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)["streams"][0]


def render(args, geometry: dict, sprites: dict) -> dict:
    output = args.output_dir.resolve()
    width, height = geometry["width"], geometry["height"]
    frame_height = 8.0
    frame_width = frame_height * args.width / args.height
    scale = min(frame_width / width, frame_height / height)
    intro, flight, hold = (geometry["durations"][name] for name in
                           ("intro_seconds", "flight_seconds", "landing_hold_seconds"))
    fps = geometry["fps"]
    radius = geometry["radius"]
    orange, blue = "#FFB14B", "#62D5FF"
    records = []

    def point(pixel):
        return np.array([(pixel[0] - width / 2) * scale, (height / 2 - pixel[1]) * scale, 0.0])

    def pixel(position):
        return [float(position[0]) / scale + width / 2, height / 2 - float(position[1]) / scale]

    def position(name, progress):
        x = geometry["centres"][name][0] + (geometry["range"] * progress if name == "fired" else 0)
        return point([x, geometry["launch_y"] + geometry["fall"] * progress ** 2])

    def label_card(text, font_size=23, colour="#FFFFFF", fill="#132F3E"):
        label = Text(text, font="DejaVu Sans", font_size=font_size, color=colour, weight="SEMIBOLD")
        panel = RoundedRectangle(corner_radius=0.12, width=label.width + 0.42,
                                 height=label.height + 0.28, fill_color=fill, fill_opacity=0.93,
                                 stroke_color="#FFFFFF", stroke_opacity=0.18, stroke_width=1)
        return VGroup(panel, label).set_z_index(15)

    class ExtractedGravityScene(Scene):
        def construct(self):
            background = ImageMobject(str(args.background.resolve()))
            background.width = width * scale
            background.height = height * scale
            background.move_to(np.zeros(3)).set_z_index(-10)
            self.add(background)

            headline = label_card("Different paths. Same landing time.", 27)
            headline.move_to([0, frame_height / 2 - 0.46, 0])
            conditions = label_card("Same height  ·  released together  ·  air resistance ignored", 17)
            conditions.move_to([0, frame_height / 2 - 1.04, 0])
            fired_label = label_card("Fired horizontally", 19, orange)
            dropped_label = label_card("Dropped", 19, blue)
            legend = VGroup(fired_label, dropped_label).arrange(np.array([1, 0, 0]), buff=0.18)
            legend.move_to([0, frame_height / 2 - 1.53, 0])
            self.add(headline, conditions, legend)

            balls, full_paths, trails = {}, {}, {}
            for name, colour in (("fired", orange), ("dropped", blue)):
                layer, info = sprites[name]
                ball = ImageMobject(str(layer)).set_z_index(8)
                ball.width = info["render_size_px"][0] * scale
                ball.height = info["render_size_px"][1] * scale
                ball.move_to(position(name, 0))
                balls[name] = ball
                full_paths[name] = ParametricFunction(lambda u, name=name: position(name, u),
                                                      t_range=[0, 1, 1 / 120], use_smoothing=True)
                trail = VMobject().set_stroke(colour, 3, opacity=0.78).set_z_index(3)
                trail.pointwise_become_partial(full_paths[name], 0, 0)
                trails[name] = trail
                self.add(trail, ball)

            same_height = DashedLine(position("dropped", 0), position("fired", 0),
                                     dash_length=0.075, dashed_ratio=0.45,
                                     color="#FFFFFF", stroke_width=1.8, stroke_opacity=0.7).set_z_index(2)
            self.add(same_height)
            # Ground contact markers sit at the image-derived ground surface.
            markers = VGroup()
            for name, colour in (("fired", orange), ("dropped", blue)):
                endpoint = pixel(position(name, 1))
                marker = Line(point([endpoint[0] - radius * 1.25, geometry["ground"]]),
                              point([endpoint[0] + radius * 1.25, geometry["ground"]]),
                              color=colour, stroke_width=2.2, stroke_opacity=0.8).set_z_index(2)
                markers.add(marker)
            self.add(markers)
            if intro:
                self.wait(intro)

            moving = Group(balls["fired"], balls["dropped"])

            def update_pair(mobject, progress):
                for name in ("fired", "dropped"):
                    balls[name].move_to(position(name, progress))
                    trails[name].pointwise_become_partial(full_paths[name], 0, progress)
                left, right = (balls[name].get_center() for name in ("dropped", "fired"))
                # A common-height dotted connector communicates shared vertical
                # motion. It is derived from both ball positions each frame.
                same_height.become(DashedLine(left, right, dash_length=0.075, dashed_ratio=0.45,
                                             color="#FFFFFF", stroke_width=1.8,
                                             stroke_opacity=0.48).set_z_index(2))
                records.append({"renderer_time": float(self.renderer.time), "flight_progress": float(progress),
                                "fired_center_px": pixel(balls["fired"].get_center()),
                                "dropped_center_px": pixel(balls["dropped"].get_center())})

            self.play(UpdateFromAlphaFunc(moving, update_pair), run_time=flight, rate_func=linear)
            same_height.set_opacity(0)
            impact = VGroup()
            for name, colour in (("fired", orange), ("dropped", blue)):
                endpoint = pixel(position(name, 1))
                glow = Ellipse(width=radius * scale * 3, height=radius * scale * 0.5,
                               color=colour, fill_color=colour, fill_opacity=0.26,
                               stroke_width=2.3, stroke_opacity=0.8)
                glow.move_to(point([endpoint[0], geometry["ground"] + 1])).set_z_index(5)
                impact.add(glow)
            landing_card = label_card("Both reach the ground together", 25)
            landing_card.move_to([0, -frame_height / 2 + 0.42, 0])
            # Both glows appear in the first landing frame; no artificial second
            # bounce or staggered impact changes the intended comparison.
            self.add(impact, landing_card)
            if hold:
                self.wait(hold)

    started = time.monotonic()
    with tempconfig({"renderer": "cairo", "pixel_width": args.width, "pixel_height": args.height,
                     "frame_width": frame_width, "frame_height": frame_height, "frame_rate": fps,
                     "background_color": "#182A30", "media_dir": str(output / "media"),
                     "output_file": "gravity-same-time", "write_to_movie": True,
                     "disable_caching": True, "verbosity": "WARNING"}):
        animation = ExtractedGravityScene()
        animation.render()
        rendered = Path(animation.renderer.file_writer.movie_file_path)
    render_seconds = time.monotonic() - started
    video = output / "gravity-same-time.mp4"
    shutil.copy2(rendered, video)
    probe = probe_video(video)
    fired = np.array([row["fired_center_px"] for row in records])
    dropped = np.array([row["dropped_center_px"] for row in records])
    progress = np.array([row["flight_progress"] for row in records])
    max_y_disagreement = float(np.max(np.abs(fired[:, 1] - dropped[:, 1])))
    expected_y = geometry["launch_y"] + geometry["fall"] * progress ** 2
    max_y_error = float(np.max(np.abs(fired[:, 1] - expected_y)))
    contact_y = geometry["ground"] - radius
    fired_contact = np.flatnonzero(np.abs(fired[:, 1] - contact_y) < 1e-6)
    dropped_contact = np.flatnonzero(np.abs(dropped[:, 1] - contact_y) < 1e-6)
    contact_together = bool(fired_contact.size and dropped_contact.size and fired_contact[0] == dropped_contact[0])
    contact_frame = int(round((intro + flight) * fps))
    total = intro + flight + hold
    metrics = {"schema_version": "infofilm.gravity-image-manim-pilot.v1",
               "manim_version": manim.__version__, "renderer": "cairo", "render_seconds": round(render_seconds, 3),
               "video": str(video), "video_probe": probe, "source_geometry": geometry["scene"],
               "physics": {"shared_flight_clock": True, "motion_primitive": "manim.UpdateFromAlphaFunc",
                           "vertical": "y = launch_y + fall_height * progress^2",
                           "horizontal_fired": "x = launch_x + horizontal_range * progress",
                           "horizontal_dropped": "x = constant", "air_resistance": "ignored",
                           "launch_y_px": geometry["launch_y"], "fall_height_px": geometry["fall"],
                           "ground_y_px": geometry["ground"], "ball_radius_px": radius,
                           "display_flight_seconds": flight,
                           "note": "Pixel-space illustrative gravity. Display duration is chosen for explanation; no metre scale is asserted."},
               "duration_seconds": total, "max_y_disagreement_px": max_y_disagreement,
               "max_vertical_equation_error_px": max_y_error,
               "dropped_max_horizontal_drift_px": float(np.max(np.abs(dropped[:, 0] - dropped[0, 0]))),
               "fired_horizontal_travel_px": float(fired[-1, 0] - fired[0, 0]),
               "contact_callback_same": contact_together,
               "first_visible_ground_contact_frame": contact_frame,
               "first_visible_ground_contact_seconds": contact_frame / fps,
               "final_visible_bottom_y_px": {"fired": float(fired[-1, 1] + radius),
                                             "dropped": float(dropped[-1, 1] + radius)},
               "position_callback_records": len(records),
               "measurement_note": "Computed sprite transforms, not subpixel image measurements. Frame count is independently decoded by FFprobe.",
               "sprite_layers": {name: info for name, (_, info) in sprites.items()},
               "checks": {"same_vertical_motion": max_y_disagreement < 1e-6,
                          "gravity_equation_followed": max_y_error < 1e-6,
                          "dropped_x_constant": bool(np.max(np.abs(dropped[:, 0] - dropped[0, 0])) < 1e-6),
                          "fired_x_moves": bool(abs((fired[-1, 0] - fired[0, 0]) - geometry["range"]) < 1e-6),
                          "contact_together": contact_together,
                          "both_touch_ground": bool(np.max(np.abs(np.r_[fired[-1, 1], dropped[-1, 1]] + radius - geometry["ground"])) < 1e-6),
                          "duration_correct": abs(float(probe["duration"]) - total) <= 1 / fps,
                          "resolution_correct": (probe["width"], probe["height"]) == (args.width, args.height),
                          "decoded_frame_count_correct": int(probe["nb_read_frames"]) == round(total * fps)},
               "inputs": {name: {"path": str(path.resolve()), "sha256": sha256(path)}
                          for name, path in (("scene", args.scene), ("background", args.background),
                                             ("fired_ball", args.fired_ball), ("dropped_ball", args.dropped_ball))}}
    (output / "positions.json").write_text(json.dumps(records, indent=2) + "\n")
    (output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    stills = output / "stills"
    stills.mkdir(exist_ok=True)
    checkpoint_times = [0, intro, intro + flight * 0.25, intro + flight * 0.5,
                        intro + flight * 0.75, intro + flight, total - 1 / fps]
    for index, at in enumerate(checkpoint_times):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at),
                        "-i", str(video), "-frames:v", "1", str(stills / f"checkpoint-{index:02d}.png")], check=True)
    result = {"status": "passed" if all(metrics["checks"].values()) else "needs_attention", "results": [metrics]}
    (output / "render-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "video": str(video), "render_seconds": metrics["render_seconds"],
                      "checks": metrics["checks"]}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--background", required=True, type=Path)
    parser.add_argument("--fired-ball", required=True, type=Path)
    parser.add_argument("--dropped-ball", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args()
    if min(args.width, args.height) <= 0:
        parser.error("Output dimensions must be positive")
    geometry = load_geometry(args.scene)
    if Image.open(args.background).size != (geometry["width"], geometry["height"]):
        parser.error("Clean background dimensions differ from the scene canvas")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sprites = {name: prepare_sprite(path, name, geometry, args.output_dir) for name, path in
               (("fired", args.fired_ball), ("dropped", args.dropped_ball))}
    result = render(args, geometry, sprites)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
