"""A pool of video workers for scene clips: one or more LTX endpoints, each pulling scenes from a shared queue
(user, 2026-10-16: a generic pool, so more than one worker can take the video job; the Spark joins the amdserver).

Worker kinds:
  ltx_comfy       the LTX 2.5 INT8 ComfyUI API (amdserver `~/projects/ltx25/api`): RIFE runs inside its graph.
                  `mode_switch`: the amdserver GPU mode switch; prepare waits for video mode, release only *asks* for
                  image mode (the next image stage waits for it), so the film does not wait ~5 min for the switch back.
  ltx_storyvault  the LTX 2.5 NVFP4 API (Spark `~/ltx25-api`): StoryVault tracking fields, no RIFE (run here,
                  `infofilm.rife`). `service`: a local user unit started early (warm) and stopped on release.
A worker that cannot be prepared simply takes no scenes; a scene that fails on one worker is retried once on another;
a worker that fails twice in a row stops taking scenes. Anything left keeps the depth camera.
"""
from __future__ import annotations

import collections
import subprocess
import threading
import time
from pathlib import Path

import httpx

READY_TIMEOUT = 900


def worker_configs(settings):
    """The configured workers; a single ltx_comfy worker from `url`/`mode_switch` when none are listed."""
    if settings.get("workers"):
        return [w for w in settings["workers"] if w.get("enabled", True)]
    return [{"id": "video", "kind": "ltx_comfy", "url": settings["url"], "mode_switch": settings.get("mode_switch")}]


def switch_mode(url, mode, timeout, wait=True):
    """Ask the amdserver mode switch for `mode`; with wait, return once it is ready."""
    with httpx.Client(timeout=30) as client:
        state = client.get(url + "/status").json()
        if state.get("mode") == mode and state.get("phase") == "ready":
            return
        if state.get("target") != mode:
            client.post(f"{url}/mode/{mode}")              # 409 while another switch runs: the wait below covers it
        if not wait:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = client.get(url + "/status").json()
            if state.get("mode") == mode and state.get("phase") == "ready":
                return
            if state.get("phase") == "failed" and state.get("target") in (None, mode):
                raise RuntimeError(f"Mode switch to {mode} failed: {state.get('detail')}")
            time.sleep(10)
    raise RuntimeError(f"Mode switch to {mode} not ready after {timeout // 60} min")


class Worker:
    def __init__(self, config, settings):
        self.config, self.settings = config, settings
        self.id = config.get("id") or config["kind"]
        self.url = config["url"].rstrip("/")

    def warm(self):
        """Start loading early (during the image stage). Default: nothing."""

    def prepare(self):
        """Block until the worker can take clips."""

    def release(self):
        """Hand the hardware back."""

    def _poll(self, client, status_url, params):
        deadline = time.monotonic() + self.settings["timeout_seconds"]
        while time.monotonic() < deadline:
            state = client.get(status_url, params=params).json()
            if state["status"] in ("succeeded", "completed"):
                return state
            if state["status"] in ("failed", "error", "cancelled"):
                raise RuntimeError(f"{self.id}: clip job {state['status']}: {str(state.get('error'))[:200]}")
            time.sleep(3)
        raise TimeoutError(f"{self.id}: clip job did not finish in time")

    def _request(self, motion):
        s = self.settings
        return {"mode": "conditioning_image_to_video", "prompt": motion, "width": s["width"], "height": s["height"],
                "num_frames": s["frames"], "frame_rate": s["frame_rate"], "seed": s["seed"],
                "conditioning_frame_index": s["middle_frame"], "condition_strength": s["strength"]}


class ComfyWorker(Worker):
    def prepare(self):
        if self.config.get("mode_switch"):
            switch_mode(self.config["mode_switch"], "video", self.settings["switch_timeout_seconds"])

    def release(self):
        if self.config.get("mode_switch"):
            switch_mode(self.config["mode_switch"], "image", self.settings["switch_timeout_seconds"], wait=False)

    def render(self, still, motion, target, key):
        with httpx.Client(timeout=60) as client, open(still, "rb") as image:
            job = client.post(self.url + "/v1/videos/generations", files={"conditioning_image": image}, data={
                **self._request(motion), "interpolate": self.settings["interpolate"],
                "idempotency_key": f"clip-{key}-{int(time.time())}"}).raise_for_status().json()
            token = job.get("token") or job.get("poll_token")
            self._poll(client, f"{self.url}/v1/videos/{job['id']}", {"token": token})
            data = client.get(f"{self.url}/v1/videos/{job['id']}/content", params={"token": token}, timeout=300)
            target.write_bytes(data.raise_for_status().content)
        return target


class StoryVaultWorker(Worker):
    def _systemctl(self, *args):
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)

    def warm(self):
        if self.config.get("service"):
            self._systemctl("start", "--no-block", self.config["service"])

    def prepare(self):
        self.warm()
        deadline = time.monotonic() + READY_TIMEOUT
        with httpx.Client(timeout=10) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get(self.url + "/ready").json().get("status") == "ready":
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(5)
        raise RuntimeError(f"{self.id}: video API not ready after {READY_TIMEOUT // 60} min")

    def release(self):
        if self.config.get("service"):
            self._systemctl("stop", "--no-block", self.config["service"])

    def render(self, still, motion, target, key):
        from .rife import interpolate
        raw = target.with_suffix(".raw.mp4")
        with httpx.Client(timeout=60) as client, open(still, "rb") as image:
            job = client.post(self.url + "/v1/videos/generations",
                              files={"conditioning_image": (Path(still).name, image, "image/png")}, data={
                **self._request(motion), "output_audio": "false", "idempotency_key": f"clip-{key}-{int(time.time())}",
                "client_request_id": f"infofilm-{key}", "story_id": "infofilm", "shot_id": key[:16],
                "asset_id": key[:16], "source_plan_sha256": key.ljust(64, "0")[:64]}).raise_for_status().json()
            state = self._poll(client, f"{self.url}/v1/videos/{job['id']}", {"token": job["token"]})
            # The service's download_url carries its configured public host; fetch the same path locally.
            link = state.get("download_url") or f"/v1/videos/{job['id']}/content?token={job['token']}"
            path = "/" + link.split("/", 3)[-1] if link.startswith("http") else link
            raw.write_bytes(client.get(self.url + path, timeout=300).raise_for_status().content)
        interpolate(raw, target, self.settings["interpolate"])
        raw.unlink(missing_ok=True)
        return target


KINDS = {"ltx_comfy": ComfyWorker, "ltx_storyvault": StoryVaultWorker}


def build(settings):
    return [KINDS[c["kind"]](c, settings) for c in worker_configs(settings)]


def warm(settings):
    """Start workers that need a long load (the Spark's service) while the images are still being made."""
    for worker in build(settings):
        try:
            worker.warm()
        except Exception:                               # noqa: BLE001 - warming is a head start, not a requirement
            pass


def ensure_image_mode(settings):
    """Before an image stage: a video worker's switch back to image mode may still be running; wait for it."""
    for config in worker_configs(settings):
        if config.get("mode_switch"):
            switch_mode(config["mode_switch"], "image", settings["switch_timeout_seconds"])


def run(settings, jobs, render_one, event):
    """Run render_one(worker, job) for every job across the pool. Returns nothing; render_one records results.
    jobs: list of (asset_id, payload). render_one raises on failure."""
    queue, lock = collections.deque(jobs), threading.Lock()
    attempts = collections.Counter()
    alive = set()

    def work(worker):
        failures = 0
        try:
            try:
                worker.prepare()
            except Exception as exc:                     # noqa: BLE001 - this worker takes no scenes
                event("worker_unavailable", {"worker": worker.id, "error": f"{type(exc).__name__}: {exc}"[:300]})
                return
            with lock:
                alive.add(worker.id)
            while failures < 2:
                with lock:
                    if not queue:
                        break
                    asset_id, payload = queue.popleft()
                try:
                    render_one(worker, asset_id, payload)
                    failures = 0
                except Exception as exc:                 # noqa: BLE001 - retried once elsewhere, then the still stays
                    failures += 1
                    with lock:
                        attempts[asset_id] += 1
                        retry = attempts[asset_id] < 2 and len(alive) > 1
                        if retry:
                            queue.append((asset_id, payload))
                    event("retry" if retry else "fallback", {"asset": asset_id, "worker": worker.id,
                                                            "reason": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            with lock:
                alive.discard(worker.id)
            try:
                worker.release()
            except Exception as exc:                     # noqa: BLE001 - reported; ensure_image_mode recovers
                event("release_failed", {"worker": worker.id, "error": str(exc)[:300]})

    threads = [threading.Thread(target=work, args=(w,), name=f"video-{w.id}", daemon=True) for w in build(settings)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with lock:
        return [asset_id for asset_id, _ in queue]     # never taken: no worker was available
