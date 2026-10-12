"""Premium image tier through a cloud model on OpenRouter (e.g. google/gemini-3.1-flash-image).

The model is chosen on the LLM settings page ("Premium images") and frozen into the job as
`premium_image_endpoint`; the key is read at call time. Same checkpoint rules as the local Flux tier: the request
record is written before the call, a saved result is reused, and an unconfirmed submission is reported rather than
blindly repeated (cloud generations are billed).
"""
from __future__ import annotations

import base64
import io
import math
import time
from pathlib import Path

import httpx
from PIL import Image

from .core import digest, read, write

RATIOS = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "3:2": 3 / 2, "2:3": 2 / 3,
          "21:9": 21 / 9}
TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def aspect_ratio(width, height):
    target = width / height
    return min(RATIOS, key=lambda name: abs(math.log(RATIOS[name] / target)))


def cover(raw, size):
    """Scale to cover the delivery frame, then centre-crop to its exact size."""
    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB")
        scale = max(size[0] / image.width, size[1] / image.height)
        resized = image.resize((math.ceil(image.width * scale), math.ceil(image.height * scale)), Image.Resampling.LANCZOS)
        left, top = (resized.width - size[0]) // 2, (resized.height - size[1]) // 2
        return resized.crop((left, top, left + size[0], top + size[1])), [image.width, image.height]


def render_cloud(config, store, root, attempt, params):
    from .llm_settings import api_key
    root = Path(root)
    candidate, provider = root / f"candidate-{attempt}.png", root / f"provider-{attempt}.json"
    result_path = root / f"provider-result-{attempt}.json"
    endpoint = config.get("premium_image_endpoint")
    if not endpoint:
        raise RuntimeError("No premium image model is set: choose one under LLM settings → Premium images")
    target = (int(params["width"]), int(params["height"]))
    references = params.get("reference_images") or []
    record = {"model": endpoint["model"], "connection": endpoint.get("id"), "prompt": params["prompt"],
              "aspect_ratio": aspect_ratio(*target), "reference_sha256": [r["sha256"] for r in references]}
    if result_path.exists() and candidate.exists():
        result = read(result_path)
        if digest(candidate.read_bytes()) != result.get("sha256"):
            raise ValueError("Cloud image candidate checksum changed")
        return result
    if provider.exists():
        saved = read(provider)
        if saved.get("request") != record:
            raise ValueError("Saved cloud image request settings differ")
        if saved.get("state") == "submission_started":
            raise RuntimeError(f"Unconfirmed cloud image request at {provider}; it may have been billed. Delete the file to retry.")
    key = api_key(config.get("llm_secrets_root"), endpoint.get("id"), endpoint)
    if not key:
        raise RuntimeError(f"The premium image connection {endpoint.get('label')} needs an API key (LLM settings)")
    content = [{"type": "text", "text": params["prompt"] + (
        "\n\nThe attached labelled reference cards show the real appearance of the named people; match their faces, "
        "but do not reproduce the cards, labels or text." if references else "")}]
    if references:
        from .person_references import validate_reference
        for ref in references:
            data = base64.b64encode(validate_reference(ref).read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + data}})
    body = {"model": endpoint["model"], "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"], "image_config": {"aspect_ratio": record["aspect_ratio"]}}
    write(provider, {"state": "submission_started", "request": record, "submitted_at": time.time()})
    store.event("cloud-image", "rendering", {"attempt": attempt + 1, "model": endpoint["model"],
                                             "aspect_ratio": record["aspect_ratio"], "references": len(references)})
    start, delay, deadline = time.monotonic(), 5, time.monotonic() + config.get("provider_wait_seconds", 1800)
    with httpx.Client(timeout=endpoint.get("timeout", 300), headers={"Authorization": "Bearer " + key}) as client:
        while True:
            try:
                response = client.post(endpoint["url"].rstrip("/") + "/chat/completions", json=body)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                # The request never reached the provider: safe to retry.
                if time.monotonic() > deadline:
                    raise
                store.event("cloud-image", "transient_retry", {"reason": type(exc).__name__, "wait_seconds": delay})
                time.sleep(delay); delay = min(delay * 2, 120)
                continue
            if response.status_code in TRANSIENT and time.monotonic() < deadline:
                store.event("cloud-image", "transient_retry", {"reason": f"HTTP {response.status_code}", "wait_seconds": delay})
                time.sleep(delay); delay = min(delay * 2, 120)
                continue
            break
    elapsed = round(time.monotonic() - start, 3)
    if response.is_error:
        write(provider, {"state": "failed", "request": record, "http_status": response.status_code})
        write(result_path, {"error": response.text[:2000], "http_status": response.status_code, "generation_seconds": elapsed})
        response.raise_for_status()
    raw_response = response.json()
    message = (raw_response.get("choices") or [{}])[0].get("message", {})
    images = message.get("images") or []
    url = images[0].get("image_url", {}).get("url", "") if images else ""
    if not url.startswith("data:image/"):
        write(provider, {"state": "failed", "request": record})
        write(result_path, {"error": "The model returned no image", "text": (message.get("content") or "")[:1000],
                            "generation_seconds": elapsed})
        raise RuntimeError(f"{endpoint['model']} returned no image")
    raw = base64.b64decode(url.split(",", 1)[1], validate=True)
    image, native = cover(raw, target)
    root.mkdir(parents=True, exist_ok=True)
    temporary = candidate.with_suffix(".tmp.png")
    image.save(temporary)
    temporary.replace(candidate)
    usage = raw_response.get("usage") or {}
    result = {"provider": "openrouter_image", "model": endpoint["model"], "generation_seconds": elapsed,
              "native_dimensions": native, "delivery_dimensions": list(target), "crop": "scale to cover; centre crop",
              "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "cost")},
              "sha256": digest(candidate.read_bytes())}
    write(result_path, result)
    write(provider, {"state": "complete", "request": record, "submitted_at": read(provider)["submitted_at"]})
    return result
