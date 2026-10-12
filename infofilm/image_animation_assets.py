"""CLI-only image animation assets, with genuine alpha and immutable checkpoints.

Public entry points are ``discover_capabilities(config)``,
``prepare_asset_requests(requests, directory, config)``,
``generate_assets(config, store, requests_manifest, execute=False)`` and
``import_assets(import_manifest_path, requests_manifest, cache_dir)``. ``directory``
and ``cache_dir`` are the experiment root; images live under ``assets/`` there.
Preparing and importing never contact a provider. Generation requires explicit
``execute=True``. No function parks/restarts services or converts RGB into alpha.

Requests have id, kind (canvas/background/part), prompt, width, height and optional
reference_ids, transparent_required, required_landmarks, landmarks, grounding and
controller. Coordinates are source-image pixels. Manual imports use relative PNG
paths beside their JSON, and retain the request_sha256 from the generated template.
"""
from __future__ import annotations

import contextlib
import io
import math
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from . import media
from .core import digest, gpu_lease, read, write
from .image_providers import provider_config
from .image_recovery import render_take

REQUEST_SCHEMA = "infofilm.image-animation-requests.v1"
ASSET_SCHEMA = "infofilm.image-animation-assets.v1"
IMPORT_SCHEMA = "infofilm.image-animation-import.v1"
CAPABILITY_SCHEMA = "infofilm.image-animation-capabilities.v1"
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


def _provider(config):
    """Use the root registry's Qwen endpoint/settings, never an ad hoc GPU service."""
    registry = config.get("image_provider_registry")
    provider_id = config.get("image_animation_provider", "qwen_local")
    current = dict(config)
    if registry:
        if provider_id not in registry["providers"]:
            raise ValueError(f"Unknown image animation provider: {provider_id}")
        provider = registry["providers"][provider_id]
        if provider.get("adapter") != "qwen_async":
            raise ValueError("Image animation generation currently requires the Qwen async adapter")
        current = provider_config(current, provider_id, provider)
    elif config.get("image_engine"):
        raise ValueError("Image animation generation currently requires the Qwen async adapter")
    current.pop("image_engine", None)
    current["image_provider_id"] = provider_id
    if not isinstance(current.get("image_url"), str):
        raise ValueError("Image animation needs the root image_url configuration")
    current["image_url"] = current["image_url"].rstrip("/")
    current.setdefault("image_steps", 25)
    if type(current["image_steps"]) is not int or not 1 <= current["image_steps"] <= 80:
        raise ValueError("image_steps must be 1..80 for Qwen")
    return current


def _resolved_schema(document, schema):
    seen = set()
    while isinstance(schema, dict) and "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/") or reference in seen:
            return {}
        seen.add(reference)
        schema = document
        for piece in reference[2:].split("/"):
            schema = schema.get(piece.replace("~1", "/").replace("~0", "~"), {})
    return schema if isinstance(schema, dict) else {}


def _local_rgba_evidence(config, status):
    """Read installed model/runtime evidence; do not import torch or instantiate it.

    QwenImage21 has a four-channel VAE and returns native RGBA without an API
    transparency flag. Absence of a flag alone therefore cannot imply no alpha.
    Local inspection is restricted to the endpoint's reported installation.
    """
    if urlparse(config["image_url"]).hostname not in {"localhost", "127.0.0.1", "::1"}:
        return []
    model_path = status.get("model_path")
    if not isinstance(model_path, str) or not Path(model_path).is_absolute():
        return []
    root = Path(model_path)
    index, vae, server = root / "model_index.json", root / "vae/config.json", root.parent / "app/main.py"
    try:
        model, channels = read(index), read(vae)
        server_source = server.read_text()
    except (OSError, ValueError):
        return []
    if (model.get("_class_name") != "QwenImage21Pipeline" or channels.get("out_channels") != 4
            or 'output.save(path, format=save_format' not in server_source
            or 'suffix = params["output_format"]' not in server_source):
        return []
    software = status.get("software", {})
    executable = software.get("python_executable", "")
    if not isinstance(executable, str) or not Path(executable).is_absolute():
        return []
    environment = Path(executable).parent.parent
    version = str(software.get("python", "")).split(".")[:2]
    if len(version) != 2 or any(not value.isdigit() for value in version):
        return []
    pipeline = (environment / "lib" / ("python" + ".".join(version)) / "site-packages/diffusers"
                / "pipelines/qwenimage21/pipeline_qwenimage21.py")
    try:
        source = pipeline.read_text()
    except OSError:
        return []
    if "self.vae.decode(latents" not in source or "self.image_processor.postprocess(image" not in source:
        return []
    return [{"file": str(path), "sha256": digest(path.read_bytes()), "finding": finding}
            for path, finding in [(index, "QwenImage21Pipeline"), (vae, "native four-channel VAE output"),
                                  (pipeline, "native decoded channels preserved by PIL postprocess"),
                                  (server, "PNG saves pipeline output without RGB flattening")]]


def discover_capabilities(config, *, request_fn=None):
    """Read GET /openapi.json and GET /api/status; never change service state.

    Native alpha is supported by an explicit API control, an explicit OpenAPI
    x-native-rgba declaration, or verified local QwenImage21 runtime evidence.
    Every generated part must still pass actual alpha-content validation.
    """
    current = _provider(config)
    request_fn = request_fn or media.request
    document = request_fn(current, "image", "GET", "/openapi.json")
    status = request_fn(current, "image", "GET", "/api/status")
    operation = document.get("paths", {}).get("/api/generate", {}).get("post", {})
    content = operation.get("requestBody", {}).get("content", {})
    body = content.get("multipart/form-data", content.get("application/x-www-form-urlencoded", {}))
    properties = _resolved_schema(document, body.get("schema", {})).get("properties", {})
    alpha_parameter = next((key for key in ("transparent_background", "use_alpha", "generate_alpha")
                            if key in properties and properties[key].get("type") == "boolean"), None)
    evidence = _local_rgba_evidence(current, status)
    native_alpha = bool(alpha_parameter or operation.get("x-native-rgba") is True or evidence)
    return {"schema_version": CAPABILITY_SCHEMA, "provider_id": current["image_provider_id"],
            "url": current["image_url"], "openapi_sha256": digest(document),
            "inspected_at_unix": time.time(),
            "form_fields": sorted(properties), "text_generation": "prompt" in properties,
            "referenced_edit": "references" in properties, "max_references": 10,
            "native_alpha": native_alpha, "alpha_parameter": alpha_parameter,
            "alpha_request_mode": "parameter" if alpha_parameter else "prompt_native_rgba" if native_alpha else "unsupported",
            # This installed pipeline rounds to 32 even though the API admits 16.
            "dimension_multiple": 32 if evidence else 16,
            "min_dimension": int(properties.get("width", {}).get("minimum", 512)),
            "max_dimension": int(properties.get("width", {}).get("maximum", 2816)),
            "runtime_evidence": evidence, "service_status": status,
            "limitations": ["Alpha content, composition and part identity still require output verification."]
            + ([] if native_alpha else ["No verified native-alpha generation route; import genuine transparent PNG assets."])}


def _landmarks(value, width, height, required=()):
    if not isinstance(value, dict):
        raise ValueError("landmarks must map names to source-pixel [x, y] points")
    result = {}
    for name, point in value.items():
        if (not isinstance(name, str) or not SAFE_ID.fullmatch(name) or not isinstance(point, (list, tuple))
                or len(point) != 2 or any(type(n) not in {int, float} or not math.isfinite(n) for n in point)
                or not 0 <= point[0] < width or not 0 <= point[1] < height):
            raise ValueError(f"Invalid source-pixel landmark: {name}")
        result[name] = list(point)
    if set(required) - set(result):
        raise ValueError("Missing required landmarks: " + ", ".join(sorted(set(required) - set(result))))
    return result


def _normalise_requests(requests, config):
    if not isinstance(requests, list) or not requests:
        raise ValueError("Image animation requests must be a nonempty array")
    output, identifiers = [], set()
    for original in requests:
        if not isinstance(original, dict):
            raise ValueError("Each asset request must be an object")
        item = dict(original)
        identifier = item.get("id")
        if not isinstance(identifier, str) or not SAFE_ID.fullmatch(identifier) or identifier in identifiers:
            raise ValueError("Asset requests need unique safe IDs")
        identifiers.add(identifier)
        if item.get("kind") not in {"canvas", "background", "part"}:
            raise ValueError(f"{identifier} kind must be canvas, background or part")
        if not isinstance(item.get("prompt"), str) or not 1 <= len(item["prompt"].strip()) <= 3700:
            raise ValueError(f"{identifier} needs a prompt of 1..3700 characters")
        item["prompt"] = item["prompt"].strip()
        for key in ("width", "height"):
            item.setdefault(key, config.get("image_" + key))
            if type(item[key]) is not int or not 1 <= item[key] <= 8192:
                raise ValueError(f"{identifier} {key} must be 1..8192 pixels")
        item.setdefault("transparent_required", item["kind"] == "part")
        if type(item["transparent_required"]) is not bool or (item["kind"] == "part" and not item["transparent_required"]):
            raise ValueError("Moving parts require transparent_required=true")
        item.setdefault("reference_ids", [])
        if (not isinstance(item["reference_ids"], list) or len(item["reference_ids"]) > 10
                or any(not isinstance(ref, str) or not SAFE_ID.fullmatch(ref) for ref in item["reference_ids"])
                or len(set(item["reference_ids"])) != len(item["reference_ids"])
                or identifier in item["reference_ids"]):
            raise ValueError(f"{identifier} needs at most ten distinct safe reference_ids")
        required = item.setdefault("required_landmarks", [])
        if (not isinstance(required, list) or any(not isinstance(name, str) or not SAFE_ID.fullmatch(name) for name in required)
                or len(set(required)) != len(required)):
            raise ValueError("required_landmarks must contain unique safe names")
        item["landmarks"] = _landmarks(item.get("landmarks", {}), item["width"], item["height"])
        for key in ("grounding", "controller"):
            item.setdefault(key, {})
            if not isinstance(item[key], dict):
                raise ValueError(f"{key} metadata must be an object")
        output.append(item)
    if any(ref not in identifiers for item in output for ref in item["reference_ids"]):
        raise ValueError("Every reference_id must name a requested asset")
    # Explicit dependency ordering handles imports and resumes independently of
    # caller list order, and rejects cycles before contacting a provider.
    ordered, remaining = [], list(output)
    while remaining:
        ready_ids = {item["id"] for item in ordered}
        available = [item for item in remaining if set(item["reference_ids"]) <= ready_ids]
        if not available:
            raise ValueError("Asset reference_ids contain a dependency cycle")
        ordered.extend(available)
        remaining = [item for item in remaining if item not in available]
    return ordered


def prepare_asset_requests(requests, directory, config, *, capabilities=None):
    """Save an immutable flat request package; this is entirely CPU/offline work."""
    current = _provider(config)
    directory = Path(directory).resolve()
    items = _normalise_requests(requests, current)
    provider = {"id": current["image_provider_id"], "url": current["image_url"],
                "adapter": "qwen_async", "steps": current["image_steps"]}
    for item in items:
        item["request_sha256"] = digest({"request": item, "provider": provider})
    result = {"schema_version": REQUEST_SCHEMA, "provider": provider, "requests": items,
              "requests_sha256": digest(items), "directory": str(directory)}
    destination = directory / "asset-requests.json"
    if destination.exists() and read(destination) != result:
        raise ValueError("Saved image-animation requests changed; use a new experiment directory")
    write(destination, result)
    if capabilities is not None:
        write(directory / "image-provider-capabilities.json", capabilities)
    _write_manifest(directory, result, _cached_assets(directory, result), [], capabilities)
    return result


def _requests(value):
    manifest = read(value) if isinstance(value, (str, Path)) else value
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != REQUEST_SCHEMA
            or manifest.get("requests_sha256") != digest(manifest.get("requests", []))):
        raise ValueError("Image-animation request schema or hash changed")
    provider = manifest.get("provider", {})
    items = _normalise_requests(manifest["requests"], {"image_width": None, "image_height": None})
    for item in items:
        saved_hash = item.pop("request_sha256", None)
        if saved_hash != digest({"request": item, "provider": provider}):
            raise ValueError("Asset request hash changed")
        item["request_sha256"] = saved_hash
    return manifest


def _asset_root(directory, request):
    root = Path(directory).resolve() / "assets" / (request["id"] + "-" + request["request_sha256"][:20])
    if not root.resolve().is_relative_to(Path(directory).resolve()):
        raise ValueError("Asset cache path escapes the experiment directory")
    return root


def inspect_asset(path, request):
    """Validate actual PNG pixels/dimensions/alpha; retain original encoded bytes."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if image.format != "PNG" or image.size != (request["width"], request["height"]):
                raise ValueError(f"Expected PNG {request['width']}x{request['height']}, got {image.format} {image.size}")
            if image.mode not in {"RGB", "RGBA", "P", "LA", "L"}:
                raise ValueError(f"Unsupported asset mode: {image.mode}")
            present = "A" in image.getbands() or "transparency" in image.info
            alpha = image.convert("RGBA").getchannel("A")
            # A solid-colour silhouette can be a valid part: its native alpha
            # supplies all shape information even if hidden RGB is uniform.
            if all(low == high for low, high in image.convert("RGBA").getextrema()):
                raise ValueError("Asset has no nonuniform visible content")
            histogram = alpha.histogram()
            pixels = image.width * image.height
            transparent = sum(histogram[:9])
            visible = sum(histogram[128:])
            opaque = sum(histogram[200:])
            content_bbox = alpha.point(lambda value: 255 if value >= 128 else 0).getbbox()
            if request["transparent_required"]:
                if not present or transparent < max(1, pixels * .005):
                    raise ValueError("Moving part needs genuine transparent background pixels, not RGB or nearly opaque alpha")
                if opaque < max(1, pixels * .001) or not content_bbox:
                    raise ValueError("Moving part has empty or nearly transparent content")
            return {"size": list(image.size), "mode": image.mode,
                    "alpha": {"channel_present": present, "range": list(alpha.getextrema()),
                              "transparent_pixels": transparent, "visible_pixels": visible,
                              "opaque_pixels": opaque, "total_pixels": pixels,
                              "content_bbox_xyxy_px": list(content_bbox) if content_bbox else None},
                    "sha256": digest(raw)}
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Unreadable asset: {path.name}: {exc}") from exc


def _cached_assets(directory, requests):
    output = {}
    for request in requests["requests"]:
        root = _asset_root(directory, request)
        selected = root / "selected.json"
        if not selected.exists():
            continue
        value = read(selected)
        path = Path(value.get("file", "")).resolve()
        if (value.get("request_sha256") != request["request_sha256"] or value.get("id") != request["id"]
                or not path.is_relative_to(root.resolve()) or not path.is_file()):
            raise ValueError("Asset checkpoint identity or cache path changed")
        observed = inspect_asset(path, request)
        if value.get("sha256") != observed["sha256"] or value.get("size") != observed["size"]:
            raise ValueError("Cached asset content changed")
        _landmarks(value.get("landmarks", {}), *observed["size"])
        output[request["id"]] = value
    for identifier, value in output.items():
        for ref, checksum in value.get("reference_sha256", {}).items():
            if ref not in output or output[ref]["sha256"] != checksum:
                raise ValueError(f"Cached reference binding changed for {identifier}: {ref}")
    return output


def _record(request, path, metadata, *, source, references, imported=None):
    imported = imported or {}
    landmarks = _landmarks(imported.get("landmarks", request["landmarks"]), request["width"], request["height"])
    missing = sorted(set(request["required_landmarks"]) - set(landmarks))
    return {"id": request["id"], "asset": request["id"], "kind": request["kind"], "status": "ready",
            "file": str(Path(path).resolve()), **metadata, "request_sha256": request["request_sha256"],
            "reference_sha256": references, "source": source, "prompt": request["prompt"],
            "landmarks": landmarks, "grounding": imported.get("grounding", request["grounding"]),
            "controller": imported.get("controller", request["controller"]),
            "required_landmarks": request["required_landmarks"], "incomplete_flags": ["missing_landmarks:" + name for name in missing],
            "visual_review": "not_performed", "provenance": imported.get("provenance", {})}


def _reason(request, capabilities, assets, execute):
    if not execute:
        return "waiting_execution"
    if not capabilities or capabilities.get("text_generation") is not True:
        return "unsupported_generation_api"
    if request["transparent_required"] and capabilities.get("native_alpha") is not True:
        return "unsupported_native_alpha"
    if request["reference_ids"] and capabilities.get("referenced_edit") is not True:
        return "unsupported_referenced_edit"
    if any(ref not in assets for ref in request["reference_ids"]):
        return "waiting_reference_assets"
    low, high, multiple = (capabilities.get("min_dimension", 512), capabilities.get("max_dimension", 2816),
                           capabilities.get("dimension_multiple", 16))
    if any(not low <= request[key] <= high or request[key] % multiple for key in ("width", "height")):
        return "unsupported_generation_dimensions"
    return None


def _write_manifest(directory, requests, assets, unresolved, capabilities):
    unresolved = list(unresolved)
    known = {value["id"] for value in unresolved}
    for request in requests["requests"]:
        if request["id"] not in assets and request["id"] not in known:
            unresolved.append({"id": request["id"], "status": "waiting_execution"})
    status = "assets_ready" if not unresolved else "waiting_execution"
    if any(item["status"].startswith("unsupported_") or item["status"] == "invalid_native_alpha" for item in unresolved):
        status = "waiting_manual_assets"
    elif any(item["status"] == "waiting_provider_busy" for item in unresolved):
        status = "waiting_provider_busy"
    result = {"schema_version": ASSET_SCHEMA, "requests_sha256": requests["requests_sha256"],
              "assets": assets, "status": status, "unresolved": unresolved}
    directory = Path(directory)
    write(directory / "asset-manifest.json", result)
    missing = []
    request_index = {item["id"]: item for item in requests["requests"]}
    for item in unresolved:
        request = request_index[item["id"]]
        missing.append({**request, "status": item["status"], "reason": item.get("reason", ""),
                        "references": {name: {"file": assets[name]["file"], "sha256": assets[name]["sha256"]}
                                       for name in request["reference_ids"] if name in assets},
                        "output_filename": request["id"] + ".png"})
    write(directory / "missing-asset-requests.json", {"schema_version": REQUEST_SCHEMA,
          "requests_sha256": requests["requests_sha256"], "requests": missing,
          "instruction": "Supply genuine PNG pixels at exact requested dimensions. Parts need a transparent background and visible subject. Preserve source-pixel landmarks; reference files and hashes bind scene appearance. No chroma key or synthetic alpha."})
    write(directory / "asset-import-template.json", {"schema_version": IMPORT_SCHEMA,
          "requests_sha256": requests["requests_sha256"], "assets": [
              {"id": item["id"], "file": item["output_filename"], "request_sha256": item["request_sha256"],
               "landmarks": item["landmarks"], "required_landmarks": item["required_landmarks"],
               "grounding": item["grounding"], "controller": item["controller"]} for item in missing]})
    if capabilities is not None:
        write(directory / "image-provider-capabilities.json", capabilities)
    return result


@contextlib.contextmanager
def _provider_session(config, store):
    """Run only on explicit execution, under the existing cooperative GPU lease.

    Preserve an already loaded Qwen model. If it is unloaded, only its /api/load
    route is used; other GPU services must already be free. No service switching.
    """
    with gpu_lease(config):
        status = media.request(config, "image", "GET", "/api/status")
        if status.get("busy") or status.get("state") in {"loading", "unloading"}:
            yield False
            return
        loaded_here = status.get("state") == "unloaded"
        if status.get("state") not in {"loaded", "unloaded"}:
            raise RuntimeError(f"Image provider is not ready: {status}")
        # Root ensure-services/startup owns service readiness. Check known
        # shared GPU workers but never release or recycle them automatically.
        if config.get("tts_url"):
            health = media.request(config, "tts", "GET", "/health")
            if (health.get("queued_or_running_jobs") or health.get("active_job")
                    or health.get("model_loaded") or health.get("loaded")):
                yield False
                return
        if loaded_here:
            media.request(config, "image", "POST", "/api/load")
            deadline = time.monotonic() + config.get("image_animation_load_timeout", 900)
            while True:
                status = media.request(config, "image", "GET", "/api/status")
                if status.get("state") == "loaded":
                    break
                if status.get("state") != "loading" or time.monotonic() >= deadline:
                    raise RuntimeError(f"Image model did not become ready: {status}")
                time.sleep(2)
        try:
            yield True
        finally:
            if loaded_here:
                status = media.request(config, "image", "GET", "/api/status")
                if not status.get("busy") and status.get("state") == "loaded":
                    media.request(config, "image", "POST", "/api/unload")


def generate_assets(config, store, requests_manifest, *, execute=False, capabilities=None, on_progress=None):
    """Generate/resume requested assets with the existing render_take adapter.

    Failed alpha/content checks are checkpointed and packaged for manual fill;
    they do not silently consume retries. Existing job-ID recovery remains the
    bounded wait_qwen_job implementation. Unknown submissions remain ambiguous.
    Cached ready assets, dry runs, and unsupported-only runs acquire no GPU lease.
    """
    requests = _requests(requests_manifest)
    directory = Path(store.root).resolve()
    current = _provider(config)
    if requests["provider"] != {"id": current["image_provider_id"], "url": current["image_url"],
                                 "adapter": "qwen_async", "steps": current["image_steps"]}:
        raise ValueError("Saved asset provider settings changed")
    assets = _cached_assets(directory, requests)
    if not execute or len(assets) == len(requests["requests"]):
        return _write_manifest(directory, requests, assets, [], capabilities)
    capabilities = capabilities if capabilities is not None else discover_capabilities(current)
    if capabilities.get("url") and capabilities["url"].rstrip("/") != current["image_url"]:
        raise ValueError("Capabilities belong to a different image provider")
    write(directory / "image-provider-capabilities.json", capabilities)
    unresolved, pending = [], []
    for request in requests["requests"]:
        if request["id"] in assets:
            continue
        root = _asset_root(directory, request)
        rejected = root / "validation-failed.json"
        if rejected.exists():
            unresolved.append({"id": request["id"], "status": "invalid_native_alpha", "reason": read(rejected)["reason"]})
            continue
        reason = _reason(request, capabilities, assets, True)
        # Dependencies being generated in this pass can become ready later.
        if reason == "waiting_reference_assets" and all(ref in assets or any(item["id"] == ref for item in pending)
                                                         for ref in request["reference_ids"]):
            reason = None
        if reason:
            unresolved.append({"id": request["id"], "status": reason})
        else:
            pending.append(request)
    if not pending:
        return _write_manifest(directory, requests, assets, unresolved, capabilities)
    with _provider_session(current, store) as available:
        if not available:
            unresolved.extend({"id": request["id"], "status": "waiting_provider_busy"} for request in pending)
            return _write_manifest(directory, requests, assets, unresolved, capabilities)
        for request in pending:
            if any(ref not in assets for ref in request["reference_ids"]):
                unresolved.append({"id": request["id"], "status": "waiting_reference_assets"})
                continue
            root = _asset_root(directory, request)
            references = {name: assets[name]["sha256"] for name in request["reference_ids"]}
            prompt = request["prompt"]
            if request["transparent_required"]:
                prompt += ("\nOutput one isolated moving part with native RGBA transparency. Background pixels have zero alpha; "
                           "the part is clearly visible and opaque. Preserve the referenced appearance and source-canvas coordinates.")
            params = {"prompt": prompt, "width": str(request["width"]), "height": str(request["height"]),
                      "steps": str(current["image_steps"]), "guidance": "0", "output_format": "png",
                      "seed": str(int(request["request_sha256"][:7], 16))}
            if references:
                params["reference_images"] = [{"file": assets[name]["file"], "sha256": checksum}
                                               for name, checksum in references.items()]
            if request["transparent_required"] and capabilities.get("alpha_parameter"):
                params[capabilities["alpha_parameter"]] = "true"
            store.event(request["id"], "image_animation_generating", {"kind": request["kind"], "references": references})
            candidate = render_take(current, store, root, params, request["id"])
            try:
                metadata = inspect_asset(candidate, request)
            except ValueError as exc:
                write(root / "validation-failed.json", {"request_sha256": request["request_sha256"],
                      "candidate": str(candidate), "sha256": digest(Path(candidate).read_bytes()), "reason": str(exc)})
                unresolved.append({"id": request["id"], "status": "invalid_native_alpha", "reason": str(exc)})
            else:
                value = _record(request, candidate, metadata, source="qwen_async", references=references)
                value["settings"] = params
                provider_path = root / "provider-0.json"
                if provider_path.exists():
                    value["provider_job"] = read(provider_path)
                write(root / "selected.json", value)
                assets[request["id"]] = value
            _write_manifest(directory, requests, assets, unresolved, capabilities)
            if on_progress:
                on_progress()
    return _write_manifest(directory, requests, assets, unresolved, capabilities)


def _safe_import_path(root, value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Imported PNG file must be a safe relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or ":" in value:
        raise ValueError("Imported PNG file must be a safe relative path")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError("Imported PNG path escapes its manifest directory or is missing")
    return result


def import_assets(import_manifest, requests_manifest, cache_dir):
    """Validate a whole manual package before copying any immutable PNG bytes.

    A package can fill any subset; remaining requests are emitted for the next
    cache-fill/resume. All import paths resolve beneath the JSON's directory,
    including symlinks. Required part landmarks must be supplied in source pixels.
    """
    requests = _requests(requests_manifest)
    source_manifest = Path(import_manifest).resolve()
    supplied = read(source_manifest)
    if (not isinstance(supplied, dict) or supplied.get("schema_version") != IMPORT_SCHEMA
            or supplied.get("requests_sha256") != requests["requests_sha256"]):
        raise ValueError("Manual import schema or request package hash changed")
    entries = supplied.get("assets")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Manual import assets must be a nonempty array")
    directory = Path(cache_dir).resolve()
    existing = _cached_assets(directory, requests)
    planned = {item["id"]: item for item in requests["requests"]}
    validated, seen = {}, set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") not in planned or entry["id"] in seen:
            raise ValueError("Manual import IDs must be unique and requested")
        identifier = entry["id"]
        seen.add(identifier)
        request = planned[identifier]
        if entry.get("request_sha256") != request["request_sha256"]:
            raise ValueError(f"Manual import request hash changed: {identifier}")
        path = _safe_import_path(source_manifest.parent, entry.get("file"))
        metadata = inspect_asset(path, request)
        if entry.get("sha256") is not None and entry["sha256"] != metadata["sha256"]:
            raise ValueError("Manual import content hash changed")
        _landmarks(entry.get("landmarks", request["landmarks"]), *metadata["size"], request["required_landmarks"])
        for key in ("grounding", "controller", "provenance"):
            if key in entry and not isinstance(entry[key], dict):
                raise ValueError(f"Imported {key} metadata must be an object")
        if identifier in existing and existing[identifier]["sha256"] != metadata["sha256"]:
            raise ValueError("A ready cached asset is immutable; use a new request to replace it")
        raw = path.read_bytes()
        if digest(raw) != metadata["sha256"]:
            raise ValueError("Import source changed during validation")
        validated[identifier] = (entry, metadata, raw)
    available = {**existing, **{identifier: metadata for identifier, (_, metadata, _) in validated.items()}}
    for identifier in validated:
        if any(ref not in available for ref in planned[identifier]["reference_ids"]):
            raise ValueError(f"Import must supply missing reference assets for {identifier}")
    # Everything above is read-only: an invalid final entry cannot partly import.
    for request in requests["requests"]:
        if request["id"] not in validated:
            continue
        entry, metadata, raw = validated[request["id"]]
        root = _asset_root(directory, request)
        root.mkdir(parents=True, exist_ok=True)
        destination = root / "imported.png"
        temporary = root / "imported.tmp.png"
        temporary.write_bytes(raw)
        temporary.replace(destination)
        references = {name: available[name]["sha256"] for name in request["reference_ids"]}
        value = _record(request, destination, metadata, source="manual_import", references=references, imported=entry)
        value["provenance"] = {**value["provenance"], "import_manifest_sha256": digest(source_manifest.read_bytes()),
                               "source_file_sha256": metadata["sha256"], "reference_use": "operator_supplied"}
        write(root / "selected.json", value)
        existing[request["id"]] = value
    return _write_manifest(directory, requests, existing, [], None)
