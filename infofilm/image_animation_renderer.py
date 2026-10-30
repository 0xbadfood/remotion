"""CPU Cairo/Manim compositor for the allowlisted image-animation contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import textwrap
import time

from PIL import Image

from .image_animation_controllers import (
    SCENE_SCHEMA, MissingControllerError, box, compile_scene, compile_timeline,
    finite, normalise_pilot_scene, normalise_pilot_timings, point, resolve_asset,
    safe_id, state_at, value_at,
)

RENDER_SCHEMA = "infofilm.image-animation-render.v1"


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def write(path: Path, value: dict | list) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_assets(scene: dict, scene_path: Path, directory: Path) -> dict:
    """Crop alpha support while preserving source size, placement and landmarks."""
    directory.mkdir(parents=True, exist_ok=True)
    width, height = int(scene["canvas"]["width"]), int(scene["canvas"]["height"])
    sprites = {}
    moving = set(scene["controller"].get("bindings", {}).values())
    for identity, spec in scene["assets"].items():
        safe_id(identity, "asset id")
        source = resolve_asset(spec["path"], scene_path.parent)
        with Image.open(source) as original:
            has_alpha = "A" in original.getbands() or "transparency" in original.info
            image = original.convert("RGBA")
        info = {**spec, **spec.get("landmarks", {}), "source_path": str(source),
            "sha256": sha256(source), "input_dimensions": list(image.size)}
        if identity == "background":
            if image.size != (width, height):
                raise ValueError("Background dimensions must match the declared source canvas")
            sprites[identity] = {**info, "path": str(source), "size_px": [width, height], "centre_px": [width/2, height/2]}
            continue
        alpha = image.getchannel("A")
        bounds = alpha.point(lambda p: 255 if p >= 8 else 0).getbbox()
        if identity in moving and (not has_alpha or alpha.getextrema() == (255, 255) or bounds is None):
            raise ValueError(f"{identity}: moving parts need a nonempty genuine transparent RGBA layer")
        if bounds is None:
            raise ValueError(f"{identity}: raster layer is empty")
        full = image.size == (width, height)
        declared = spec.get("bbox_xyxy_px", spec.get("bounding_box_xyxy_px"))
        if full:
            source_size = [bounds[2]-bounds[0], bounds[3]-bounds[1]]
            source_center = [(bounds[0]+bounds[2])/2, (bounds[1]+bounds[3])/2]
        elif declared is not None:
            x1, y1, x2, y2 = box(declared, identity + " bbox")
            source_size = [(x2-x1)*(bounds[2]-bounds[0])/image.width, (y2-y1)*(bounds[3]-bounds[1])/image.height]
            source_center = [x1+(bounds[0]+bounds[2])/2*(x2-x1)/image.width, y1+(bounds[1]+bounds[3])/2*(y2-y1)/image.height]
        elif "size_px" in spec and "centre_px" in spec:
            size = point(spec["size_px"], identity + " size")
            center = point(spec["centre_px"], identity + " center")
            source_size = [size[0]*(bounds[2]-bounds[0])/image.width, size[1]*(bounds[3]-bounds[1])/image.height]
            source_center = [center[0]+((bounds[0]+bounds[2])/2/image.width-.5)*size[0],
                center[1]+((bounds[1]+bounds[3])/2/image.height-.5)*size[1]]
        else:
            raise ValueError(f"{identity}: cropped raster needs a source bbox or centre_px and size_px")
        target = directory / f"{identity}.png"
        image.crop(bounds).save(target)
        # An explicitly normalised rig size/centre is applied to alpha support,
        # after original bbox mapping. Its provenance belongs to the scene.
        if spec.get("normalised_size_px"):
            source_size = point(spec["normalised_size_px"], identity + " normalised size")
        if spec.get("normalised_centre_px"):
            source_center = point(spec["normalised_centre_px"], identity + " normalised center")
        sprites[identity] = {**info, "path": str(target.resolve()), "size_px": source_size,
            "centre_px": source_center, "alpha_crop_xyxy": list(bounds), "full_canvas_layer": full}
    controller = scene["controller"]
    if controller["type"] == "shared_projectile_drop":
        radius = float(controller["parameters"]["radius_px"])
        for role in ("fired", "dropped"):
            sprite = sprites[controller["bindings"][role]]
            sprite["size_px"] = [sprite["size_px"][0]/sprite["size_px"][1]*2*radius, 2*radius]
    return sprites


def probe_video(path: Path) -> dict:
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=width,height,avg_frame_rate,nb_read_frames,duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    streams = json.loads(proc.stdout)["streams"]
    if len(streams) != 1:
        raise ValueError("Delivery must have one video stream")
    return streams[0]


def canvas_layout(source_width: float, source_height: float, output_width: int, output_height: int,
                  *, has_labels: bool = True) -> dict:
    """Fit the whole diagram and a separate code-label region into either canvas."""
    if min(source_width, source_height, output_width, output_height) <= 0:
        raise ValueError("Positive source and output canvas dimensions are required")
    frame_height = 8.0
    frame_width = frame_height*output_width/output_height
    margin = min(frame_width, frame_height)*.035
    if not has_labels:
        area = [-frame_width/2+margin, -frame_height/2+margin, frame_width/2-margin, frame_height/2-margin]
        labels = None
    elif output_width >= output_height:
        division = frame_width*.18
        area = [-frame_width/2+margin, -frame_height/2+margin, division-margin, frame_height/2-margin]
        labels = [division+margin, -frame_height/2+margin, frame_width/2-margin, frame_height/2-margin]
    else:
        division = -frame_height*.12
        area = [-frame_width/2+margin, division+margin, frame_width/2-margin, frame_height/2-margin]
        labels = [-frame_width/2+margin, -frame_height/2+margin, frame_width/2-margin, division-margin]
    scale = min((area[2]-area[0])/source_width, (area[3]-area[1])/source_height)
    center = [(area[0]+area[2])/2, (area[1]+area[3])/2]
    fitted = [center[0]-source_width*scale/2, center[1]-source_height*scale/2,
        center[0]+source_width*scale/2, center[1]+source_height*scale/2]
    return {"frame_width": frame_width, "frame_height": frame_height, "scale": scale,
        "diagram_center": center, "diagram_bounds": fitted, "label_bounds": labels,
        "checks": {"diagram_inside_output": fitted[0] >= -frame_width/2 and fitted[2] <= frame_width/2 and
            fitted[1] >= -frame_height/2 and fitted[3] <= frame_height/2,
            "diagram_labels_separate": labels is None or fitted[2] <= labels[0] or fitted[1] >= labels[3]}}


def _validate_rig_positions(scene: dict, controller: dict, sprites: dict) -> None:
    """Check actual alpha-support transforms as well as analytic anchor geometry."""
    width, height = controller["canvas"]["width"], controller["canvas"]["height"]
    extent = 720 if controller["type"] == "ideal_slider_crank_four_stroke" else 360 if controller["type"] == "orbit_path" else 1
    for index in range(145):
        state = state_at(controller, extent*index/144)
        for role, xy in state.get("positions_px", {}).items():
            sprite = sprites[controller["bindings"][role]]
            if xy[0]-sprite["size_px"][0]/2 < -1e-6 or xy[0]+sprite["size_px"][0]/2 > width+1e-6 or xy[1]-sprite["size_px"][1]/2 < -1e-6 or xy[1]+sprite["size_px"][1]/2 > height+1e-6:
                raise ValueError(f"{role}: moving alpha support leaves the source canvas")
        for role, offset in state.get("offsets_px", {}).items():
            sprite = sprites[controller["bindings"][role]]
            xy = [sprite["centre_px"][0]+offset[0], sprite["centre_px"][1]+offset[1]]
            if xy[0]-sprite["size_px"][0]/2 < 0 or xy[0]+sprite["size_px"][0]/2 > width or xy[1]-sprite["size_px"][1]/2 < 0 or xy[1]+sprite["size_px"][1]/2 > height:
                raise ValueError(f"{role}: moving alpha support leaves the source canvas")


def render_clip(scene: dict, controller: dict, row: dict, sprites: dict, directory: Path,
                *, width: int, height: int, fps: int, fingerprint: str, preview: bool) -> dict:
    import manim
    import numpy as np
    from manim import (Arrow, Circle, Group, ImageMobject, Line, ParametricFunction,
        Rectangle, RoundedRectangle, Scene, Text, UpdateFromAlphaFunc, VGroup, linear, tempconfig)

    directory.mkdir(parents=True, exist_ok=True)
    video, metrics = directory / "clip.mp4", directory / "metrics.json"
    if video.exists() and metrics.exists():
        prior = read(metrics)
        if prior.get("input_fingerprint") == fingerprint and all(prior.get("checks", {"missing": False}).values()) and prior.get("video_sha256") == sha256(video):
            print(json.dumps({"scene": row["id"], "state": "cached", "frames": row["frames"]}), flush=True)
            return prior
    source_width, source_height = controller["canvas"]["width"], controller["canvas"]["height"]
    labels = row.get("display_labels", [])
    labels = [item.get("text", item.get("label", "")) if isinstance(item, dict) else str(item) for item in labels]
    has_labels = bool(row.get("title") or labels or any(a.get("label") for a in row.get("actions", [])))
    layout = canvas_layout(source_width, source_height, width, height, has_labels=has_labels)
    scale = layout["scale"]
    parameters, bindings = controller["parameters"], controller["bindings"]
    records = []

    def to_point(xy):
        return np.array([layout["diagram_center"][0]+(xy[0]-source_width/2)*scale,
            layout["diagram_center"][1]+(source_height/2-xy[1])*scale, 0])

    def label_card(text: str, card_width: float, max_height: float, font_size: int = 22):
        wrapped = "\n".join(textwrap.wrap(str(text), width=max(14, round(card_width*11)), break_long_words=True))
        label = Text(wrapped or " ", font="DejaVu Sans", font_size=font_size, color="#E7F0F7")
        ratio = min(1, (card_width-.24)/max(.01, label.width), (max_height-.16)/max(.01, label.height))
        label.scale(max(.001, ratio))
        panel = RoundedRectangle(corner_radius=.08, width=card_width, height=label.height+.16,
            fill_color="#173143", fill_opacity=.97, stroke_color="#78A2BB", stroke_opacity=.35, stroke_width=1)
        return VGroup(panel, label).set_z_index(100)

    class ImageAnimationScene(Scene):
        def construct(self):
            objects, templates = {}, {}
            for identity, sprite in sprites.items():
                image = ImageMobject(sprite["path"]).set_z_index(float(sprite.get("z_index", -20 if identity == "background" else 5)))
                image.width, image.height = sprite["size_px"][0]*scale, sprite["size_px"][1]*scale
                image.move_to(to_point(sprite["centre_px"]))
                self.add(image)
                objects[identity], templates[identity] = image, image.copy()
            for cavity in scene.get("cavity_masks", []):
                if cavity.get("type") == "rectangle":
                    x1, y1, x2, y2 = box(cavity.get("bbox_xyxy_px"), "cavity bbox")
                    shape = Rectangle(width=(x2-x1)*scale, height=(y2-y1)*scale).move_to(to_point([(x1+x2)/2, (y1+y2)/2]))
                elif cavity.get("type") == "circle":
                    radius = finite(cavity.get("radius_px"), "cavity radius")
                    if radius <= 0:
                        raise ValueError("Cavity radius must be positive")
                    shape = Circle(radius=radius*scale).move_to(to_point(point(cavity.get("center_px"), "cavity center")))
                else:
                    raise ValueError("Only rectangle/circle cavity masks are supported")
                shape.set_fill(cavity.get("colour", "#DDDAD1"), 1).set_stroke(opacity=0).set_z_index(-15)
                self.add(shape)
            cue_cards = []
            if has_labels:
                left, bottom, right, top = layout["label_bounds"]
                card_width = right-left
                title = label_card(row.get("title", row["id"]), card_width, .9, 26)
                title.move_to([(left+right)/2, top-title.height/2, 0]); self.add(title)
                cue_height = min(.95, (top-bottom)*.3)
                cue_y = bottom+cue_height/2
                usable = max(.15, top-bottom-title.height-cue_height-.28)
                cards = [label_card(text, card_width, usable/max(1, len(labels))) for text in labels]
                if cards:
                    group = VGroup(*cards).arrange(np.array([0, -1, 0]), buff=.08)
                    if group.height > usable:
                        group.scale(usable/group.height)
                    group.move_to([(left+right)/2, (bottom+cue_height+top-title.height-.2)/2, 0]); self.add(group)
                for action in row.get("actions", []):
                    if action.get("label"):
                        card = label_card(action["label"], card_width, cue_height, 19)
                        card.move_to([(left+right)/2, cue_y, 0]).set_opacity(0)
                        self.add(card); cue_cards.append((action, card))
                if preview:
                    card = label_card("INCOMPLETE PREVIEW", min(card_width, 3), .4, 15)
                    card.move_to([0, -layout["frame_height"]/2+card.height/2+.04, 0]); self.add(card)
            overlays, relation_objects = [], []
            rod = None
            if controller["type"] == "ideal_slider_crank_four_stroke":
                if "connecting_rod" not in bindings:
                    rod = Line(to_point(state_at(controller, row["initial_value"])["wrist_pin_px"]),
                        to_point(state_at(controller, row["initial_value"])["crank_pin_px"]), color="#AAC2CE", stroke_width=3).set_z_index(7)
                    self.add(rod); overlays.append(rod)
                spark_location = parameters.get("spark_tip_px")
                if spark_location:
                    spark = Circle(radius=10*scale, fill_color="#FFF07B", fill_opacity=1, stroke_width=0).move_to(to_point(spark_location)).set_z_index(15)
                    self.add(spark); overlays.append(spark)
                else:
                    spark = None
            else:
                spark = None
            if controller["type"] == "orbit_path":
                if parameters.get("draw_guide"):
                    guide = ParametricFunction(lambda t: to_point(state_at(controller, t)["positions_px"]["orbiter"]),
                        t_range=[0, 360, 360/256], use_smoothing=True).set_stroke(parameters.get("guide_colour", "#1D7FFD"), 2).set_z_index(0)
                    self.add(guide)
                if parameters.get("show_relations"):
                    position = to_point(state_at(controller, row["initial_value"])["positions_px"]["orbiter"])
                    relation_objects = [Line(to_point(parameters["center_px"]), position, color="#9BA9B5"),
                        Arrow(position, to_point(parameters["center_px"]), buff=0, color="#FFAE4B"),
                        Arrow(position, position+[.1, 0, 0], buff=0, color="#5CCDDD")]
                    self.add(*relation_objects); overlays.extend(relation_objects)
            moving = Group(*objects.values(), *overlays)
            last_record = -1

            def update(_group, alpha):
                nonlocal last_record
                now = row["start"]+alpha*(row["end"]-row["start"])
                value = value_at(now, row["segments"], row["initial_value"])
                state = state_at(controller, value)
                for role, xy in state.get("positions_px", {}).items():
                    objects[bindings[role]].move_to(to_point(xy))
                for role, offset in state.get("offsets_px", {}).items():
                    sprite = sprites[bindings[role]]
                    objects[bindings[role]].move_to(to_point([sprite["centre_px"][0]+offset[0], sprite["centre_px"][1]+offset[1]]))
                for role, angle in state.get("rotation_rad", {}).items():
                    identity = bindings[role]
                    anchor = sprites[identity].get("rotation_anchor_px", parameters.get("crank_center_px"))
                    objects[identity].become(templates[identity].copy().rotate(-angle, about_point=to_point(anchor)))
                if controller["type"] == "ideal_slider_crank_four_stroke":
                    a, b = to_point(state["wrist_pin_px"]), to_point(state["crank_pin_px"])
                    if rod is not None:
                        rod.put_start_and_end_on(a, b)
                    if "connecting_rod" in bindings:
                        identity = bindings["connecting_rod"]
                        source_a, source_b = to_point(sprites[identity]["endpoint_a_px"]), to_point(sprites[identity]["endpoint_b_px"])
                        source_vector, target_vector = source_b-source_a, b-a
                        ratio = np.linalg.norm(target_vector)/np.linalg.norm(source_vector)
                        angle = math.atan2(target_vector[1], target_vector[0])-math.atan2(source_vector[1], source_vector[0])
                        objects[identity].become(templates[identity].copy().scale(ratio, about_point=source_a).rotate(angle, about_point=source_a).shift(a-source_a))
                    if spark is not None:
                        explicit = [action for action in row.get("actions", []) if action["kind"] in {"spark", "ignition"}]
                        visible = any(a["start"] <= now <= a["end"] for a in explicit) if explicit else state["spark"]
                        spark.set_opacity(1 if visible else 0)
                if relation_objects:
                    pos = to_point(state["positions_px"]["orbiter"])
                    center = to_point(state["center_px"])
                    radial = center-pos
                    tangent = np.array([state["tangent_px"][0], -state["tangent_px"][1], 0.0])
                    tangent /= np.linalg.norm(tangent)
                    relation_objects[0].put_start_and_end_on(center, pos)
                    relation_objects[1].put_start_and_end_on(pos, pos+radial*.38)
                    relation_objects[2].put_start_and_end_on(pos, pos+tangent*min(parameters["radius_xy_px"])*scale*.35)
                active = [(action, card) for action, card in cue_cards if action["start"] <= now <= action["end"]]
                chosen = min(active, key=lambda pair: pair[0]["end"]-pair[0]["start"])[0]["id"] if active else None
                for action, card in cue_cards:
                    card.set_opacity(1 if action["id"] == chosen else 0)
                sample = round(alpha*row["render_duration"]*2)
                if sample != last_record:
                    records.append({"narration_time_seconds": now, **state})
                    last_record = sample

            update(moving, 0)
            self.play(UpdateFromAlphaFunc(moving, update), run_time=row["render_duration"]-1e-8, rate_func=linear)

    started = time.monotonic()
    with tempconfig({"renderer": "cairo", "pixel_width": width, "pixel_height": height,
        "frame_width": layout["frame_width"], "frame_height": layout["frame_height"], "frame_rate": fps,
        "background_color": "#0B202D", "media_dir": str(directory / "media"), "output_file": "clip",
        "write_to_movie": True, "disable_caching": True, "verbosity": "WARNING"}):
        animation = ImageAnimationScene(); animation.render()
        shutil.copy2(Path(animation.renderer.file_writer.movie_file_path), video)
    probe = probe_video(video)
    checks = {"frame_count_correct": int(probe["nb_read_frames"]) == row["frames"],
        "resolution_correct": (int(probe["width"]), int(probe["height"])) == (width, height),
        "duration_correct": abs(float(probe["duration"])-row["render_duration"]) < 1/fps+.001,
        **layout["checks"], **controller["geometry_checks"]}
    result = {"schema_version": RENDER_SCHEMA, "scene_id": row["id"], "controller": controller["type"],
        "input_fingerprint": fingerprint, "video": str(video.resolve()), "video_sha256": sha256(video),
        "frames": row["frames"], "duration_seconds": row["render_duration"], "elapsed_seconds": time.monotonic()-started,
        "renderer": "cairo", "manim_version": manim.__version__, "probe": probe, "checks": checks,
        "layout": layout, "preview_incomplete": preview}
    write(directory / "positions.json", records); write(metrics, result)
    if not all(checks.values()):
        raise RuntimeError(f"Scene {row['id']} render verification failed: {checks}")
    print(json.dumps({"scene": row["id"], "state": "rendered", "frames": row["frames"], "checks": checks}), flush=True)
    return result


def stitch(results: list[dict], output: Path, audio: Path | None, *, fps: int,
           source_duration: float) -> dict:
    listing = output / "concat.txt"
    listing.write_text("".join("file '"+str(Path(item["video"]).resolve()).replace("'", "'\\''")+"'\n" for item in results))
    silent = output / "animation-silent.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-movflags", "+faststart", str(silent)], check=True)
    probe = probe_video(silent)
    delivery = {"silent_video": str(silent.resolve()), "video": str(silent.resolve()), "probe": probe,
        "checks": {"all_scene_frames_present": int(probe["nb_read_frames"]) == sum(item["frames"] for item in results)}}
    if audio is not None:
        streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(audio)], capture_output=True, text=True, check=True).stdout)
        audio_duration = float(streams["format"]["duration"])
        if abs(audio_duration-source_duration) > max(1/fps+.01, .05):
            raise ValueError("Narration audio duration differs from the complete MFA timeline")
        narrated = output / "animation-narrated.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(silent), "-i", str(audio), "-map", "0:v:0",
            "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-af", "apad",
            "-shortest", "-movflags", "+faststart", str(narrated)], check=True)
        all_streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(narrated)],
            capture_output=True, text=True, check=True).stdout)["streams"]
        audio_streams = [s for s in all_streams if s.get("codec_type") == "audio"]
        delivery.update(video=str(narrated.resolve()), source_audio=str(audio), source_audio_sha256=sha256(audio),
            source_audio_reused=True, delivery_audio="AAC 192 kbps; original source audio unchanged")
        delivery["checks"].update(audio_stream_present=len(audio_streams) == 1,
            source_audio_complete=bool(audio_streams) and abs(float(audio_streams[0].get("duration", 0))-audio_duration) <= 2/fps+.02)
    return delivery


def render_project(scene_path: Path | str, timings_path: Path | str, output_dir: Path | str,
                   *, width: int = 1280, height: int = 720, fps: int = 30,
                   audio: Path | str | None = None, narration_input: Path | str | None = None,
                   only_scene: list[str] | None = None, check_only: bool = False,
                   allow_incomplete_preview: bool = False) -> dict:
    scene_path, timings_path, output = Path(scene_path).resolve(), Path(timings_path).resolve(), Path(output_dir).resolve()
    if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (width, height, fps)) or width % 2 or height % 2:
        raise ValueError("Output width/height must be positive even integers, with positive integer FPS")
    scene, timings = read(scene_path), read(timings_path)
    if scene.get("schema_version") != SCENE_SCHEMA:
        if "mechanism" in scene:
            scene = normalise_pilot_scene(scene)
        else:
            raise ValueError("Use the explicit pilot adapter to supply background/layer paths before rendering")
    controller = compile_scene(scene, allow_incomplete_preview=allow_incomplete_preview)
    if timings.get("schema_version") == "engine-narration-scenes.v1":
        timings = normalise_pilot_timings(timings, controller)
    timeline = compile_timeline(timings, controller, fps=fps)
    output.mkdir(parents=True, exist_ok=True)
    if controller["preview_incomplete"] and not only_scene and not check_only:
        raise ValueError("Incomplete preview must select individual clips; it cannot create a final delivery")
    sprites = prepare_assets(scene, scene_path, output / "prepared-assets")
    _validate_rig_positions(scene, controller, sprites)
    narration_bundle = read(Path(narration_input).resolve()) if narration_input else None
    if narration_bundle:
        expected_duration = narration_bundle.get("duration_seconds")
        if expected_duration is not None and abs(float(expected_duration)-timeline["source_duration_seconds"]) > 1e-6:
            raise ValueError("Narration attachment duration differs from the MFA scene manifest")
    audio_value = audio or timings.get("audio_path") or timings.get("narration_audio") or (narration_bundle or {}).get("audio_path")
    audio_base = Path(narration_input).resolve().parent if narration_input and not (audio or timings.get("audio_path") or timings.get("narration_audio")) else timings_path.parent
    audio_path = resolve_asset(audio_value, audio_base) if audio_value else None
    expected_audio_sha = timings.get("audio_sha256") or (narration_bundle or {}).get("audio_sha256")
    if expected_audio_sha and (not audio_path or sha256(audio_path) != expected_audio_sha):
        raise ValueError("Original narration audio differs from the frozen MFA audio SHA")
    audio_checks = {}
    if audio_path is not None:
        audio_probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(audio_path)], capture_output=True, text=True, check=True).stdout)
        if abs(float(audio_probe["format"]["duration"])-timeline["source_duration_seconds"]) > max(1/fps+.01, .05):
            raise ValueError("Narration audio duration differs from the complete MFA timeline")
        audio_checks["source_audio_matches_complete_clock"] = True
    preflight = {"schema_version": RENDER_SCHEMA, "status": "preview_incomplete" if controller["preview_incomplete"] else "ready",
        "renderer": "cairo", "controller": controller, "timeline": timeline, "provenance": scene.get("provenance", {}),
        "incomplete_flags": scene.get("incomplete_flags", []), "assets": {k: {"sha256": v["sha256"], "source_path": v["source_path"],
            "size_px": v["size_px"], "centre_px": v["centre_px"]} for k, v in sprites.items()},
        "source_audio": str(audio_path) if audio_path else None, "checks": {**controller["geometry_checks"], **timeline["checks"], **audio_checks}}
    write(output / "preflight.json", preflight)
    if check_only:
        return preflight
    selected = timeline["scenes"]
    if only_scene:
        if set(only_scene)-{r["id"] for r in selected}:
            raise ValueError("Requested scene IDs are absent")
        selected = [r for r in selected if r["id"] in only_scene]
    code_hashes = {p.name: sha256(p) for p in (Path(__file__), Path(__file__).with_name("image_animation_controllers.py"))}
    # Renderer versions matter even when contract and assets are unchanged.
    import manim
    base_fingerprint = {"scene": scene, "controller": controller, "assets": {k: s["sha256"] for k, s in sprites.items()},
        "code": code_hashes, "manim_version": manim.__version__, "quality": [width, height, fps]}
    results = []
    for row in selected:
        fingerprint = hashlib.sha256(json.dumps({**base_fingerprint, "row": row}, sort_keys=True).encode()).hexdigest()
        results.append(render_clip(scene, controller, row, sprites, output / "clips" / row["id"],
            width=width, height=height, fps=fps, fingerprint=fingerprint, preview=controller["preview_incomplete"]))
    result = {"schema_version": RENDER_SCHEMA, "status": "partial" if only_scene else "passed", "scenes": results,
        "checks": preflight["checks"], "provenance": preflight["provenance"]}
    if not only_scene:
        result["delivery"] = stitch(results, output, audio_path, fps=fps, source_duration=timeline["source_duration_seconds"])
        if not all(result["delivery"]["checks"].values()):
            result["status"] = "failed"
    write(output / ("partial-render-results.json" if only_scene else "render-results.json"), result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", required=True, type=Path)
    parser.add_argument("--timings", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--narration-input", type=Path, help="Existing verified narration/MFA attachment manifest")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--only-scene", action="append")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--allow-incomplete-preview", action="store_true", help="Permit unresolved flags for selected preview clips only")
    args = parser.parse_args()
    try:
        result = render_project(args.scene, args.timings, args.output_dir, width=args.width, height=args.height,
            fps=args.fps, audio=args.audio, narration_input=args.narration_input, only_scene=args.only_scene,
            check_only=args.check_only, allow_incomplete_preview=args.allow_incomplete_preview)
    except MissingControllerError as error:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write(args.output_dir / "missing-controller-request.json", error.request)
        print(json.dumps(error.request), flush=True)
        return 2
    print(json.dumps(result), flush=True)
    return 1 if result["status"] == "failed" else 0
