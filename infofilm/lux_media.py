"""Checkpointed LuxTTS narration (Apache-2.0, 48 kHz voice cloning). Each point is a clip; every clip passes the audio
audit, and failed points are retaken with a new seed (2026-10-23: chosen over VibeVoice in a blind test of the heart
film). With `lux_url` set the clips are made by the amdserver's media factory on its GPU (user, 2026-10-24); if it
cannot be reached, the same manifest runs on the local CPU, with the same seeds."""
from __future__ import annotations

import json
import re
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
    return bool(config.get("lux_url")) or (Path(config.get("lux_python", "")).is_file()
                                          and (Path(config.get("lux_root", "")) / "zipvoice" / "luxvoice.py").is_file())


def _run_remote(config, store, manifest, name):
    """The manifest's missing clips from the media factory; False when it cannot be reached (the caller runs locally)."""
    import httpx
    tasks = [t for t in manifest["tasks"] if not Path(t["out"]).is_file()]
    if not tasks:
        return True
    body = {**{k: manifest[k] for k in ["model", "speed", "num_steps", "t_shift", "seed", "reference_text"]},
            "tasks": [{"id": t["id"], "index": t["index"], "text": t["text"]} for t in tasks]}
    url = config["lux_url"].rstrip("/")
    try:
        with httpx.Client(timeout=60) as client:
            with open(manifest["reference"], "rb") as reference:
                job = client.post(url + "/api/tts", data={"manifest": json.dumps(body)},
                                  files={"reference": ("reference.wav", reference, "audio/wav")})
            job.raise_for_status()
            job, started = job.json(), time.monotonic()
            store.event(name, "remote_job", {"url": url, "job": job["id"], "points": [t["id"] for t in tasks]})
            seen = set()
            while job["status"] not in ("complete", "error"):
                if time.monotonic() - started > 3600:
                    raise TimeoutError(f"LuxTTS job {job['id']} still running after an hour")
                time.sleep(1)
                job = client.get(f"{url}/api/jobs/{job['id']}").json()
                seen = _recorded(store, name, manifest, [c["id"] if isinstance(c, dict) else c for c in job.get("clips") or []], seen)
            if job["status"] == "error":
                raise RuntimeError(f"Media factory LuxTTS job failed: {job.get('message')}")
            for task in tasks:
                out = Path(task["out"])
                out.parent.mkdir(parents=True, exist_ok=True)
                part = out.with_name(out.name + ".part")
                part.write_bytes(client.get(f"{url}/outputs/{job['id']}/{task['id']}.wav").raise_for_status().content)
                part.replace(out)
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        store.event(name, "remote_unavailable", {"url": url, "error": str(exc)[:200]})
        return False
    return True


SPEAKABLE = 5       # the version of speakable(): part of a recording's identity, so a change records again
SAID_AS_WORDS = {"NASA", "ISRO", "LASER", "RADAR", "SONAR", "UNESCO", "UNICEF", "NATO", "SCUBA", "AIDS", "COVID", "OPEC",
                 "SEBI", "NITI", "ASEAN", "SAARC", "BRICS", "GATT", "FIFA", "OK"}
ROMAN = re.compile(r"^[IVXLCDM]+$")


def speakable(text):
    """The script as LuxTTS should read it (user, 2026-10-25: "The SI unit" was spoken "the see unit" and every
    Whisper heard "the C unit"): an acronym is spelled letter by letter (S.I., D.N.A.) unless it is said as a word or
    is a Roman numeral, and unit letters standing alone are spelled too ("m s to the power minus two" -> "m. s.").
    Only the voice's input changes; alignment and the audit still compare against the script as written."""
    def acronym(m):
        word = m.group(0)
        if word in SAID_AS_WORDS or ROMAN.match(word):
            return word
        return ".".join(word) + "."
    text = re.sub(r"\b[A-Z]{2,5}\b", acronym, text)
    # The digits after a point, five or more, are said one by one with commas between: the voice ran long decimals
    # together ("three hundred seventy three point seven one four seven five four" came out "3 meters, 307, 17.5" in
    # every take of the significant-figures film, 2026-10-29).
    digit = r"(?:zero|one|two|three|four|five|six|seven|eight|nine)"
    text = re.sub(rf"\b(point\s+)({digit}(?:\s+{digit}\b(?!-)){{4,}})",
                  lambda m: m.group(1) + ", ".join(m.group(2).split()), text, flags=re.I)
    # A run of the same digit word ("zero zero zero") is said with commas between: in "one point zero zero zero three"
    # (air's refractive index) the voice dropped a zero in every take of Class 10 Light C6 (2026-10-29), and the
    # audit rightly heard 1.003; with commas each is said on its own.
    text = re.sub(r"\b(zero|one|two|three|four|five|six|seven|eight|nine)(?:\s+\1\b(?!-))+",
                  lambda m: ", ".join([m.group(1)] * len(m.group(0).split())), text, flags=re.I)
    # Two or more single letters in a row (not the words a and I) are a unit or a label: spell them. A unit vector
    # (k-hat) is a word of its own: "A z k-hat" became "A z. k.-hat", and the voice said "a zic hat" (2026-10-26).
    return re.sub(r"\b(?:[b-hj-zB-HJ-Z]\s+)+[b-hj-zB-HJ-Z]\b(?![.-])", lambda m: " ".join(c + "." for c in m.group(0).split()), text)


def _recorded(store, name, manifest, done, seen):
    """An event for each clip newly recorded, so the studio can show narration progress while the worker runs."""
    for point in [p for p in done if p not in seen]:
        store.event(name, "point_recorded", {"point": point, "attempt": manifest.get("attempt", manifest["seed"]) + 1, "of": len(manifest["tasks"])})
    return seen | set(done)


def _run(config, store, root, settings, reference, reference_text, tasks, attempt, name):
    """One worker run for the clips of this attempt. The manifest is saved first, so a rerun resumes it."""
    manifest_path = root / f"provider-{attempt}.json"
    manifest = {**{k: settings[k] for k in ["model", "device", "threads", "speed", "num_steps", "t_shift"]},
                "lux_root": str(Path(config["lux_root"]).resolve()), "reference": str(reference),
                # A whole-film repair (audio_variant 1) gets seeds of its own: with the first take's seeds it
                # recorded the same audio byte for byte and failed the same way (the vector film, 2026-10-26).
                "reference_text": reference_text or None, "attempt": attempt,
                "seed": attempt + 100 * int(config.get("audio_variant", 0)),
                "tasks": [{"id": p["id"], "index": i, "text": speakable(mfa_numeric_text(p["narration"])),
                           "out": str(root / "points" / p["id"] / f"take-{attempt}" / "narration.wav")}
                          for i, p in enumerate(tasks)]}
    if manifest_path.exists():
        saved = read(manifest_path)
        known = {t["id"] for t in saved["tasks"]}
        manifest["tasks"] = [*saved["tasks"], *[t for t in manifest["tasks"] if t["id"] not in known]]
    write(manifest_path, manifest)
    if config.get("lux_url") and _run_remote(config, store, manifest, name):
        store.event(name, "worker_done", {"attempt": attempt + 1, "points": [p["id"] for p in tasks], "remote": True})
        return
    log_path = root / "worker.log"
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    seen, started = {t["id"] for t in manifest["tasks"] if Path(t["out"]).is_file()}, time.monotonic()
    with log_path.open("ab", buffering=0) as log:
        # -I: the worker sits in infofilm/, whose queue.py would otherwise hide the standard library's queue.
        process = subprocess.Popen([config["lux_python"], "-I", str(WORKER), str(manifest_path)], cwd=config["lux_root"],
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
        while process.poll() is None:
            if time.monotonic() - started > 3600:
                process.kill()
                raise TimeoutError("LuxTTS worker still running after an hour")
            time.sleep(1)
            seen = _recorded(store, name, manifest, [t["id"] for t in manifest["tasks"] if Path(t["out"]).is_file()], seen)
    _recorded(store, name, manifest, [t["id"] for t in manifest["tasks"] if Path(t["out"]).is_file()], seen)
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


NUMBER = re.compile(r"\d+(?:\.\d+)?|\b(?:zero|one|two|three|four|five|six|seven|eight|nine)\s+point\b|\bto the power\b", re.I)
DENSE = 5           # numbers in a point from which it is recorded sentence by sentence


def pieces(point):
    """A point with many numbers, as its sentences (each recorded and audited on its own, then joined): in a point
    reading ten numbers every take slipped on one, a different one each time, and the whole point was taken again
    (the significant-figures film, 2026-10-29). Other points stay whole."""
    sentences = [x for x in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", point["narration"].strip()) if x]
    if len(NUMBER.findall(point["narration"])) < DENSE or len(sentences) < 2:
        return [point]
    return [{**point, "id": f"{point['id']}-s{j + 1}", "narration": x, "part_of": point["id"]} for j, x in enumerate(sentences)]


def render_luxtts(config, store, points, name):
    if not lux_installed(config):
        raise RuntimeError("LuxTTS runtime is missing (lux_python, lux_root)")
    settings = lux_settings(config)
    reference = Path(config["reference_audio"]).expanduser().resolve()
    reference_text = " ".join(str(config.get("reference_transcript") or "").split())
    text = "\n\n".join(point["narration"] for point in points)
    identity = digest({"points": points, "reference_sha256": digest(reference.read_bytes()), "reference_text": reference_text,
                       "speakable": SPEAKABLE, "pieces": DENSE,
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
    units = [piece for p in points for piece in pieces(p)]
    selected, pending, started = {}, list(units), time.monotonic()
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
            # The point's earlier failed takes go with it: the same failure twice goes to the LLM arbiter (as in
            # vibe_media). Without them the arbiter never ran for LuxTTS (the projectile film, 2026-10-26: 'v naught'
            # heard as v0 failed every take of six points). Saved beside each take, so a resumed run has them too.
            earlier = [read(f) for k in range(attempt)
                       if (f := root / "points" / point["id"] / f"take-{k}" / "audit.json").is_file()]
            audit = audit_audio(config, store, clip, point["narration"], earlier_audits=earlier)
            write(clip.parent / "audit.json", {k: v for k, v in audit.items() if k != "recognizer_attempts"})
            if audit["status"] == "passed":
                selected[point["id"]] = clip
                store.event(name, "point_accepted", {"point": point["id"], "attempt": attempt + 1, "score": audit["score"]})
            else:
                failures.append(point)
                store.event(name, "point_audit_failed", {"point": point["id"], "attempt": attempt + 1,
                                                         "max_attempts": settings["max_attempts"],
                                                         "score": audit["score"], "reasons": audit["reasons"]})
        pending = failures
    if pending:
        raise RuntimeError(f"LuxTTS point narration still needs review after {settings['max_attempts']} attempts: "
                           + ", ".join(p["id"] for p in pending))
    taken = {key: int(path.parent.name.split("-")[-1]) + 1 for key, path in selected.items()}
    for p in points:                                    # a point recorded in sentences: its sentences joined
        parts = [u["id"] for u in units if u.get("part_of") == p["id"]]
        if parts:
            joined = root / "points" / p["id"] / "joined" / "narration.wav"
            joined.parent.mkdir(parents=True, exist_ok=True)
            _join_wavs([selected[x] for x in parts], joined)
            selected[p["id"]] = joined
            taken[p["id"]] = max(taken[x] for x in parts)
            for x in parts:
                selected.pop(x)
    _join_wavs([selected[p["id"]] for p in points], final)
    spoken = {p["id"]: mfa_numeric_text(p["narration"]) for p in points}
    info = {**audio_info(final), "text": text, "text_sha256": digest(text), "audit_policy": AUDIT_POLICY,
            "spoken_points": spoken, "reference_audio": str(reference), "reference_audio_sha256": digest(reference.read_bytes()),
            "reference_transcript": reference_text or None, "language": "en", "provider": "LuxTTS",
            **{k: settings[k] for k in ["model", "device", "speed", "num_steps", "t_shift", "max_attempts"]},
            "point_attempts": {key: taken[key] for key in selected},
            "measured_latency_seconds": round(time.monotonic() - started, 3),
            "observed_words_per_minute": round(word_count(text) / audio_info(final)["duration_seconds"] * 60, 3),
            "point_clips": {key: str(value) for key, value in selected.items()}}
    write(root / "result.json", info)
    store.event(name, "succeeded", {"duration_seconds": info["duration_seconds"],
                                     "observed_words_per_minute": info["observed_words_per_minute"], "point_clips": len(selected)})
    return final, info
