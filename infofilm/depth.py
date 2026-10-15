"""Depth maps for scene images, for the renderer's 2.5D camera (animation pass 2, user, 2026-10-15).

One cheap pass after the images: Depth Anything V2 Small (Apache-2.0; the larger sizes are non-commercial) gives each
accepted image a relative depth map (white = near), softened so that the shader warps edges smoothly instead of
tearing them (Italy test: a sliver beside a horse's leg at sharp depth edges disappeared after the blur). The model
runs in a child process so its GPU memory is freed afterwards. About 0.05 s per image on the Spark; a film needs
seconds. Advisory: any failure leaves the scenes on the flat camera, and a depth map never stops a film. A finished
film can get depth by resuming it from its scenes (images, prompts and audio are reused) and rendering again.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .core import digest, read, write

PROTOCOL = "infofilm.image-depth.v1"
MODEL = "depth-anything/Depth-Anything-V2-Small-hf"
SOFTEN = .004          # Gaussian blur radius as a share of the image width (6 px at 1536)


def manifest_path(store):
    return Path(store.root) / "depth" / "manifest.json"


def current(store, images):
    """{asset_id: {file, sha256}} for depth maps that match the accepted images exactly."""
    path = manifest_path(store)
    if not path.exists():
        return {}
    saved = read(path).get("maps", {})
    out = {}
    for asset_id, image in images.items():
        value = saved.get(asset_id)
        if (value and value.get("source_sha256") == image.get("sha256") and Path(value["file"]).is_file()
                and digest(Path(value["file"]).read_bytes()) == value["sha256"]):
            out[asset_id] = value
    return out


def depth_maps(config, store, images):
    """Depth maps for every accepted image, computed once (cached by image hash). Returns what exists."""
    if not config.get("image_depth", True) or not images:
        return {}
    have = current(store, images)
    missing = {k: v["file"] for k, v in images.items() if k not in have}
    if not missing:
        return have
    root = Path(store.root) / "depth"
    request = write(root / "request.json", {"protocol": PROTOCOL, "model": MODEL, "soften": SOFTEN,
                                            "images": missing, "directory": str(root)})
    try:
        store.event("depth", "running", {"images": len(missing), "model": MODEL})
        with (root / "worker.log").open("w") as log:
            subprocess.run([config.get("depth_python", sys.executable), "-m", "infofilm.depth", str(request)],
                           cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=subprocess.STDOUT, check=True,
                           timeout=config.get("depth_timeout_seconds", 900))
        made = read(root / "result.json")["maps"]
        saved = read(manifest_path(store)).get("maps", {}) if manifest_path(store).exists() else {}
        for asset_id, value in made.items():
            saved[asset_id] = {**value, "source_sha256": images[asset_id]["sha256"], "model": MODEL, "protocol": PROTOCOL}
        write(manifest_path(store), {"protocol": PROTOCOL, "maps": saved})
        have = current(store, images)
        store.event("depth", "succeeded", {"images": len(have)})
    except Exception as exc:                    # noqa: BLE001 - advisory: flat camera for the missing scenes
        store.event("depth", "failed", {"error": f"{type(exc).__name__}: {exc}"[:300]})
    return have


def main(request_path):
    """Child process: one model load, every image, softened 8-bit maps at the image's own size."""
    import torch
    from PIL import Image, ImageFilter
    from transformers import pipeline
    request = read(request_path)
    root = Path(request["directory"])
    pipe = pipeline("depth-estimation", model=request["model"], device=0 if torch.cuda.is_available() else -1)
    maps = {}
    for asset_id, file in request["images"].items():
        image = Image.open(file).convert("RGB")
        depth = pipe(image)["depth"].convert("L").resize(image.size)
        depth = depth.filter(ImageFilter.GaussianBlur(max(1, round(image.size[0] * request["soften"]))))
        target = root / f"{asset_id}-{digest(Path(file).read_bytes())[:16]}.png"
        depth.save(target)
        maps[asset_id] = {"file": str(target), "sha256": digest(target.read_bytes())}
    write(root / "result.json", {"protocol": request["protocol"], "maps": maps})


if __name__ == "__main__":
    main(sys.argv[1])
