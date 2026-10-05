"""Resumable instrumental generation and a separate, audited delivery master."""
from __future__ import annotations

import array
import math
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx

from .core import ROOT, digest, gpu_lease, read, write
from .media import audio_info, audit_audio, request
from .planning import script_text


DEFAULT_DELIVERY_GAIN_DB = 4.0


def delivery_levels(config=None):
    gain = (config or {}).get("delivery_gain_db", DEFAULT_DELIVERY_GAIN_DB)
    if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not math.isfinite(gain) or not -12 <= gain <= 12:
        raise ValueError("delivery_gain_db must be a finite number between -12 and 12")
    return {"policy": "delivery-gain-v1", "gain_db": float(gain), "peak_limit": .95}


def _delivery_filter(gain_db, duration):
    levels = delivery_levels({"delivery_gain_db": gain_db})
    # Boost the complete mix once, preserving its speech/music balance. A
    # compensated limiter protects peaks without shifting subtitle timing.
    return (f"volume={levels['gain_db']}dB,"
            f"alimiter=limit={levels['peak_limit']}:level=false:latency=true,atrim=end={duration}")


def level_master(narration, destination, duration, gain_db=DEFAULT_DELIVERY_GAIN_DB):
    """Make an audible speech-only delivery while preserving the clean stem."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    graph = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo," + _delivery_filter(gain_db, duration)
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(narration),
        "-af", graph, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(destination)], check=True)
    return audio_info(destination)


def release_services(config):
    """Called under the cooperative GPU lease; never displace active work."""
    image = request(config, "image", "GET", "/api/status")
    if image.get("busy") or image.get("state") == "loading":
        raise RuntimeError("Images are busy; music cannot acquire the shared GPU")
    if image.get("state") == "loaded":
        request(config, "image", "POST", "/api/unload")
        state = request(config, "image", "GET", "/api/status")
        if state.get("state") in {"loaded", "loading"}:
            raise RuntimeError("Images did not release the shared GPU")
    from .gpu_release import release_vibevoice
    release_vibevoice(config, request_fn=request)
    for base in [config.get("omni_url", "http://127.0.0.1:8094"), config.get("acestep_service_url", "http://127.0.0.1:8006")]:
        try:
            httpx.get(base, timeout=2)
        except httpx.RequestError:
            continue
        raise RuntimeError(f"Another managed music/voice service is present at {base}; leave it undisturbed")


def pcm_metrics(path):
    with wave.open(str(path)) as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("Expected PCM16 music/master WAV")
        samples = array.array("h", wav.readframes(wav.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max((abs(x) for x in samples), default=0) / 32768
    rms = math.sqrt(sum(float(x) * x for x in samples) / max(1, len(samples))) / 32768
    return {"peak": round(peak, 6), "rms": round(rms, 6),
            "clipped_sample_ratio": sum(abs(x) >= 32767 for x in samples) / max(1, len(samples))}


def music_screen(config, store, audio):
    from .music_vocals import screen
    return screen(config, store, audio)


def _generate(config, store, request_path, name):
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    command = [config["acestep_python"], "-u", "-m", "infofilm.music_worker", str(request_path)]
    with gpu_lease(config) as lease:
        if (request_path.parent / "worker-result.json").exists() and (request_path.parent / "music.wav").is_file():
            return
        release_services(config)
        with (request_path.parent / "worker.log").open("ab", buffering=0) as log:
            # An orphaned child retains the GPU lock until its process exits.
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                       pass_fds=(lease.fileno(),), start_new_session=True)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    try:
                        process.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        store.event(name, "waiting", {"elapsed_seconds": round(time.monotonic() - started)})
                    if time.monotonic() - started > config.get("music_timeout_seconds", 1800):
                        raise TimeoutError("ACE-Step exceeded its bounded generation time")
                if process.returncode:
                    raise RuntimeError(f"ACE-Step failed; inspect {request_path.parent / 'worker.log'}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


def generate_score(config, store, film, brief):
    root_model = Path(config["acestep_root"]) / "checkpoints" / config["acestep_model"]
    if not Path(config["acestep_python"]).is_file() or not (root_model / "config.json").is_file():
        raise RuntimeError("ACE-Step runtime or Turbo checkpoint is missing")
    seconds = film["actual_seconds"]
    if not 10 <= seconds <= 600:
        raise ValueError("ACE-Step supports 10–600 seconds; this narration needs another music duration strategy")
    params = {**brief["music"], "caption": "Instrumental background score underneath spoken explanation. No vocals, singing, speech or choir. Sparse arrangement; do not overpower the narrator. " + brief["music"]["caption"],
        "duration_seconds": seconds, "acestep_root": config["acestep_root"], "model": config["acestep_model"],
        "steps": config.get("acestep_steps", 8), "policy": "instrumental-dit-only-v1"}
    identity = digest(params)
    root = store.root / "music" / identity[:20]
    selected = root / "selected.json"
    if selected.exists():
        value = read(selected)
        if Path(value["audio"]).is_file() and digest(Path(value["audio"]).read_bytes()) == value["audio_sha256"]:
            return value
        raise ValueError("Selected music changed after review")
    max_attempts = config.get("music_max_attempts", 3)
    ambiguous_candidates = []
    for attempt in range(max_attempts):
        directory = root / f"take-{attempt}"
        seed = (int(identity[:8], 16) + attempt) % (2**31)
        request_path = write(directory / "request.json", {**params, "seed": seed})
        audio = directory / "music.wav"
        store.event("music-score", "rendering" if attempt == 0 else "retrying", {"attempt": attempt + 1, "max_attempts": max_attempts, "seed": seed})
        if not audio.exists() or not (directory / "worker-result.json").exists():
            try:
                _generate(config, store, request_path, "music-score")
            except (RuntimeError, TimeoutError) as exc:
                write(directory / "failure.json", {"error": str(exc)})
                store.event("music-score", "attempt_failed", {"attempt": attempt + 1, "error": str(exc)})
                continue
        info, levels = audio_info(audio), pcm_metrics(audio)
        screen = music_screen(config, store, audio)
        reasons = []
        if abs(info["duration_seconds"] - seconds) > .5:
            reasons.append("duration_mismatch")
        if levels["rms"] < .0001:
            reasons.append("silent_score")
        if levels["clipped_sample_ratio"] > .005:
            reasons.append("clipped_score")
        if screen["suspected_vocals"]:
            reasons.append("suspected_vocals")
        checks = {"passed": not reasons, "reasons": reasons, "levels": levels, "screen": screen,
                  "requires_listening_review": screen["status"] == "ambiguous"}
        previous_checks = directory / "checks.json"
        if previous_checks.exists() and read(previous_checks) != checks:
            previous = read(previous_checks)
            write(directory / "checks-history" / (digest(previous)[:20] + ".json"), previous)
        write(directory / "checks.json", checks)
        if reasons:
            store.event("music-score", "audit_failed", {"attempt": attempt + 1, "reasons": reasons})
            continue
        value = {"audio": str(audio), **info, "request": params, "seed": seed, "attempt": attempt + 1,
            "checks": checks, "generation": read(directory / "worker-result.json"), "human_listening_review": "pending"}
        if screen["status"] == "ambiguous":
            ambiguous_candidates.append(value)
            store.event("music-score", "screen_ambiguous", {"attempt": attempt + 1, "transcript": screen["transcript"], "listening_review": "pending"})
            continue
        write(selected, value)
        return value
    if ambiguous_candidates:
        value = min(ambiguous_candidates, key=lambda x: (x["checks"]["screen"]["residual_token_count"], x["attempt"]))
        value["selection_policy"] = "No clear-screen take; selected the lowest-information ASR candidate, with listening review pending."
        write(selected, value)
        return value
    raise RuntimeError(f"ACE-Step score needs review after {max_attempts} attempts; narration is preserved")


def mix_master(narration, score, destination, duration, gain=.25, delivery_gain_db=DEFAULT_DELIVERY_GAIN_DB):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fade = min(1.8, duration / 4)
    graph = ("[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[voice][sc];"
        "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"loudnorm=I=-24:TP=-6:LRA=7,aresample=48000,volume={gain},apad,atrim=end={duration},"
        f"afade=t=in:d=0.5,afade=t=out:st={max(0,duration-fade)}:d={fade}[bed];"
        "[bed][sc]sidechaincompress=threshold=0.015:ratio=6:attack=20:release=300[duck];"
        f"[voice][duck]amix=inputs=2:normalize=0:duration=first,{_delivery_filter(delivery_gain_db, duration)}[out]")
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(narration), "-i", str(score),
        "-filter_complex", graph, "-map", "[out]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(destination)], check=True)
    return audio_info(destination)


def prepare_music(config, store, film, brief, engine="none"):
    levels = delivery_levels(config)
    if engine == "none":
        identity = digest({"narration": film["audio_info"]["audio_sha256"], "delivery_levels": levels})
        master = store.root / "delivery-audio" / identity[:20] / "master.wav"
        selected = master.parent / "selected.json"
        if selected.exists():
            value = read(selected)
            if digest(Path(value["master_audio"]).read_bytes()) != value["master_audio_info"]["audio_sha256"]:
                raise ValueError("Selected delivery master changed")
        else:
            info = level_master(film["audio"], master, film["actual_seconds"], levels["gain_db"])
            if abs(info["duration_seconds"] - film["actual_seconds"]) > 1/48000 + 1e-6:
                raise ValueError("Delivery gain changed narration duration")
            if pcm_metrics(master)["clipped_sample_ratio"]:
                raise ValueError("Delivery master contains clipping")
            value = {"engine": "none", "status": "disabled", "master_audio": str(master),
                     "master_audio_info": info, "delivery_levels": levels,
                     "narration_sha256": film["audio_info"]["audio_sha256"]}
            write(selected, value)
    elif engine == "acestep":
        score = generate_score(config, store, film, brief)
        identity = digest({"narration": film["audio_info"]["audio_sha256"], "music": score["audio_sha256"],
                           "mix_policy": "ducked-stereo-v2", "delivery_levels": levels})
        root = store.root / "music-mix" / identity[:20]
        selected = root / "selected.json"
        if selected.exists():
            value = read(selected)
            if digest(Path(value["master_audio"]).read_bytes()) != value["master_audio_info"]["audio_sha256"]:
                raise ValueError("Selected delivery master changed")
        else:
            for attempt, gain in enumerate([.25, .14, .07]):
                master = root / f"take-{attempt}" / "master.wav"
                info = mix_master(film["audio"], score["audio"], master, film["actual_seconds"], gain, levels["gain_db"])
                audit = audit_audio(config, store, master, script_text(film["script"]))
                levels = pcm_metrics(master)
                if abs(info["duration_seconds"] - film["actual_seconds"]) > 1/48000 + 1e-6:
                    raise ValueError("Mixed master changed narration duration")
                if audit["status"] == "passed" and levels["clipped_sample_ratio"] == 0:
                    value = {"engine": engine, "status": "generated", "score": score, "master_audio": str(master),
                        "master_audio_info": info, "mixed_speech_audit": audit, "mix": {"music_gain": gain, "speech_ducking": True, "levels": levels},
                        "narration_sha256": film["audio_info"]["audio_sha256"], "human_listening_review": "pending",
                        "delivery_levels": levels}
                    write(selected, value)
                    break
                store.event("music-mix", "retrying", {"attempt": attempt + 1, "speech_audit": audit["status"], "next_gain": [.14, .07, None][attempt]})
            else:
                raise RuntimeError("Mixed speech still needs review after three gain settings; narration is preserved")
    else:
        raise ValueError("Unknown background score engine")
    write(store.root / f"film-{film['requested_seconds']}" / "music-selection.json", value)
    return value
