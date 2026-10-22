"""LuxTTS batch worker. Runs under the LuxTTS environment (not infofilm's), so it imports nothing from infofilm.

  <lux python> lux_worker.py manifest.json

The manifest names the model, device, reference recording (and its transcript, when known), speed, seed and the
clips to write. Each clip is written as mono 16-bit PCM WAV at 48 kHz, atomically, and skipped if it already exists.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch


def main(path):
    manifest = json.loads(Path(path).read_text())
    sys.path.insert(0, manifest["lux_root"])
    from zipvoice.luxvoice import LuxTTS

    device, threads = manifest.get("device", "cpu"), int(manifest.get("threads", 8))
    if device == "cpu":
        torch.set_num_threads(threads)
    started = time.monotonic()
    tts = LuxTTS(manifest["model"], device=device, threads=threads)
    if manifest.get("reference_text"):
        # The narrator's transcript is known exactly; use it rather than LuxTTS's own Whisper guess.
        known = manifest["reference_text"]
        tts.transcriber = lambda _audio: {"text": known}
    prompt = tts.encode_prompt(manifest["reference"], rms=manifest.get("rms", 0.01), duration=1000)
    print(json.dumps({"event": "ready", "seconds": round(time.monotonic() - started, 2)}), flush=True)
    for task in manifest["tasks"]:
        out = Path(task["out"])
        if out.is_file():
            continue
        torch.manual_seed(int(manifest.get("seed", 0)) * 1000 + int(task.get("index", 0)))
        t = time.monotonic()
        wav = tts.generate_speech(task["text"], prompt, num_steps=manifest.get("num_steps", 4),
                                  t_shift=manifest.get("t_shift", 0.5), speed=manifest["speed"]).numpy().squeeze()
        wav = np.clip(wav.astype(np.float32), -1, 1)
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        sf.write(part, wav, 48000, subtype="PCM_16", format="WAV")
        part.replace(out)
        print(json.dumps({"event": "clip", "id": task["id"], "seconds": round(len(wav) / 48000, 3),
                          "took": round(time.monotonic() - t, 3)}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
