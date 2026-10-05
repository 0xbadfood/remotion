"""A music-specific vocal screen: speech activity first, stable-ts when useful."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from .core import ROOT, digest, gpu_lease, read, write

POLICY = "speech-activity-stable-ts-v1"


def classify_vocals(vad, transcript):
    """Only supported, confident words can turn a music take into a hard failure."""
    if not vad["regions"]:
        return {"status": "clear", "suspected_vocals": False, "transcript": "", "residual_token_count": 0,
                "decision": "No speech-like regions detected; ASR was skipped."}
    text = transcript.get("transcript", "")
    supported = []
    for segment in transcript.get("segments", []):
        for word in segment.get("words", []):
            token = re.sub(r"[^a-z]", "", word.get("word", "").lower())
            start, end = word.get("start", 0), word.get("end", 0)
            if (len(token) >= 2 and word.get("probability", 0) >= .5 and end > start
                    and end-start <= 4 and any(min(end,r["end"]) > max(start,r["start"]) for r in vad["regions"])):
                supported.append(token)
    # One isolated/repeated syllable is still insufficient to establish vocals.
    suspected = len(supported) >= 2 and len(set(supported)) >= 2
    return {"status": "suspected" if suspected else "ambiguous", "suspected_vocals": suspected,
            "transcript": text, "residual_token_count": len(supported), "supported_tokens": supported,
            "decision": "Speech activity and confident words corroborate vocals." if suspected else
                        "Speech-like activity without corroborated words; listening review remains pending."}


def _stable_transcript(config, store, root, audio):
    python = Path(config.get("music_stable_ts_python", "/home/nitin/stable-ts/.venv/bin/python"))
    model = Path(config.get("music_stable_ts_model", str(Path.home() / ".cache/whisper/large-v3.pt")))
    if not python.is_file() or not model.is_file():
        raise RuntimeError("The stable-ts runtime or cached large-v3 checkpoint is missing")
    request = write(root / "request.json", {"audio": str(Path(audio).resolve()), "model_file": str(model),
        "with_demucs": config.get("music_vocal_isolation", False),
        "demucs_bin": config.get("music_demucs_bin", str(Path(config["story_backend"]) / "env/bin/demucs")),
        "demucs_cache": str(Path.home() / ".cache/torch/hub/checkpoints")})
    from .music import release_services
    with gpu_lease(config) as lease:
        if (root / "worker-result.json").exists():
            return read(root / "worker-result.json")
        release_services(config)
        env = {**os.environ, "INFOFILM_GPU_LEASE_FD": str(lease.fileno()), "PYTHONDONTWRITEBYTECODE": "1",
               "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        with (root / "worker.log").open("ab", buffering=0) as log:
            process = subprocess.Popen([str(python), "-u", "-m", "infofilm.music_screen_worker", str(request)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, pass_fds=(lease.fileno(),), start_new_session=True)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    try:
                        process.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        store.event("music-vocals", "waiting", {"elapsed_seconds": round(time.monotonic()-started), "engine": "stable-ts"})
                    if time.monotonic()-started > config.get("music_screen_timeout_seconds", 600):
                        raise TimeoutError("Music vocal screening exceeded its bounded time")
                if process.returncode:
                    raise RuntimeError(f"Stable-ts vocal screening failed; inspect {root / 'worker.log'}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
    return read(root / "worker-result.json")


def screen(config, store, audio):
    audio = Path(audio).resolve()
    audio_hash = digest(audio.read_bytes())
    identity = digest({"audio": audio_hash, "policy": POLICY,
        "isolation": config.get("music_vocal_isolation", False),
        "model": config.get("music_stable_ts_model", "large-v3"),
        "worker_revision": {name: digest((ROOT / "infofilm" / name).read_bytes()) for name in
                            ["music_screen_worker.py", "music_vad_worker.py", "music_vocals.py"]}})
    root = store.root / "music-vocal-screen" / identity[:20]
    result = root / "screen.json"
    if result.exists():
        saved = read(result)
        if saved["audio_sha256"] != audio_hash:
            raise ValueError("Cached vocal screen belongs to a different score")
        return saved
    root.mkdir(parents=True, exist_ok=True)
    vad_path = root / "speech-activity.json"
    store.event("music-vocals", "running", {"audio": str(audio), "task": "Speech activity before stable-ts"})
    if not vad_path.exists():
        python = config.get("music_vad_python", "/home/nitin/story_generator/.whisperx-env/bin/python3")
        subprocess.run([python, "-u", "-m", "infofilm.music_vad_worker", str(audio), str(vad_path)],
                       cwd=ROOT, check=True, timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    vad = read(vad_path)
    transcript = _stable_transcript(config, store, root, audio) if vad["regions"] else {}
    value = {**classify_vocals(vad, transcript), "engine": "speech activity + stable-ts",
        "policy": POLICY, "audio_sha256": audio_hash, "speech_activity": vad,
        "stable_ts_used": bool(vad["regions"]), "stable_ts": transcript,
        "limitation": "Speech detection and ASR can miss humming or quiet singing. This is an automated screen; musical quality and voice absence need listening review."}
    write(result, value)
    store.event("music-vocals", "succeeded", {"status": value["status"], "detected_seconds": vad["detected_seconds"],
        "stable_ts_used": value["stable_ts_used"], "supported_tokens": value.get("supported_tokens", [])})
    return value
