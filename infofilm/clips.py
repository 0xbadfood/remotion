"""Video clips from accepted scene images (animation pass 3; user design and approval, 2026-10-16).

After the images (and their depth maps) are accepted: one planner call writes every motion prompt (`motion.py`), the
video node renders a short LTX 2.5 clip per image with the still fixed at the middle frame and RIFE in-between frames,
and the renderer plays each clip slowed down to fill its scene. Nobody speaks on screen, so the slow motion is fine;
the viewer only sees an illustration come alive, so there is no prompt-adherence review (user, 2026-10-16). Code
catches only real breakage (a clip that will not decode, is blank, or whose middle no longer looks like the still);
those scenes, like any the video node does not return, keep the depth camera. Clips never stop a film.

The video node today is the amdserver's `video` mode: the stage switches it from image mode and back (the film queue
runs one film at a time, so nothing else needs the image factory meanwhile). A dedicated sibling node later needs only
`clips.mode_switch` removed and `clips.url` pointed at it. Short films only for now: long-film chapters render in
parallel, so their clips need one film-level stage.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import httpx

from .core import digest, read, write

PROTOCOL = "infofilm.scene-clips.v1"
DEFAULTS = {"enabled": False, "url": "http://10.8.0.8:8011", "mode_switch": "http://10.8.0.8:8099", "width": 768,
            "height": 448, "frames": 121, "frame_rate": 24, "middle_frame": 57, "strength": .7, "interpolate": 2,
            "seed": 11, "timeout_seconds": 900, "switch_timeout_seconds": 1500,
            # Middle frame vs the still (mean grey difference at 64x36): Italy pilot clips 3-7, a wrong scene 56.
            "max_middle_difference": 20}


def settings(config):
    return {**DEFAULTS, **(config.get("clips") or {})}


def manifest_path(store):
    return Path(store.root) / "clips" / "manifest.json"


def current(store, images):
    """{asset_id: {file, sha256, seconds}} for clips made from exactly these accepted images."""
    path = manifest_path(store)
    if not path.exists():
        return {}
    out = {}
    for asset_id, value in read(path).get("clips", {}).items():
        image = images.get(asset_id)
        if (image and value.get("source_sha256") == image.get("sha256") and Path(value["file"]).is_file()
                and digest(Path(value["file"]).read_bytes()) == value["sha256"]):
            out[asset_id] = value
    return out


def switch_mode(url, mode, timeout):
    """Ask the amdserver mode switch for `mode` and wait until it is ready."""
    with httpx.Client(timeout=30) as client:
        if client.get(url + "/status").json().get("mode") == mode:
            return
        client.post(f"{url}/mode/{mode}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = client.get(url + "/status").json()
            if state.get("mode") == mode and state.get("phase") == "ready":
                return
            if state.get("phase") == "failed":
                raise RuntimeError(f"Mode switch to {mode} failed: {state.get('detail')}")
            time.sleep(10)
    raise RuntimeError(f"Mode switch to {mode} not ready after {timeout // 60} min")


def probe_seconds(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                          str(path)], text=True).strip())


def breakage(clip, still, limit):
    """None if the clip decodes, is not blank and its middle frame still looks like the accepted image; else why not."""
    from PIL import Image, ImageChops, ImageStat
    try:
        seconds = probe_seconds(clip)
        frame = clip.with_suffix(".middle.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{seconds / 2:.3f}", "-i", str(clip), "-frames:v", "1",
                        str(frame)], check=True, timeout=120)
        a = Image.open(frame).convert("L").resize((64, 36))
        b = Image.open(still).convert("L").resize((64, 36))
    except Exception as exc:                          # noqa: BLE001 - any decode problem is breakage
        return f"does not decode: {type(exc).__name__}: {exc}"[:200]
    low, high = a.getextrema()
    if high - low < 12:
        return "blank frame"
    difference = ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
    return f"middle frame differs from the still ({difference:.0f})" if difference > limit else None


def render_clip(config, still, motion, root, key):
    """One clip from the video API (submit, poll, download). Returns the file."""
    s = settings(config)
    target = root / f"{key}.mp4"
    if target.exists():
        return target
    with httpx.Client(timeout=60) as client, open(still, "rb") as image:
        job = client.post(s["url"] + "/v1/videos/generations", files={"conditioning_image": image}, data={
            "mode": "conditioning_image_to_video", "prompt": motion, "width": s["width"], "height": s["height"],
            "num_frames": s["frames"], "frame_rate": s["frame_rate"], "seed": s["seed"],
            "conditioning_frame_index": s["middle_frame"], "condition_strength": s["strength"],
            "interpolate": s["interpolate"], "idempotency_key": f"clip-{key}-{int(time.time())}"}).raise_for_status().json()
        token = job.get("token") or job.get("poll_token")
        deadline = time.monotonic() + s["timeout_seconds"]
        while time.monotonic() < deadline:
            state = client.get(f"{s['url']}/v1/videos/{job['id']}", params={"token": token}).json()
            if state["status"] in ("succeeded", "completed"):
                data = client.get(f"{s['url']}/v1/videos/{job['id']}/content", params={"token": token}, timeout=300)
                target.write_bytes(data.raise_for_status().content)
                return target
            if state["status"] in ("failed", "error", "cancelled"):
                raise RuntimeError(f"clip job {state['status']}: {str(state.get('error'))[:200]}")
            time.sleep(3)
    raise TimeoutError("clip job did not finish in time")


def scene_inputs(images, prompts):
    by_id = {a["id"]: a for a in prompts.get("assets", [])}
    rows = []
    for asset_id, image in images.items():
        prompt = image.get("prompt") or by_id.get(asset_id, {}).get("prompt", "")
        rows.append({"id": asset_id, "visual_message": image.get("visual_message") or by_id.get(asset_id, {}).get("visual_message"),
                     "scene": prompt.split("Scene:", 1)[-1].strip()[:900],
                     "people": [p.get("name") for p in image.get("people_in_frame") or by_id.get(asset_id, {}).get("people_in_frame") or []]})
    return rows


def make_clips(config, store, models, images, prompts):
    """Clips for every accepted image of a short film; returns what exists. Advisory: never raises."""
    s = settings(config)
    if not s["enabled"] or not images:
        return {}
    have = current(store, images)
    missing = {k: v for k, v in images.items() if k not in have}
    if not missing:
        return have
    root = Path(store.root) / "clips"
    root.mkdir(parents=True, exist_ok=True)
    switched = False
    try:
        store.event("clips", "running", {"clips": len(missing), "video_node": s["url"]})
        motion_path = root / "motion.json"
        motion = read(motion_path) if motion_path.exists() else {}
        if set(missing) - set(motion):
            from .motion import make_motion_prompts
            motion.update(make_motion_prompts(models, scene_inputs(missing, prompts)))
            write(motion_path, motion)
        if s.get("mode_switch"):
            store.event("clips", "switching", {"mode": "video"})
            switch_mode(s["mode_switch"], "video", s["switch_timeout_seconds"])
            switched = True
        saved = read(manifest_path(store)).get("clips", {}) if manifest_path(store).exists() else {}
        for n, (asset_id, image) in enumerate(missing.items(), 1):
            key = digest({"protocol": PROTOCOL, "image": image["sha256"], "motion": motion[asset_id],
                          "settings": {k: s[k] for k in ("width", "height", "frames", "middle_frame", "strength",
                                                         "interpolate", "seed")}})[:20]
            try:
                clip = render_clip(config, image["file"], motion[asset_id], root, key)
                problem = breakage(clip, image["file"], s["max_middle_difference"])
                if problem:
                    store.event("clips", "fallback", {"asset": asset_id, "reason": problem})
                    continue
                saved[asset_id] = {"file": str(clip), "sha256": digest(clip.read_bytes()), "seconds": probe_seconds(clip),
                                   "source_sha256": image["sha256"], "motion": motion[asset_id], "protocol": PROTOCOL}
                write(manifest_path(store), {"protocol": PROTOCOL, "clips": saved})
                store.event("clips", "progress", {"asset": asset_id, "done": n, "total": len(missing)})
            except Exception as exc:                  # noqa: BLE001 - this scene keeps the depth camera
                store.event("clips", "fallback", {"asset": asset_id, "reason": f"{type(exc).__name__}: {exc}"[:300]})
        have = current(store, images)
        store.event("clips", "succeeded", {"clips": len(have), "of": len(images)})
    except Exception as exc:                          # noqa: BLE001 - advisory: the film keeps its stills
        store.event("clips", "failed", {"error": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        if switched:
            try:
                store.event("clips", "switching", {"mode": "image"})
                switch_mode(s["mode_switch"], "image", s["switch_timeout_seconds"])
            except Exception as exc:                  # noqa: BLE001 - reported; the next image stage will wait
                store.event("clips", "switch_back_failed", {"error": str(exc)[:300]})
    return current(store, images)
