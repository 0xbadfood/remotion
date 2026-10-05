"""Inspect an isolated music vocal stem with the installed stable-ts runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    root = args.request.parent
    started = time.monotonic()
    source = Path(request["audio"])
    vocal = source
    separation = None
    if request.get("with_demucs", False):
        vocal, separation = isolate_vocals(request, root, source)
    import numpy as np
    import stable_whisper
    import torch
    torch.set_num_threads(4)
    def rms(path):
        raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
            "-ar", "16000", "-ac", "1", "-f", "f32le", "-"], capture_output=True, check=True).stdout
        values = np.frombuffer(raw, dtype="<f4").astype(np.float64)
        return float(np.sqrt(np.mean(np.square(values))))
    original_rms, isolated_rms = rms(source), rms(vocal)
    model = stable_whisper.load_model(request["model_file"], device="cuda")
    result = model.transcribe(str(vocal), language="en", condition_on_previous_text=False,
        word_timestamps=True, suppress_silence=True, suppress_word_ts=True,
        vad=False, temperature=0, regroup=False, verbose=False)
    payload = result.to_dict()
    (root / "stable_ts.json").write_text(json.dumps(payload, indent=2))
    with source.open("rb") as stream:
        audio_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {"engine": "stable-ts large-v3", "audio_sha256": audio_hash,
        "asr_audio": str(vocal), "demucs_used": separation is not None,
        "vocal_rms": isolated_rms if separation else None, "music_rms": original_rms,
        "vocal_to_music_rms_ratio": isolated_rms / max(original_rms, 1e-12) if separation else None,
        "transcript": payload.get("text", ""), "segments": payload.get("segments", []),
        "model_file": request["model_file"], "demucs_command": separation,
        "elapsed_seconds": round(time.monotonic()-started, 3)}
    temporary = root / "worker-result.pending.json"
    temporary.write_text(json.dumps(report, indent=2))
    temporary.replace(root / "worker-result.json")
    print(json.dumps({k: report[k] for k in ["transcript", "demucs_used", "elapsed_seconds"]}), flush=True)


def isolate_vocals(request, root, source):
    # Use an entirely local Demucs repository, with existing cached weights.
    repo = root / "demucs-repo"
    repo.mkdir(exist_ok=True)
    signatures = ["f7e0c4bc", "d12395a8", "92cfc3b6", "04573f0d"]
    for signature in signatures:
        matches = list(Path(request["demucs_cache"]).glob(signature + "-*.th"))
        if not matches:
            raise RuntimeError(f"Cached Demucs weight is missing: {signature}")
        target = repo / matches[0].name
        if not target.exists():
            target.symlink_to(matches[0])
    (repo / "htdemucs_ft.yaml").write_text("models: ['f7e0c4bc', 'd12395a8', '92cfc3b6', '04573f0d']\n"
        "weights: [[1., 0., 0., 0.], [0., 1., 0., 0.], [0., 0., 1., 0.], [0., 0., 0., 1.]]\n")
    vocal = root / "stems/htdemucs_ft" / source.stem / "vocals.wav"
    separation = [request["demucs_bin"], "--repo", str(repo), "--two-stems=vocals", "-n", "htdemucs_ft",
        "--device", "cuda", "--shifts", "0", "--float32", "-j", "1", "-o", str(root / "stems"), str(source)]
    if not vocal.is_file():
        lease_fd = os.environ.get("INFOFILM_GPU_LEASE_FD")
        inherited = (int(lease_fd),) if lease_fd else ()
        # Existing official Demucs checkpoints include their model class; newer
        # PyTorch defaults reject that legacy package unless explicitly enabled.
        subprocess.run(separation, check=True, pass_fds=inherited,
            env={**os.environ, "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "1"})
    return vocal, separation


if __name__ == "__main__":
    main()
