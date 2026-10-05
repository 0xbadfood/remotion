"""One-shot native ACE-Step process. Process exit releases all music GPU state."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    sys.path.insert(0, request["acestep_root"])
    import torch
    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationConfig, GenerationParams, generate_music

    started = time.monotonic()
    handler = AceStepHandler()
    message, ok = handler.initialize_service(project_root=request["acestep_root"],
        config_path=request["model"], device="cuda", use_flash_attention=False,
        compile_model=False, offload_to_cpu=False)
    if not ok:
        raise RuntimeError(message)
    torch.cuda.reset_peak_memory_stats()
    params = GenerationParams(caption=request["caption"], lyrics="[Instrumental]",
        duration=request["duration_seconds"], bpm=request["bpm"], keyscale=request.get("keyscale", ""),
        timesignature=request.get("timesignature", "4"), inference_steps=request["steps"],
        thinking=False, use_cot_caption=False, use_cot_metas=False, use_cot_language=False)
    generated = generate_music(handler, None, params,
        GenerationConfig(batch_size=1, audio_format="wav", use_random_seed=False, seeds=[request["seed"]]),
        save_dir=str(args.request.parent / "native"))
    if not generated.success or not generated.audios:
        raise RuntimeError(generated.error or generated.status_message)
    source = Path(generated.audios[0]["path"])
    target = args.request.parent / "music.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
        "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(target)], check=True)
    report = {"audio": str(target), "model": request["model"], "seed": request["seed"],
        "latency_seconds": round(time.monotonic() - started, 3),
        "peak_allocated_gib": round(torch.cuda.max_memory_allocated() / 2**30, 3),
        "peak_reserved_gib": round(torch.cuda.max_memory_reserved() / 2**30, 3),
        "lora": None, "internal_lm": False, "status_message": generated.status_message}
    (args.request.parent / "worker-result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
