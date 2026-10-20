"""Motion beats (key-idea films, user 2026-10-20): an image beat with an `action` becomes a short LTX video that starts
from the accepted still and shows the action, as long as its beat (3-10 s), played at about normal speed.

Unlike the illustration clips (clips.py: gentle motion slowed to fill a scene), these clips carry part of the
explanation: the guide climbing down two steps is the idea. They are planned with the scene, rendered from the
still's first frame (LTX "first_image_to_video" on the Spark's LTX service, tested 2026-10-20: the character stays
the same and does the action; 8 s in about 4 minutes, 10 s in about 5), and noted on the scene (action_file,
action_sha256, action_seconds). No review for now: the user judges the first pilots and the review strategy follows
from that. A clip that fails to render leaves its scene as the still with the depth camera; clips never stop a film.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import httpx

from .core import digest, read, write

PROTOCOL = "infofilm.action-clips.v1"
DEFAULTS = {"url": "http://127.0.0.1:8010", "service": "ltx25-api", "width": 768, "height": 448, "frame_rate": 24,
            "condition_strength": .9, "seed": 11, "timeout_seconds": 1200,
            "style": "Flat 2D cartoon animation, steady camera, plain uncluttered background, no text."}
LONGEST_FRAMES = 241            # 10 s at 24 fps; LTX needs (frames - 1) % 8 == 0


def settings(config):
    return {**DEFAULTS, **(config.get("action_clips") or {})}


def frames_for(seconds, rate):
    """Frames for a clip at least as long as the beat (LTX: 8k + 1), at most LONGEST_FRAMES."""
    return min(LONGEST_FRAMES, max(9, math.ceil((seconds * rate - 1) / 8) * 8 + 1))


def prompt_for(scene, s):
    return f"{s['style']} {scene['action']}"


class Spark:
    """The Spark's LTX service, started for the clips and stopped after (it shares the Spark with the voices)."""

    def __init__(self, s):
        from .video_pool import READY_TIMEOUT, StoryVaultWorker
        self.s, self.ready_timeout = s, READY_TIMEOUT
        self.worker = StoryVaultWorker({"id": "spark", "kind": "ltx_storyvault", "url": s["url"],
                                        "service": s["service"]}, {"timeout_seconds": s["timeout_seconds"]})

    def __enter__(self):
        self.worker.prepare()
        return self

    def __exit__(self, *exc):
        self.worker.release()

    def render(self, still, prompt, frames, target, key):
        s = self.s
        with httpx.Client(timeout=60) as client, open(still, "rb") as image:
            job = client.post(s["url"] + "/v1/videos/generations",
                              files={"first_image": (Path(still).name, image, "image/png")}, data={
                "mode": "first_image_to_video", "prompt": prompt, "width": s["width"], "height": s["height"],
                "num_frames": frames, "frame_rate": s["frame_rate"], "seed": s["seed"],
                "condition_strength": s["condition_strength"], "output_audio": "false",
                "idempotency_key": f"action-{key}-{int(time.time())}", "client_request_id": f"infofilm-action-{key}",
                "story_id": "infofilm", "shot_id": key[:16], "asset_id": key[:16],
                "source_plan_sha256": key.ljust(64, "0")[:64]}).raise_for_status().json()
            state = self.worker._poll(client, f"{s['url']}/v1/videos/{job['id']}", {"token": job["token"]})
            link = state.get("download_url") or f"/v1/videos/{job['id']}/content?token={job['token']}"
            path = "/" + link.split("/", 3)[-1] if link.startswith("http") else link
            target.write_bytes(client.get(s["url"] + path, timeout=300).raise_for_status().content)
        return target


def make_action_clips(config, store, films, images, service=None):
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
    try:
        missing = [t for t in todo if not t[4].exists()]
        if missing:
            with (service or Spark(s)) as ltx:
                for scene, image, frames, key, target in missing:
                    started = time.monotonic()
                    try:
                        ltx.render(image["file"], prompt_for(scene, s), frames, target, key)
                        store.event("action-clips", "rendered", {"scene": scene["id"], "frames": frames,
                                                                 "seconds": round(time.monotonic() - started, 1)})
                    except Exception as exc:                          # noqa: BLE001 - this scene keeps its still
                        target.unlink(missing_ok=True)
                        store.event("action-clips", "fallback", {"scene": scene["id"], "error": str(exc)[:300]})
    except Exception as exc:                                          # noqa: BLE001 - the service never came up
        store.event("action-clips", "failed", {"error": str(exc)[:300]})
    made = 0
    for scene, image, frames, key, target in todo:
        if target.exists():
            scene.update(action_file=str(target), action_sha256=digest(target.read_bytes()),
                         action_seconds=round((frames - 1) / s["frame_rate"], 3))
            made += 1
    store.event("action-clips", "succeeded", {"clips": made, "planned": len(todo)})
