"""ImageStudio synchronous image API adapter; prompts stay positive-only."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
from PIL import Image

from .core import digest, read, write, gpu_lease

ENGINES = {"zimage", "ernie-turbo", "flux2", "flux2-klein"}


def user_systemd_environment():
    """System-service workers need the local user's bus for --user units."""
    env = os.environ.copy()
    runtime = env.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    env['XDG_RUNTIME_DIR'] = runtime
    if not env.get('DBUS_SESSION_BUS_ADDRESS'):
        env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={runtime}/bus'
    return env


@contextlib.contextmanager
def park_idle_gpu_services(config, store):
    """Drop CUDA contexts as well as models, restoring initially active services."""
    urls = [config["tts_url"].rstrip("/") + "/health", config["qwen_url"].rstrip("/") + "/api/status"]
    if not config["tts_url"].rstrip("/") in {"http://127.0.0.1:8092", "http://localhost:8092"} or not config["qwen_url"].rstrip("/") in {"http://127.0.0.1:8075", "http://localhost:8075"}:
        raise ValueError("Parking supports only the installed local VibeVoice and Qwen services")
    with httpx.Client(timeout=30) as client:
        responses = [client.get(url) for url in urls]
        for response in responses:
            response.raise_for_status()
        vibe, qwen = [response.json() for response in responses]
    if vibe.get("active_job") or vibe.get("queued_or_running_jobs"):
        raise RuntimeError("VibeVoice has active work; refusing to park it")
    if qwen.get("busy") or qwen.get("state") in {"loading", "unloading"}:
        raise RuntimeError("Qwen Image has active work; refusing to park it")
    units = [(["systemctl", "--user"], "qimage21.service", urls[1]),
             (["systemctl", "--no-ask-password"], "vibevoice-api-local.service", urls[0])]
    user_env = user_systemd_environment()
    stopped = []
    try:
        for manager, unit, url in units:
            options = {'env': user_env} if '--user' in manager else {}
            status = subprocess.run(manager + ["is-active", "--quiet", unit],
                                    timeout=30, capture_output=True, text=True, **options)
            if status.returncode not in {0, 3, 4}:
                diagnostic = (status.stderr or status.stdout or '').strip()
                raise RuntimeError(f"Cannot reach or query the service manager for {unit}: {diagnostic or 'systemctl failed'}. "
                                   "No GPU service switch was attempted for this unit.")
            if status.returncode != 0:
                raise RuntimeError(f"Provider API exists but its expected service {unit} is not active")
        # Check both managers before parking either service. System-unit
        # start/stop is authorized by the narrow installed Polkit rule; sudo
        # cannot elevate workers that inherit NoNewPrivileges from the web app.
        for manager, unit, url in units:
            options = {'env': user_env} if '--user' in manager else {}
            stopped.append((manager, unit, url))
            subprocess.run(manager + ["stop", unit], check=True, timeout=90, **options)
            store.event("gpu", "service_parked", {"unit": unit})
        remaining = subprocess.check_output(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"], text=True, timeout=15).strip()
        if remaining:
            raise RuntimeError("Other CUDA processes remain; leave them undisturbed: " + remaining)
        store.event("gpu", "cleared", {"compute_processes": 0})
        yield
    finally:
        failures = []
        for manager, unit, url in reversed(stopped):
            try:
                options = {'env': user_env} if '--user' in manager else {}
                subprocess.run(manager + ["start", unit], check=True, timeout=90, **options)
                deadline = time.monotonic() + 60
                while True:
                    try:
                        response = httpx.get(url, timeout=5)
                        response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"Restored {unit}, but its API is not ready")
                        time.sleep(1)
                store.event("gpu", "service_restored", {"unit": unit})
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError("Could not fully restore parked providers: " + "; ".join(failures))


def studio_payload(config, params):
    multiple = config.get("image_dimension_multiple", 64)
    width, height = int(params["width"]), int(params["height"])
    payload = {"engine": config["image_engine"], "prompt": params["prompt"],
            "width": ((width + multiple - 1) // multiple) * multiple,
            "height": ((height + multiple - 1) // multiple) * multiple,
            "steps": int(params["steps"]), "cfgScale": config.get("image_cfg_scale", 1.0),
            "seed": int(params["seed"]), "sampler": "euler", "style": "custom",
            # /api/generate injects "text, watermark" even for custom style.
            # Its SD-compatible endpoint explicitly honors the empty override.
            "negative_prompt": ""}
    if params.get('reference_images'):
        if config['image_engine'] not in {'flux2', 'flux2-klein'}:
            raise ValueError('This ImageStudio engine cannot accept person reference cards')
        from .person_references import validate_reference
        payload['referenceImages'] = [base64.b64encode(validate_reference(ref).read_bytes()).decode('ascii')
                                      for ref in params['reference_images']]
    return payload


def release_qwen(config):
    """InfoFilm holds its GPU lease; refuse to interrupt another provider job."""
    with httpx.Client(timeout=180) as client:
        url = config["qwen_url"].rstrip("/")
        response = client.get(url + "/api/status")
        response.raise_for_status()
        state = response.json()
        if state.get("busy") or state.get("state") in {"loading", "unloading"}:
            raise RuntimeError("Qwen Image is busy; refusing to switch its GPU")
        if state.get("state") == "loaded":
            response = client.post(url + "/api/unload")
            response.raise_for_status()
            state = client.get(url + "/api/status").json()
            if state.get("state") != "unloaded":
                raise RuntimeError("Qwen Image did not release the GPU")


def save_image(raw, candidate, native_size, target_size):
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        if image.size != tuple(native_size):
            raise ValueError(f"ImageStudio returned {image.size}, expected {native_size}")
        native = candidate.with_name(candidate.stem + "-native.png")
        native.write_bytes(raw)
        left = (image.width - target_size[0]) // 2
        top = (image.height - target_size[1]) // 2
        if left < 0 or top < 0:
            raise ValueError("Native image is smaller than the delivery frame")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        temporary = candidate.with_suffix(".tmp.png")
        image.convert("RGB").crop((left, top, left + target_size[0], top + target_size[1])).save(temporary)
        temporary.replace(candidate)


def render_studio(config, store, root, attempt, params):
    """Checkpoint before POST; reconcile unknown submissions, never blindly repeat. Flux runs on the local GPU, so it
    holds the "flux" lease even when Qwen images (remote) do not take the GPU lease."""
    shared = config.get("shared_gpu_services")
    separate = shared is not None and "images" not in shared      # else the image stage already holds the lease
    with gpu_lease(config, "flux") if separate else contextlib.nullcontext():
        return _render_studio(config, store, root, attempt, params)


def _render_studio(config, store, root, attempt, params):
    root = Path(root)
    candidate = root / f"candidate-{attempt}.png"
    provider = root / f"provider-{attempt}.json"
    result_path = root / f"provider-result-{attempt}.json"
    payload = studio_payload(config, params)
    # Persist reference hashes/paths, not inline photo bytes.
    recorded_payload = {k: v for k, v in payload.items() if k != 'referenceImages'}
    if params.get('reference_images'):
        recorded_payload['reference_sha256'] = [ref['sha256'] for ref in params['reference_images']]
    target_size = (int(params["width"]), int(params["height"]))
    with httpx.Client(base_url=config["image_url"], timeout=1800) as client:
        if provider.exists() and read(provider).get('payload') != recorded_payload:
            raise ValueError('Saved ImageStudio submission settings differ')
        if result_path.exists():
            result = read(result_path)
            if result.get("error"):
                raise RuntimeError(result["error"])
            if candidate.exists():
                if digest(candidate.read_bytes()) != result.get("sha256"):
                    raise ValueError("ImageStudio candidate checksum changed")
                return result
            response = client.get("/generated/" + result["filename"])
            response.raise_for_status()
            save_image(response.content, candidate, (payload["width"], payload["height"]), target_size)
            result["sha256"] = digest(candidate.read_bytes())
            write(result_path, result)
            return result
        if provider.exists():
            record = read(provider)
            if record.get("payload") != recorded_payload:
                raise ValueError("Saved ImageStudio submission settings differ")
            history = client.get("/api/history")
            history.raise_for_status()
            matches = [item for item in history.json()["history"]
                       if all(item.get(k) == payload[k] for k in ("engine", "prompt", "seed", "width", "height", "steps"))
                       and (not params.get('reference_images') or
                            [ref.get('sha256') for ref in item.get('references', [])] == recorded_payload['reference_sha256'])
                       and item.get("createdAt", 0) >= record["submitted_at"] - 1]
            if len(matches) != 1:
                raise RuntimeError(f"Unknown ImageStudio submission outcome at {provider}; reconcile before resubmitting")
            info = matches[0]
            response = client.get(info["imageUrl"])
            response.raise_for_status()
            raw = response.content
            elapsed = info["elapsedMs"] / 1000
        else:
            write(provider, {"state": "submission_started", "params": params, "payload": recorded_payload,
                             "submitted_at": time.time()})
            store.event("studio-" + config["image_engine"], "rendering", {"attempt": attempt + 1,
                "seed": payload["seed"], "native_dimensions": [payload["width"], payload["height"]]})
            start = time.monotonic()
            response = client.post("/sdapi/v1/txt2img", json=payload)
            elapsed = round(time.monotonic() - start, 3)
            if response.is_error:
                write(result_path, {"error": response.text, "http_status": response.status_code,
                                    "generation_seconds": elapsed})
                response.raise_for_status()
            value = response.json()
            info = json.loads(value["info"])
            raw = base64.b64decode(value["images"][0].split(",")[-1], validate=True)
        if info["engine"] != config["image_engine"]:
            raise ValueError("ImageStudio used the wrong engine")
        # Store the recoverable server filename before decoding/cropping.
        result = {**info, "filename": info.get("filename", info["jobId"] + ".png"),
                  "generation_seconds": elapsed, "server_seconds": info["elapsedMs"] / 1000,
                  "native_dimensions": [payload["width"], payload["height"]],
                  "delivery_dimensions": list(target_size), "crop": "center; no resizing"}
        write(result_path, result)
        save_image(raw, candidate, result["native_dimensions"], target_size)
        result["sha256"] = digest(candidate.read_bytes())
        write(result_path, result)
        write(provider, {**read(provider), "state": "complete", "job_id": info["jobId"]})
        return result
