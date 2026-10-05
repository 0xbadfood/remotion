"""Checkpointed OmniVoice narration using its native batch API."""
from __future__ import annotations

import json
import os
import subprocess
import time
import wave
from contextlib import ExitStack, contextmanager
from pathlib import Path

import httpx

from .core import digest, gpu_lease, read, write
from .media import audio_info, audit_audio, mfa_numeric_text, request
from .planning import word_count
from .audio_audit import AUDIT_POLICY


@contextmanager
def omni_service(config, store):
    """Own the shared GPU for the whole batch and release it before images."""
    python = Path(config["omni_python"])
    model = Path(config["omni_model"])
    if not python.is_file() or not (model / "config.json").is_file():
        raise RuntimeError("OmniVoice runtime or local model is missing")
    base = config["omni_url"].rstrip("/")
    if base != "http://127.0.0.1:8094":
        raise ValueError("Managed OmniVoice must use the configured local port 8094")
    try:
        httpx.get(base + "/health", timeout=2)
    except httpx.RequestError:
        pass
    else:
        raise RuntimeError("OmniVoice port 8094 is already occupied; leave the other service undisturbed")
    with gpu_lease(config):
        image = request(config, "image", "GET", "/api/status")
        if image.get("busy") or image.get("state") == "loading":
            raise RuntimeError("Image service is busy; OmniVoice will wait for the next run")
        if image.get("state") == "loaded":
            request(config, "image", "POST", "/api/unload")
            image = request(config, "image", "GET", "/api/status")
            if image.get("busy") or image.get("state") in {"loaded", "loading"}:
                raise RuntimeError("Image service did not release the shared GPU")
        from .gpu_release import release_vibevoice
        release_vibevoice(config, store, request_fn=request)
        log_path = store.root / "audio" / "omni-service.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["OMNIVOICE_BATCH_ROOT"] = str(store.root / "audio" / "omni-batches")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen(
                [str(python), "-m", "omnivoice.api", "--model", str(model),
                 "--device", "cuda:0", "--host", "127.0.0.1", "--port", "8094"],
                cwd=Path(config.get("omni_root", "/home/nitin/omni")),
                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        try:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    tail = log_path.read_text(errors="replace")[-2000:]
                    raise RuntimeError(f"OmniVoice exited during startup: {tail}")
                try:
                    health = httpx.get(base + "/health", timeout=3).json()
                    if health.get("status") == "ready" and health.get("native_batching"):
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(2)
            else:
                raise TimeoutError("OmniVoice did not become ready within four minutes")
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def _batch(config, store, base, root, tasks, reference, reference_text, attempt, name):
    record_path = root / f"provider-{attempt}.json"
    manifest = {
        "tasks": [{"id": point["id"], "text": mfa_numeric_text(point["narration"]),
                   "language": "English", "speed": config.get("omni_speed", .8)} for point in tasks],
        "ref_text": reference_text,
        "batch_size": 1,
        "num_step": 32,
        "guidance_scale": 2.0,
        "denoise": True,
        "normalize_text": False,
    }
    if record_path.exists():
        record = read(record_path)
        if not record.get("job_id"):
            raise RuntimeError(f"OmniVoice submission outcome needs reconciliation: {record_path}")
        job_id = record["job_id"]
    else:
        write(record_path, {"state": "submission_started", "manifest": manifest})
        with httpx.Client(timeout=120) as client, reference.open("rb") as handle:
            response = client.post(base + "/v1/audio/batches",
                                   data={"manifest": json.dumps(manifest, ensure_ascii=False)},
                                   files={"ref_audio": (reference.name, handle, "audio/wav")})
            response.raise_for_status()
            job_id = response.json()["id"]
        write(record_path, {"state": "submitted", "job_id": job_id, "manifest": manifest})
    deadline = time.monotonic() + 3600
    last_notice = 0.0
    while time.monotonic() < deadline:
        with httpx.Client(timeout=30) as client:
            response = client.get(base + "/v1/audio/batches/" + job_id)
            response.raise_for_status()
            job = response.json()
        if job.get("status") == "complete":
            break
        if job.get("status") == "failed":
            raise RuntimeError(f"OmniVoice batch {job_id} failed: {job.get('error')}")
        if time.monotonic() - last_notice >= 20:
            store.event(name, "waiting", {"completed": job.get("completed"), "total": job.get("total")})
            last_notice = time.monotonic()
        time.sleep(2)
    else:
        raise TimeoutError(f"OmniVoice batch {job_id} is still unresolved; resume its saved provider job")
    with httpx.Client(timeout=90) as client:
        response = client.get(base + f"/v1/audio/batches/{job_id}/manifest")
        response.raise_for_status()
        results = {row["id"]: row for row in response.json()["results"]}
        for point in tasks:
            target = root / "points" / point["id"] / f"take-{attempt}" / "narration.wav"
            if target.is_file():
                continue
            entry = results.get(point["id"])
            if not entry or entry.get("status") != "complete" or not entry.get("audio_url"):
                raise RuntimeError(f"OmniVoice has no complete clip for {point['id']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            response = client.get(base + entry["audio_url"])
            response.raise_for_status()
            target.write_bytes(response.content)
    return job_id


def _join_wavs(paths, destination):
    params = None
    with wave.open(str(destination), "wb") as output:
        for path in paths:
            with wave.open(str(path), "rb") as source:
                current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if params is None:
                    if current[0] != 1 or current[1] != 2:
                        raise ValueError("OmniVoice must return mono PCM16 WAV clips")
                    output.setparams((*current, 0, "NONE", "not compressed"))
                    params = current
                elif current != params:
                    raise ValueError("OmniVoice point clips have incompatible audio formats")
                output.writeframes(source.readframes(source.getnframes()))


def render_omnivoice(config, store, points, name):
    speed = config.get("omni_speed", .8)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not .5 <= speed <= 2:
        raise ValueError("omni_speed must be between 0.5 and 2")
    max_attempts = config.get("omni_max_attempts", 5)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
        raise ValueError("omni_max_attempts must be an integer between 1 and 10")
    reference = Path(config["reference_audio"]).expanduser().resolve()
    reference_text = " ".join(str(config.get("reference_transcript") or "").split())
    if not reference_text:
        raise ValueError("OmniVoice requires the exact reference recording transcript")
    text = "\n\n".join(point["narration"] for point in points)
    identity = digest({"points": points, "reference_sha256": digest(reference.read_bytes()),
                       "reference_text": reference_text, "model": config["omni_model"],
                       "speed": speed, "variant": config.get("audio_variant", 0)})
    root = store.root / "audio" / "omnivoice" / identity[:20]
    final = root / "narration.wav"
    if final.exists() and (root / "result.json").exists():
        cached = read(root / "result.json")
        if cached.get("audit_policy") == AUDIT_POLICY:
            if digest(final.read_bytes()) != cached["audio_sha256"]:
                raise ValueError("Saved OmniVoice narration changed after its audit")
            return final, cached
    root.mkdir(parents=True, exist_ok=True)
    selected = {}
    started = time.monotonic()
    # Start the GPU service only when a missing take actually needs generation.
    # Resuming re-audits saved clips using the current comparison policy.
    with ExitStack() as stack:
        base = None
        pending = list(points)
        for attempt in range(max_attempts):
            if not pending:
                break
            record_path = root / f"provider-{attempt}.json"
            record = read(record_path) if record_path.exists() else {}
            tasks = [p for p in pending if not (
                root / "points" / p["id"] / f"take-{attempt}" / "narration.wav").is_file()]
            if record:
                # A saved batch is immutable. Points newly rejected by a changed
                # audit policy can be submitted in the next unused batch slot.
                recorded = {p["id"]: p["text"] for p in record["manifest"]["tasks"]}
                tasks = [p for p in tasks if recorded.get(p["id"]) in {p["narration"], mfa_numeric_text(p["narration"])}]
            job_id = record.get("job_id")
            if tasks:
                store.event(name, "point_rendering" if attempt == 0 else "point_retry", {
                    "attempt": attempt + 1, "max_attempts": max_attempts,
                    "points": [p["id"] for p in tasks],
                })
                if base is None:
                    base = stack.enter_context(omni_service(config, store))
                job_id = _batch(config, store, base, root, tasks, reference, reference_text, attempt, name)
            failures = []
            for point in pending:
                clip = root / "points" / point["id"] / f"take-{attempt}" / "narration.wav"
                if not clip.is_file():
                    failures.append(point)
                    continue
                audit = audit_audio(config, store, clip, point["narration"])
                if audit["status"] == "passed":
                    selected[point["id"]] = clip
                else:
                    failures.append(point)
                    store.event(name, "point_audit_failed", {"point": point["id"], "job_id": job_id,
                                                               "attempt": attempt + 1, "max_attempts": max_attempts,
                                                               "score": audit["score"], "reasons": audit["reasons"]})
            pending = failures
        if pending:
            raise RuntimeError(f"OmniVoice point narration still needs review after {max_attempts} attempts: "
                               + ", ".join(p["id"] for p in pending))
        _join_wavs([selected[p["id"]] for p in points], final)
    info = {**audio_info(final), "text": text, "text_sha256": digest(text), "audit_policy": AUDIT_POLICY,
            "reference_audio": str(reference), "reference_audio_sha256": digest(reference.read_bytes()),
            "reference_transcript": reference_text, "language": "en", "provider": "OmniVoice",
            "speed": speed,
            "max_attempts": max_attempts,
            "point_attempts": {key: int(path.parent.name.split("-")[-1]) + 1 for key, path in selected.items()},
            "measured_latency_seconds": round(time.monotonic() - started, 3),
            "observed_words_per_minute": round(word_count(text) / audio_info(final)["duration_seconds"] * 60, 3),
            "point_clips": {key: str(value) for key, value in selected.items()}}
    write(root / "result.json", info)
    store.event(name, "succeeded", {"duration_seconds": info["duration_seconds"],
                                     "observed_words_per_minute": info["observed_words_per_minute"],
                                     "point_clips": len(selected)})
    return final, info
