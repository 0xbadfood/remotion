#!/usr/bin/env python3
"""Render an image-derived circular scene using Manim's actual MoveAlongPath.

This isolated pilot consumes extracted raster layers and measured image geometry.
It does not modify production settings or ask an LLM to create keyframes.

Example:
  .venv-manim-pilot/bin/python scripts/experiment-circular-image-manim.py \
    --scene artifacts/experiments/circular-image-mggen-manim/scene.json \
    --background path/to/reconstructed-background.png \
    --ball path/to/ball-rgba.png --output-dir path/to/manim --relations

Scene geometry uses source-image pixels, top-left origin:
  {"canvas":{"width":1672,"height":941},
   "path":{"type":"circle","center_px":[836,470],"radius_px":280,
           "start_px":[1116,470],"direction":"counterclockwise"},
   "ball":{"bounding_box_xyxy_px":[1080,434,1152,506]}}
Full-canvas transparent layers need no ball bounding box; alpha bounds supply it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np
from PIL import Image
import manim
from manim import (
    Arrow, Circle, ImageMobject, Line, MoveAlongPath, ParametricFunction, Scene,
    always_redraw, linear, tempconfig,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_geometry(scene_path: Path) -> dict:
    scene = json.loads(scene_path.read_text())
    canvas, path = scene["canvas"], scene["path"]
    width, height = int(canvas["width"]), int(canvas["height"])
    if width <= 0 or height <= 0:
        raise ValueError("Canvas dimensions must be positive source-image pixels")
    centre = np.asarray(path["center_px"], dtype=float)
    radii = np.asarray(path.get("radius_xy_px", [path.get("radius_px")] * 2), dtype=float)
    start = np.asarray(path["start_px"], dtype=float)
    if centre.shape != (2,) or radii.shape != (2,) or start.shape != (2,):
        raise ValueError("Circle centre, radii and start need two coordinates")
    if not np.all(np.isfinite(np.r_[centre, radii, start])) or np.any(radii <= 0):
        raise ValueError("Geometry must contain finite positive radii")
    if path.get("type", "circle") not in {"circle", "ellipse"}:
        raise ValueError("This first pilot supports a circle or ellipse only")
    direction = path.get("direction", "counterclockwise")
    if direction not in {"counterclockwise", "clockwise"}:
        raise ValueError("Direction must be clockwise or counterclockwise")
    return {"scene": scene, "width": width, "height": height, "centre": centre,
            "radii": radii, "start": start, "direction": direction}


def prepare_ball(ball_path: Path, geometry: dict, output_dir: Path) -> tuple[Path, dict]:
    image = Image.open(ball_path).convert("RGBA")
    alpha = image.getchannel("A")
    if alpha.getextrema() == (255, 255):
        raise ValueError("The extracted ball must have transparency; an opaque crop would obscure the scene")
    alpha_box = alpha.point(lambda value: 255 if value >= 8 else 0).getbbox()
    if alpha_box is None:
        raise ValueError("Extracted ball layer is empty")
    # Retain a small antialiasing border around the useful alpha support.
    left, top, right, bottom = alpha_box
    left, top = max(0, left - 2), max(0, top - 2)
    right, bottom = min(image.width, right + 2), min(image.height, bottom + 2)
    crop_box = (left, top, right, bottom)
    crop = image.crop(crop_box)
    crop_path = output_dir / "ball-render-layer.png"
    crop.save(crop_path)
    full_canvas = image.size == (geometry["width"], geometry["height"])
    ball_spec = geometry["scene"].get("ball", {})
    bbox = ball_spec.get("bounding_box_xyxy_px")
    if full_canvas:
        source_size = [crop.width, crop.height]
        source_centre = [(left + right) / 2, (top + bottom) / 2]
    elif bbox:
        x1, y1, x2, y2 = map(float, bbox)
        if x2 <= x1 or y2 <= y1:
            raise ValueError("Ball bounding box must use [left,top,right,bottom]")
        # A cropped layer may include transparency padding. Preserve its source
        # scale rather than stretch its alpha bounds to the detection box.
        source_size = [(x2 - x1) * crop.width / image.width,
                       (y2 - y1) * crop.height / image.height]
        source_centre = [x1 + (left + right) / 2 * (x2 - x1) / image.width,
                         y1 + (top + bottom) / 2 * (y2 - y1) / image.height]
    elif ball_spec.get("size_px"):
        source_size = list(map(float, ball_spec["size_px"]))
        source_centre = list(map(float, geometry["start"]))
    else:
        raise ValueError("A cropped layer requires ball.bounding_box_xyxy_px or ball.size_px")
    return crop_path, {"input_dimensions": list(image.size), "alpha_crop_xyxy": list(crop_box),
                       "full_canvas_layer": full_canvas, "source_size_px": source_size,
                       "source_centre_px": source_centre}


def probe_video(path: Path) -> dict:
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-show_entries", "stream=width,height,avg_frame_rate,nb_frames,duration",
                           "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)["streams"][0]


def render_variant(args, geometry: dict, ball_layer: Path, ball_info: dict, relations: bool) -> dict:
    name = "ball-orbit-relations" if relations else "ball-orbit"
    output_dir = args.output_dir.resolve() / name
    output_dir.mkdir(parents=True, exist_ok=True)
    width, height = geometry["width"], geometry["height"]
    frame_height = 8.0
    frame_width = frame_height * args.width / args.height
    units_per_px = min(frame_width / width, frame_height / height)
    centre = geometry["centre"]
    radii = geometry["radii"]

    def pixel_to_point(point):
        return np.array([(float(point[0]) - width / 2) * units_per_px,
                         (height / 2 - float(point[1])) * units_per_px, 0.0])

    def point_to_pixel(point):
        return [(float(point[0]) / units_per_px + width / 2),
                (height / 2 - float(point[1]) / units_per_px)]

    centre_point = pixel_to_point(centre)
    normalized_start = (geometry["start"] - centre) / radii
    start_angle = math.atan2(-normalized_start[1], normalized_start[0])
    direction_sign = 1 if geometry["direction"] == "counterclockwise" else -1
    records = []
    final_state = {}

    class ExtractedCircularScene(Scene):
        def construct(self):
            background = ImageMobject(str(args.background.resolve()))
            background.width = width * units_per_px
            background.height = height * units_per_px
            background.move_to(np.zeros(3)).set_z_index(-5)
            self.add(background)
            ball = ImageMobject(str(ball_layer)).set_z_index(5)
            ball.width = ball_info["source_size_px"][0] * units_per_px
            ball.height = ball_info["source_size_px"][1] * units_per_px
            if abs(radii[0] - radii[1]) <= 1e-6:
                path = Circle(radius=radii[0] * units_per_px)
                path.rotate(start_angle)
                path.move_to(centre_point)
                if direction_sign < 0:
                    path.reverse_points()
            else:
                path = ParametricFunction(
                    lambda phase: centre_point + units_per_px * np.array([
                        radii[0] * math.cos(start_angle + direction_sign * phase),
                        radii[1] * math.sin(start_angle + direction_sign * phase), 0.0]),
                    t_range=[0, 2 * math.pi, 2 * math.pi / 256], use_smoothing=True)
            # This independent controller is invisible. The guide can remain
            # in the reconstructed background, or be rebuilt from the same
            # source-measured path when extraction removed its occluded pixels.
            path.set_stroke(opacity=0)
            path_spec = geometry["scene"]["path"]
            if args.draw_guide or path_spec.get("draw_guide", False):
                guide = path.copy().set_stroke(
                    color=path_spec.get("guide_colour", path_spec.get("guide_color", "#1A7FFF")),
                    # Cairo maps a Manim stroke width to 0.01 scene units.
                    # Measure its requested width in source pixels instead.
                    width=float(path_spec.get("guide_width_px", path_spec.get("guide_width_source_px", 7))) * units_per_px / 0.01,
                    opacity=1).set_z_index(0)
                self.add(guide)
            ball.move_to(path.point_from_proportion(0))
            initial = ball.get_center().copy()

            if relations:
                string = always_redraw(lambda: Line(centre_point, ball.get_center(),
                                                      color="#8C98A6", stroke_width=2).set_z_index(1))

                def inward_arrow():
                    point = ball.get_center()
                    radial = point - centre_point
                    norm = np.linalg.norm(radial)
                    return Arrow(point - radial / norm * ball.width * 0.3,
                                 point - radial / norm * min(radii) * units_per_px * 0.38,
                                 buff=0, color="#E87924", stroke_width=5,
                                 max_tip_length_to_length_ratio=0.2).set_z_index(4)

                def tangent_arrow():
                    point = ball.get_center()
                    radial = point - centre_point
                    # A circle's tangent is perpendicular to its radius. The
                    # ellipse derivative uses the measured two-axis geometry.
                    phase = math.atan2(radial[1] / (radii[1] * units_per_px),
                                       radial[0] / (radii[0] * units_per_px))
                    tangent = direction_sign * np.array([-radii[0] * math.sin(phase),
                                                          radii[1] * math.cos(phase), 0.0])
                    tangent /= np.linalg.norm(tangent)
                    return Arrow(point + tangent * ball.width * 0.35,
                                 point + tangent * min(radii) * units_per_px * 0.4,
                                 buff=0, color="#219EBC", stroke_width=5,
                                 max_tip_length_to_length_ratio=0.2).set_z_index(4)

                self.add(string, always_redraw(inward_arrow), always_redraw(tangent_arrow))
            self.add(ball)

            def record_position(mobject, dt):
                position = mobject.get_center().copy()
                records.append({"time": float(self.renderer.time),
                                "pixel_center": point_to_pixel(position)})

            ball.add_updater(record_position)
            for _ in range(args.rotations):
                self.play(MoveAlongPath(ball, path), run_time=args.duration / args.rotations,
                          rate_func=linear)
            final_state.update(initial_pixel_center=point_to_pixel(initial),
                               final_pixel_center=point_to_pixel(ball.get_center()),
                               closure_error_px=float(np.linalg.norm(ball.get_center() - initial) / units_per_px))

    started = time.monotonic()
    with tempconfig({"renderer": "cairo", "pixel_width": args.width, "pixel_height": args.height,
                     "frame_width": frame_width, "frame_height": frame_height, "frame_rate": args.fps,
                     "background_color": "#FFFFFF", "media_dir": str(output_dir / "media"),
                     "output_file": name, "write_to_movie": True, "disable_caching": True,
                     "verbosity": "WARNING"}):
        animation = ExtractedCircularScene()
        animation.render()
        rendered = Path(animation.renderer.file_writer.movie_file_path)
    video = output_dir / (name + ".mp4")
    shutil.copy2(rendered, video)
    samples = np.asarray([row["pixel_center"] for row in records], dtype=float)
    norm_radius = np.linalg.norm((samples - centre) / radii, axis=1)
    stats = {"schema_version": "infofilm.image-manim-pilot.v1", "variant": name,
             "manim_version": manim.__version__, "renderer": "cairo",
             "motion_primitive": "manim.animation.movement.MoveAlongPath",
             "relations": ["centre_to_ball_line", "inward_force_arrow", "tangent_velocity_arrow"] if relations else [],
             "path_visible": False, "source_geometry": geometry["scene"],
             "guide_reconstructed_from_measured_path": bool(args.draw_guide or geometry["scene"]["path"].get("draw_guide", False)),
             "ball_layer": ball_info, "duration_seconds": args.duration, "rotations": args.rotations,
             "video": str(video), "render_seconds": round(time.monotonic() - started, 3),
             "measured_positions": len(records), "normalized_radius_min": float(norm_radius.min()),
             "normalized_radius_max": float(norm_radius.max()),
             "max_radius_error_px": float(np.max(np.abs(norm_radius - 1)) * max(radii)),
             "initial_projection_error_px": float(np.linalg.norm(np.asarray(final_state["initial_pixel_center"]) - geometry["start"])),
             "letterbox": {"scale_units_per_source_pixel": units_per_px,
                           "output_content_size_px": [width * units_per_px / frame_width * args.width,
                                                      height * units_per_px / frame_height * args.height]},
             **final_state, "video_probe": probe_video(video),
             "inputs": {key: {"path": str(path.resolve()), "sha256": sha256(path)}
                        for key, path in {"scene": args.scene, "background": args.background, "ball": args.ball}.items()}}
    stats["checks"] = {"orbit_closed": stats["closure_error_px"] < 0.01,
                       "radius_preserved": bool(stats["max_radius_error_px"] < max(radii) * 0.001),
                       "duration_correct": abs(float(stats["video_probe"]["duration"]) - args.duration) <= 1 / args.fps,
                       "resolution_correct": (stats["video_probe"]["width"], stats["video_probe"]["height"]) == (args.width, args.height)}
    (output_dir / "positions.json").write_text(json.dumps(records, indent=2) + "\n")
    # Checkpoints correspond to an actual decoded video frame, not a separate
    # reconstruction of the expected image.
    stills = output_dir / "stills"
    stills.mkdir(exist_ok=True)
    for index, at in enumerate([0, args.duration / args.rotations / 4,
                                args.duration / args.rotations / 2,
                                3 * args.duration / args.rotations / 4,
                                args.duration - 1 / args.fps]):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at),
                        "-i", str(video), "-frames:v", "1", str(stills / f"checkpoint-{index:02d}.png")], check=True)
    (output_dir / "metrics.json").write_text(json.dumps(stats, indent=2) + "\n")
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--background", type=Path, required=True)
    parser.add_argument("--ball", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=12)
    parser.add_argument("--rotations", type=int, default=2)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--relations", action="store_true", help="Also render derived string and force/velocity arrows")
    parser.add_argument("--draw-guide", action="store_true", help="Reconstruct the source circumference from its measured path")
    args = parser.parse_args()
    if args.duration <= 0 or args.rotations <= 0 or args.fps <= 0 or args.width <= 0 or args.height <= 0:
        parser.error("Duration, rotations, fps and output dimensions must be positive")
    if abs(args.duration * args.fps - round(args.duration * args.fps)) > 1e-6:
        parser.error("Duration must own an exact integer number of frames")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    geometry = load_geometry(args.scene)
    if Image.open(args.background).size != (geometry["width"], geometry["height"]):
        parser.error("Background dimensions differ from source canvas geometry")
    ball_layer, ball_info = prepare_ball(args.ball, geometry, args.output_dir)
    results = [render_variant(args, geometry, ball_layer, ball_info, False)]
    if args.relations:
        results.append(render_variant(args, geometry, ball_layer, ball_info, True))
    summary = {"status": "passed" if all(all(row["checks"].values()) for row in results) else "needs_attention",
               "results": results}
    (args.output_dir / "render-results.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"status": summary["status"], "renders": [{"variant": row["variant"],
                     "video": row["video"], "render_seconds": row["render_seconds"],
                     "checks": row["checks"]} for row in results]}))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
