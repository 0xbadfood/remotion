"""Motion beats (key-idea films, user 2026-10-20): an image beat with an `action` becomes a short LTX video that starts
from the accepted still and shows the action, as long as its beat (3-10 s), played at about normal speed.

Unlike the illustration clips (clips.py: gentle motion slowed to fill a scene), these clips carry part of the
explanation: the guide climbing down two steps is the idea. They are planned with the scene, rendered from the
still's first frame (tested 2026-10-20 on the Spark: the character stays the same and does the action; 8 s in about
4 minutes, 10 s in about 5; the amdserver, warm, about 11 s per second of clip), and noted on the scene (action_file,
action_sha256, action_seconds). No review for now: the user judges the first pilots and the review strategy follows
from that. A clip that fails to render leaves its scene as the still with the depth camera; clips never stop a film.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from .core import digest

PROTOCOL = "infofilm.action-clips.v1"
# The amdserver makes the clips (video_pool), switched to video mode once the stills are accepted (96 GB since
# 2026-10-20: the user cleared mode switches during films). Measured on I4 v2, warm: about 0.47 s per frame there
# against 1.2 s on the Spark, so the user chose the amdserver alone; the Spark entry can be added back by config.
DEFAULTS = {"width": 768, "height": 448, "frame_rate": 24, "condition_strength": .9, "seed": 11,
            "timeout_seconds": 1500, "switch_timeout_seconds": 1500,
            "style": "Flat 2D cartoon animation, steady camera, plain uncluttered background, no text.",
            "workers": [{"id": "amdserver", "kind": "ltx_comfy", "url": "http://10.8.0.8:8011",
                         "mode_switch": "http://10.8.0.8:8099"}]}
LONGEST_FRAMES = 241            # 10 s at 24 fps; LTX needs (frames - 1) % 8 == 0


def settings(config):
    return {**DEFAULTS, **(config.get("action_clips") or {})}


def frames_for(seconds, rate):
    """Frames for a clip at least as long as the beat (LTX: 8k + 1), at most LONGEST_FRAMES."""
    return min(LONGEST_FRAMES, max(9, math.ceil((seconds * rate - 1) / 8) * 8 + 1))


def prompt_for(scene, s):
    return f"{s['style']} {scene['action']}"


def make_action_clips(config, store, films, images, run=None):
    """A clip for every action beat whose still was accepted; notes it on the scene. Never raises."""
    from .creative import resolved_scenes
    s = settings(config)
    jobs = []
    for film in films:
        scenes = resolved_scenes(film["storyboard"], film["script"], film["timing"])
        for scene, resolved in zip(film["storyboard"]["scenes"], scenes):
            if scene.get("action") and scene.get("asset_id") in images:
                jobs.append((scene, resolved["end"] - resolved["start"]))
    if not jobs:
        return
    root = Path(store.root) / "action-clips"
    root.mkdir(parents=True, exist_ok=True)
    store.event("action-clips", "running", {"clips": len(jobs)})
    todo = []
    for scene, seconds in jobs:
        image = images[scene["asset_id"]]
        frames = frames_for(seconds, s["frame_rate"])
        key = digest({"protocol": PROTOCOL, "image": image["sha256"], "prompt": prompt_for(scene, s), "frames": frames,
                      "settings": {k: s[k] for k in ("width", "height", "frame_rate", "condition_strength", "seed")}})[:20]
        todo.append((scene, image, frames, key, root / f"{key}.mp4"))
    missing = {key: (scene, image, frames, target) for scene, image, frames, key, target in todo if not target.exists()}
    if missing:
        from . import video_pool

        def render_one(worker, key, payload):
            scene, image, frames, target = payload
            started = time.monotonic()
            try:
                worker.render_action(image["file"], prompt_for(scene, s), frames, target, key, s)
            except Exception:
                target.unlink(missing_ok=True)
                raise
            store.event("action-clips", "rendered", {"scene": scene["id"], "worker": worker.id, "frames": frames,
                                                     "seconds": round(time.monotonic() - started, 1)})
        try:
            (run or video_pool.run)(s, list(missing.items()), render_one,
                                    lambda state, detail: store.event("action-clips", state, detail))
        except Exception as exc:                                      # noqa: BLE001 - scenes keep their stills
            store.event("action-clips", "failed", {"error": str(exc)[:300]})
    made = 0
    for scene, image, frames, key, target in todo:
        if target.exists():
            scene.update(action_file=str(target), action_sha256=digest(target.read_bytes()),
                         action_seconds=round((frames - 1) / s["frame_rate"], 3))
            made += 1
    store.event("action-clips", "succeeded", {"clips": made, "planned": len(todo)})
