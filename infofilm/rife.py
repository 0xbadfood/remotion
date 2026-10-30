"""RIFE frame interpolation for clips made on video workers without it (the Spark's LTX API), so every clip in a
film gets the same in-between frames as the amdserver's ComfyUI graph (RIFE 4.7, x2, fast mode, ensemble).

Vendored network: vendor/rife/rife_arch.py (MIT, ComfyUI-Frame-Interpolation); checkpoint models/rife/rife47.pth.
Runs as a child process (`python -m infofilm.rife in.mp4 out.mp4 [multiplier]`) so its GPU memory is freed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = ROOT / "models" / "rife" / "rife47.pth"


def interpolate(source, target, multiplier=2, timeout=900):
    """Write `target` with (multiplier - 1) RIFE frames between each pair of `source` frames, at the source's fps."""
    subprocess.run([sys.executable, "-m", "infofilm.rife", str(source), str(target), str(multiplier)],
                   cwd=ROOT, check=True, timeout=timeout, capture_output=True)
    return Path(target)


def _probe(path):
    out = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(path)], text=True).strip()
    width, height, rate = out.split(",")
    return int(width), int(height), rate


def main(source, target, multiplier=2):
    import numpy as np
    import torch
    sys.path.insert(0, str(ROOT / "vendor" / "rife"))
    from rife_arch import IFNet
    width, height, rate = _probe(source)
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         check=True, capture_output=True).stdout
    frames = torch.from_numpy(np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3).copy())
    frames = frames.permute(0, 3, 1, 2).float().div(255)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = IFNet(arch_ver="4.7")
    model.load_state_dict(torch.load(CHECKPOINT, map_location="cpu", weights_only=False))
    model.eval().to(device)
    encoder = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                                f"{width}x{height}", "-r", rate, "-i", "-", "-c:v", "libx264", "-crf", "18",
                                "-pix_fmt", "yuv420p", str(target)], stdin=subprocess.PIPE)
    write = lambda t: encoder.stdin.write((t.clamp(0, 1) * 255).round().byte().permute(0, 2, 3, 1).cpu().numpy().tobytes())  # noqa: E731
    steps = [step / multiplier for step in range(1, multiplier)]
    with torch.inference_mode():
        for i in range(len(frames) - 1):
            a = frames[i:i + 1].to(device)
            b = frames[i + 1:i + 2].to(device)
            write(a)
            if steps:
                count = len(steps)
                t = torch.tensor(steps, device=device).view(-1, 1, 1, 1)
                write(model(a.expand(count, -1, -1, -1), b.expand(count, -1, -1, -1), t, [8, 4, 2, 1], True, True))
        write(frames[-1:].to(device))
    encoder.stdin.close()
    if encoder.wait():
        raise RuntimeError("ffmpeg could not write the interpolated clip")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 2)
