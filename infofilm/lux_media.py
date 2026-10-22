"""Checkpointed LuxTTS narration (Apache-2.0, 48 kHz voice cloning). It runs on the CPU by default, so it needs no
GPU lease and can run while the image model holds the GPU. Each point is a clip; every clip passes the audio audit,
and failed points are retaken with a new seed (2026-10-23: chosen over VibeVoice in a blind test of the heart film)."""
from __future__ import annotations

import json
import os
import subprocess
import time
import wave
from pathlib import Path

from .audio_audit import AUDIT_POLICY
from .core import digest, read, write
from .media import audio_info, audit_audio, mfa_numeric_text
from .planning import word_count

WORKER = Path(__file__).with_name("lux_worker.py")


def lux_settings(config):
    speed = config.get("lux_speed", .75)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not .4 <= speed <= 1.5:
        raise ValueError("lux_speed must be between 0.4 and 1.5 (LuxTTS speeds speech up 1.3x internally; 0.75 is natural)")
    attempts = config.get("lux_max_attempts", 4)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
        raise ValueError("lux_max_attempts must be an integer between 1 and 10")
    return {"model": config.get("lux_model", "YatharthS/LuxTTS"), "device": config.get("lux_device", "cpu"),
            "threads": int(config.get("lux_threads", 16)), "speed": float(speed), "num_steps": int(config.get("lux_steps", 4)),
            "t_shift": float(config.get("lux_t_shift", .5)), "max_attempts": attempts}


def lux_installed(config):
    return Path(config.get("lux_python", "")).is_file() and (Path(config.get("lux_root", "")) / "zipvoice" / "luxvoice.py").is_file()


def _run(config, store, root, settings, reference, reference_text, tasks, attempt, name):
    """One worker run for the clips of this attempt. The manifest is saved first, so a rerun resumes it."""
    manifest_path = root / f"provider-{attempt}.json"
    manifest = {**{k: settings[k] for k in ["model", "device", "threads", "speed", "num_steps", "t_shift"]},
                "lux_root": str(Path(config["lux_root"]).resolve()), "reference": str(reference),
                "reference_text": reference_text or None, "seed": attempt,
                "tasks": [{"id": p["id"], "index": i, "text": mfa_numeric_text(p["narration"]),
                           "out": str(root / "points" / p["id"] / f"take-{attempt}" / "narration.wav")}
                          for i, p in enumerate(tasks)]}
    if manifest_path.exists():
        saved = read(manifest_path)
        known = {t["id"] for t in saved["tasks"]}
        manifest["tasks"] = [*saved["tasks"], *[t for t in manifest["tasks"] if t["id"] not in known]]
    write(manifest_path, manifest)
    log_path = root / "worker.log"
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    with log_path.open("ab", buffering=0) as log:
        # -I: the worker sits in infofilm/, whose queue.py would otherwise hide the standard library's queue.
        process = subprocess.run([config["lux_python"], "-I", str(WORKER), str(manifest_path)], cwd=config["lux_root"],
                                 env=env, stdout=log, stderr=subprocess.STDOUT, timeout=3600)
    if process.returncode:
        tail = log_path.read_text(errors="replace")[-2000:]
        raise RuntimeError(f"LuxTTS worker failed (exit {process.returncode}): {tail}")
    store.event(name, "worker_done", {"attempt": attempt + 1, "points": [p["id"] for p in tasks]})


def _join_wavs(paths, destination):
    params = None
    with wave.open(str(destination), "wb") as output:
        for path in paths:
            with wave.open(str(path), "rb") as source:
                current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if params is None:
                    if current[0] != 1 or current[1] != 2:
                        raise ValueError("LuxTTS must write mono PCM16 WAV clips")
                    output.setparams((*current, 0, "NONE", "not compressed"))
                    params = current
                elif current != params:
                    raise ValueError("LuxTTS point clips have incompatible audio formats")
                output.writeframes(source.readframes(source.getnframes()))


def render_luxtts(config, store, points, name):
    if not lux_installed(config):
        raise RuntimeError("LuxTTS runtime is missing (lux_python, lux_root)")
    settings = lux_settings(config)
    reference = Path(config["reference_audio"]).expanduser().resolve()
    reference_text = " ".join(str(config.get("reference_transcript") or "").split())
    text = "\n\n".join(point["narration"] for point in points)
    identity = digest({"points": points, "reference_sha256": digest(reference.read_bytes()), "reference_text": reference_text,
                       **{k: settings[k] for k in ["model", "speed", "num_steps", "t_shift"]}, "variant": config.get("audio_variant", 0)})
    root = store.root / "audio" / "luxtts" / identity[:20]
    final = root / "narration.wav"
    if final.exists() and (root / "result.json").exists():
        cached = read(root / "result.json")
        if cached.get("audit_policy") == AUDIT_POLICY:
            if digest(final.read_bytes()) != cached["audio_sha256"]:
                raise ValueError("Saved LuxTTS narration changed after its audit")
            return final, cached
    root.mkdir(parents=True, exist_ok=True)
    selected, pending, started = {}, list(points), time.monotonic()
    for attempt in range(settings["max_attempts"]):
        if not pending:
            break
        missing = [p for p in pending if not (root / "points" / p["id"] / f"take-{attempt}" / "narration.wav").is_file()]
        if missing:
            store.event(name, "point_rendering" if attempt == 0 else "point_retry",
                        {"attempt": attempt + 1, "max_attempts": settings["max_attempts"], "points": [p["id"] for p in missing]})
            _run(config, store, root, settings, reference, reference_text, missing, attempt, name)
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
                store.event(name, "point_audit_failed", {"point": point["id"], "attempt": attempt + 1,
                                                         "max_attempts": settings["max_attempts"],
                                                         "score": audit["score"], "reasons": audit["reasons"]})
        pending = failures
    if pending:
        raise RuntimeError(f"LuxTTS point narration still needs review after {settings['max_attempts']} attempts: "
                           + ", ".join(p["id"] for p in pending))
    _join_wavs([selected[p["id"]] for p in points], final)
    spoken = {p["id"]: mfa_numeric_text(p["narration"]) for p in points}
    info = {**audio_info(final), "text": text, "text_sha256": digest(text), "audit_policy": AUDIT_POLICY,
            "spoken_points": spoken, "reference_audio": str(reference), "reference_audio_sha256": digest(reference.read_bytes()),
            "reference_transcript": reference_text or None, "language": "en", "provider": "LuxTTS",
            **{k: settings[k] for k in ["model", "device", "speed", "num_steps", "t_shift", "max_attempts"]},
            "point_attempts": {key: int(path.parent.name.split("-")[-1]) + 1 for key, path in selected.items()},
            "measured_latency_seconds": round(time.monotonic() - started, 3),
            "observed_words_per_minute": round(word_count(text) / audio_info(final)["duration_seconds"] * 60, 3),
            "point_clips": {key: str(value) for key, value in selected.items()}}
    write(root / "result.json", info)
    store.event(name, "succeeded", {"duration_seconds": info["duration_seconds"],
                                     "observed_words_per_minute": info["observed_words_per_minute"], "point_clips": len(selected)})
    return final, info
