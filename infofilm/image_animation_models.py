"""Activity routing for the isolated image-backed animation workflow.

The existing local ``Models`` adapter remains responsible for local JSON calls.
Luna uses the official Responses API and never receives credentials from a file.
Registry snapshots, descriptions and cache metadata contain no authentication.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .core import ROOT, digest, read, write
from .llm import ModelOutputError, Models

SCHEMA = "infofilm.image-animation-providers.v1"
ADAPTER = "infofilm.image-animation-model-routing.v1"
ACTIVITIES = ("research", "classification", "parent", "evidence", "teaching",
              "narration", "canvas", "grounding", "motion", "review")
ROUTES = ("luna", "local", "local-reviewer")
DEFAULT_ROUTES = {activity: ("luna" if activity in {"teaching", "narration", "canvas", "motion"}
                           else "local-reviewer" if activity in {"grounding", "review"}
                           else "local") for activity in ACTIVITIES}
INSTRUCTIONS = (
    "Return only the requested complete JSON object. Be precise, evidence-grounded and concise. "
    "Follow the supplied image, source facts and renderer contract. Do not invent sources, "
    "capabilities or human review. Runtime is advisory."
)
TRANSPORT_MAX_ATTEMPTS = 3
TRANSPORT_RETRY_DELAYS = (1, 2)
ERROR_BODY_LIMIT = 1200


class ModelTransportError(RuntimeError):
    """A provider request failed, with safe diagnostics available to callers."""

    def __init__(self, route, diagnostic, path):
        self.provider = route
        self.diagnostic = diagnostic
        self.reason = diagnostic["reason"]
        self.status_code = diagnostic.get("status_code")
        self.retryable = diagnostic.get("retryable", False)
        self.diagnostic_path = path
        status = f"HTTP {self.status_code}" if self.status_code else diagnostic["exception_type"]
        super().__init__(f"{route} model request failed with {status} ({diagnostic['classification']}); "
                         f"safe diagnostics saved to {path}")


def _transport_diagnostic(exc):
    """Do not persist exception strings, request headers or arbitrary response objects."""
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    retryable = (status in {408, 429} or (status is not None and 500 <= status <= 599)) if status else isinstance(
        exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError))
    if status == 429:
        classification = "rate_limited"
    elif status == 408:
        classification = "request_timeout"
    elif status and 500 <= status <= 599:
        classification = "provider_unavailable"
    elif status == 401:
        classification = "authentication"
    elif status == 403:
        classification = "permission_denied"
    elif status == 404:
        classification = "endpoint_not_found"
    elif status:
        classification = "invalid_request"
    else:
        classification = "transport_error"
    detail = {"reason": "provider_http_error" if status else "provider_transport_error",
              "classification": classification, "exception_type": type(exc).__name__, "retryable": retryable}
    if status is not None:
        detail["status_code"] = status
        # Redact before truncating, so an API key straddling the boundary cannot leak.
        body = _redact(exc.response.text)
        body = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [redacted]", body)
        body = re.sub(r"(?i)([\"']?(?:api[_-]?key|authorization|access[_-]?token|token|password|secret)[\"']?\s*[:=]\s*)"
                      r"(?:[\"'][^\"']*[\"']|[^\s,;}<>]+)", r"\1[redacted]", body)
        body = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", body)
        body = re.sub(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", "[inline image omitted]", body)
        if status == 400 and re.search(r"(?i)(context.{0,30}(?:length|window|limit)|"
                                      r"maximum.{0,20}(?:tokens|context)|input.{0,20}too.{0,10}long)", body):
            detail["classification"] = "context_length"
        detail["body_preview"] = body[:ERROR_BODY_LIMIT]
        detail["body_truncated"] = len(body) > ERROR_BODY_LIMIT
    return detail


class OutputBudgetError(ModelOutputError):
    """The configured output ceiling was reached; semantic retries cannot fix it."""
    output_budget_exhausted = True

    def __init__(self, budget, usage=None):
        self.budget, self.usage = budget, usage or {}
        super().__init__(f"Model output reached its {budget}-token ceiling; incomplete response preserved")


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def _endpoint(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an HTTP endpoint")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError(f"{name} must be an HTTP endpoint without credentials, query or fragment")
    return value.rstrip("/")


def validate_registry(value):
    """Validate a credential-free registry, returning an independent snapshot."""
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        raise ValueError(f"Expected {SCHEMA} registry")
    if set(value) - {"schema_version", "providers", "activities", "limits"}:
        raise ValueError("Unknown image animation registry fields")
    result = copy.deepcopy(value)
    providers = result.get("providers")
    if not isinstance(providers, dict) or set(providers) != set(ROUTES):
        raise ValueError("Providers must define luna, local and local-reviewer")
    for route, provider in providers.items():
        if not isinstance(provider, dict):
            raise ValueError(f"Provider {route} must be an object")
        allowed = {"adapter", "url", "url_config_key", "model", "reasoning_effort",
                   "max_output_tokens", "timeout_seconds"}
        if set(provider) - allowed:
            raise ValueError(f"Unknown or credential-bearing settings for {route}")
        expected = "openai_responses" if route == "luna" else "local_chat_completions"
        if provider.get("adapter") != expected:
            raise ValueError(f"Provider {route} requires adapter {expected}")
        if ("url" in provider) == ("url_config_key" in provider):
            raise ValueError(f"Provider {route} needs exactly one URL or config endpoint key")
        if "url" in provider:
            provider["url"] = _endpoint(provider["url"], route + " URL")
        elif provider["url_config_key"] not in {"planner_url", "reviewer_url"}:
            raise ValueError(f"Invalid config endpoint key for {route}")
        if route == "luna":
            if provider.get("url") != "https://api.openai.com/v1":
                raise ValueError("Luna uses the official https://api.openai.com/v1 endpoint")
            if provider.get("model") != "gpt-6-luna":
                raise ValueError("The Luna route requires gpt-6-luna")
            if provider.get("reasoning_effort") not in {"low", "medium", "high"}:
                raise ValueError("Luna reasoning_effort must be low, medium or high")
            _integer(provider.get("timeout_seconds", 300), "timeout_seconds", 1, 900)
            provider.setdefault("timeout_seconds", 300)
        elif "reasoning_effort" in provider or "timeout_seconds" in provider:
            raise ValueError("Local adapter settings use the existing Models defaults")
        if "model" in provider and (not isinstance(provider["model"], str) or not provider["model"].strip()):
            raise ValueError(f"Provider {route} model must be a nonempty string")
        _integer(provider.get("max_output_tokens"), route + " max_output_tokens", 16, 128000)
    routes = result.get("activities")
    if not isinstance(routes, dict) or set(routes) != set(ACTIVITIES):
        raise ValueError("Activities must define every supported activity exactly once")
    for activity, route in routes.items():
        if not isinstance(route, str) or route not in ROUTES:
            raise ValueError(f"Invalid model route for {activity}: choose {', '.join(ROUTES)}")
    limits = result.get("limits", {})
    if not isinstance(limits, dict) or set(limits) - {"max_format_attempts", "max_images", "max_image_bytes"}:
        raise ValueError("Invalid image animation routing limits")
    defaults = {"max_format_attempts": 2, "max_images": 8, "max_image_bytes": 20 * 1024 * 1024}
    result["limits"] = {**defaults, **limits}
    for key, low, high in (("max_format_attempts", 1, 2), ("max_images", 1, 32),
                           ("max_image_bytes", 1, 50 * 1024 * 1024)):
        _integer(result["limits"][key], key, low, high)
    return result


def load_registry(path=None, *, overrides=None):
    registry = validate_registry(read(path or ROOT / "image-animation-providers.json"))
    if overrides:
        if not isinstance(overrides, dict) or set(overrides) - set(ACTIVITIES):
            raise ValueError("Route overrides must name supported activities")
        registry["activities"].update(overrides)
    return validate_registry(registry)


def _secrets():
    return tuple(value for name in ("OPENAI_API_KEY", "OPENAI_TOKEN", "VLLM_API_KEY")
                 if (value := os.environ.get(name)))


def _redact(value):
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()
                if key.lower() not in {"authorization", "api_key", "token", "headers"}}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        for secret in _secrets():
            value = value.replace(secret, "[redacted]")
        if value.startswith("data:image/"):
            return "[inline image omitted]"
    return value


class _LocalStore:
    def __init__(self, parent, root):
        self.root, self.parent = root, parent

    def event(self, stage, state, detail=None):
        self.parent.event(stage, state, _redact(detail or {}))


class ImageAnimationModels:
    """Own a frozen route registry and vend a Models facade for each activity."""

    def __init__(self, config, store, *, registry=None, routes=None):
        self.config, self.store = copy.deepcopy(config), store
        self.registry = validate_registry(registry) if registry is not None else load_registry()
        if routes:
            if not isinstance(routes, dict) or set(routes) - set(ACTIVITIES):
                raise ValueError("Route overrides must name supported activities")
            self.registry["activities"].update(routes)
            self.registry = validate_registry(self.registry)
        self.providers = copy.deepcopy(self.registry["providers"])
        for route, provider in self.providers.items():
            if "url_config_key" in provider:
                key = provider["url_config_key"]
                provider["url"] = _endpoint(self.config.get(key), key)
        self.fingerprint = digest({"adapter": ADAPTER, "routes": self.registry["activities"],
                                   "providers": self.providers, "limits": self.registry["limits"]})
        self._identities, self._locals = {}, {}

    def describe_routes(self):
        """Return resolved, credential-free routing information without network IO."""
        return {activity: {"provider": route, **copy.deepcopy(self.providers[route])}
                for activity, route in self.registry["activities"].items()}

    def for_activity(self, activity):
        if activity not in ACTIVITIES:
            raise ValueError(f"Unknown image animation activity: {activity}")
        return ActivityModels(self, activity)

    def _transport(self, operation, *, route, name, prefix, phase="completion"):
        """Retry transport failures only; parsing, incomplete output and semantic checks stay outside this loop."""
        history = []
        for attempt in range(1, TRANSPORT_MAX_ATTEMPTS + 1):
            try:
                value = operation()
            except httpx.HTTPError as exc:
                detail = {**_transport_diagnostic(exc), "phase": phase, "attempt": attempt,
                          "max_attempts": TRANSPORT_MAX_ATTEMPTS, "provider": route}
                history.append(detail)
                write(prefix.with_suffix(f".transport-{attempt}.json"), detail)
                event_detail = {key: value for key, value in detail.items() if key != "body_preview"}
                retrying = detail["retryable"] and attempt < TRANSPORT_MAX_ATTEMPTS
                if not retrying:
                    failure_path = prefix.with_suffix(".failure.json")
                    write(failure_path, {**detail, "transport_attempts": history})
                    self.store.event(name, "failed", {**event_detail, "diagnostic": str(failure_path)})
                    raise ModelTransportError(route, detail, failure_path) from None
                delay = TRANSPORT_RETRY_DELAYS[attempt - 1]
                self.store.event(name, "transport_retry", {**event_detail, "delay_seconds": delay})
                time.sleep(delay)
            else:
                if history:
                    write(prefix.with_suffix(".transport.json"), {"status": "recovered", "phase": phase,
                        "attempt_count": attempt, "transport_attempts": history})
                    self.store.event(name, "transport_recovered", {"provider": route, "phase": phase,
                                                                  "attempt": attempt})
                return value

    def _images(self, image_path, references):
        paths = ([image_path] if image_path else []) + list(references or [])
        if len(paths) > self.registry["limits"]["max_images"]:
            raise ValueError("Image input count exceeds the configured limit")
        images = []
        for path in paths:
            path = Path(path)
            if path.stat().st_size > self.registry["limits"]["max_image_bytes"]:
                raise ValueError("Image input exceeds the configured byte limit")
            data = path.read_bytes()
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif data.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            elif data[:6] in {b"GIF87a", b"GIF89a"}:
                mime = "image/gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                raise ValueError(f"Unsupported image input format: {path.name}")
            images.append({"path": str(path), "content_hash": digest(data), "mime_type": mime, "data": data})
        return images

    def _local_identity(self, route, provider):
        if provider.get("model"):
            return provider["model"]
        endpoint = provider["url"]
        if endpoint not in self._identities:
            headers = {"Authorization": "Bearer " + os.environ["VLLM_API_KEY"]} if os.environ.get("VLLM_API_KEY") else {}
            prefix = self.store.root / "requests" / ("model-discovery-" + digest({"provider": route, "url": endpoint})[:12])
            def discover():
                with httpx.Client(timeout=30, headers=headers) as client:
                    response = client.get(endpoint + "/models")
                    response.raise_for_status()
                return response
            try:
                response = self._transport(discover, route=route, name="model-discovery-" + route,
                                           prefix=prefix, phase="model_discovery")
                identity = response.json()["data"][0]["id"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise RuntimeError(f"Unable to discover the {route} model identity") from None
            if not isinstance(identity, str) or not identity:
                raise RuntimeError(f"The {route} endpoint returned an invalid model identity")
            self._identities[endpoint] = identity
        return self._identities[endpoint]

    def _call(self, activity, role, name, prompt, image_path, references, max_tokens):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,220}", name):
            raise ValueError("Model request name must be a safe filename")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Model prompt must be a nonempty string")
        _integer(max_tokens, "max_tokens", 16, 128000)
        references = list(references or [])
        images = self._images(image_path, references)
        route = self.registry["activities"][activity]
        provider = self.providers[route]
        budget = min(max_tokens, provider["max_output_tokens"])
        model = provider["model"] if route == "luna" else self._local_identity(route, provider)
        settings = {"max_output_tokens": budget, **({"reasoning_effort": provider["reasoning_effort"],
                    "timeout_seconds": provider["timeout_seconds"], "image_detail": "auto"}
                    if route == "luna" else {"temperature": 0.15, "enable_thinking": False, "timeout_seconds": 900})}
        if "json" not in prompt.lower():
            prompt = "Return the requested complete JSON object.\n" + prompt
        metadata = {"adapter": ADAPTER, "activity": activity, "provider": route, "role": role,
                    "url": provider["url"], "model": model, "prompt": prompt,
                    "instructions": INSTRUCTIONS if route == "luna" else "existing Models JSON instructions",
                    "settings": settings,
                    "images": [{key: value for key, value in image.items() if key != "data"} for image in images]}
        # Content, not filesystem location, determines reuse.
        identity = digest({**metadata, "images": [{key: value for key, value in image.items() if key not in {"data", "path"}}
                                                  for image in images]})
        prefix = self.store.root / "requests" / f"{name}-{identity[:12]}"
        result_path = prefix.with_suffix(".result.json")
        if result_path.exists():
            result = read(result_path)
            if not isinstance(result, dict):
                raise ModelOutputError("Cached model result must be a JSON object")
            return result
        failure_path = prefix.with_suffix(".failure.json")
        if failure_path.exists():
            failure = read(failure_path)
            if failure.get("reason") == "output_token_limit":
                raise OutputBudgetError(budget, failure.get("usage"))
        write(prefix.with_suffix(".request.json"), _redact({**metadata, "input_hash": identity}))
        try:
            if route == "luna":
                value = self._transport(lambda: self._responses(name, prefix, metadata, images),
                                        route=route, name=name, prefix=prefix)
            else:
                local_key = digest({"provider": route, "url": provider["url"], "model": model, "settings": settings})
                if local_key not in self._locals:
                    local = Models({"planner_url": provider["url"], "reviewer_url": provider["url"]},
                                   _LocalStore(self.store, self.store.root / "model-cache" / local_key[:16]))
                    local.identities.update(planner=model, reviewer=model)
                    self._locals[local_key] = local
                value = self._transport(lambda: self._local_call(self._locals[local_key], role, name, prompt,
                                        image_path=image_path, references=references, budget=budget),
                                        route=route, name=name, prefix=prefix)
        except ModelOutputError as exc:
            if getattr(exc, "output_budget_exhausted", False) or "output limit" in str(exc):
                write(failure_path, {"reason": "output_token_limit", "max_output_tokens": budget,
                                     "usage": _redact(getattr(exc, "usage", {}))})
                if not getattr(exc, "output_budget_exhausted", False):
                    raise OutputBudgetError(budget) from None
            raise
        if not isinstance(value, dict):
            raise ModelOutputError("Model must return one JSON object")
        if _redact(value) != value:
            raise ModelOutputError("Model output contained credentials or inline image bytes; result omitted")
        write(result_path, value)
        return value

    def _local_call(self, local, role, name, prompt, *, image_path, references, budget):
        # Models caches successful JSON but not responses which failed parsing. Reuse those
        # preserved responses too, without paying for the identical request on resume.
        signature = digest({"url": local.config[role + "_url"], "prompt": prompt,
                            "image": digest(Path(image_path).read_bytes()) if image_path else None,
                            "max_tokens": budget,
                            **({"reference_images": [digest(Path(path).read_bytes()) for path in references]} if references else {})})
        prefix = local.store.root / "requests" / f"{name}-{signature[:12]}"
        response_path = prefix.with_suffix(".response.json")
        if response_path.exists() and not prefix.with_suffix(".result.json").exists():
            raw = read(response_path)
            try:
                choice = raw["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise OutputBudgetError(budget, raw.get("usage"))
                text = choice["message"].get("content") or ""
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
                value = json.loads(text)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                raise ModelOutputError("Cached local response is invalid JSON; response preserved") from None
            if not isinstance(value, dict):
                raise ModelOutputError("Cached local response must contain one JSON object; response preserved")
            return value
        try:
            return local.call(role, name, prompt, image_path=image_path,
                              reference_image_paths=references, max_tokens=budget)
        finally:
            # The shared adapter writes directly to disk; scrub its artifacts even when
            # parsing failed before the outer route could inspect the returned value.
            for suffix in (".response.json", ".result.json"):
                path = prefix.with_suffix(suffix)
                if path.exists():
                    raw = read(path)
                    safe = _redact(raw)
                    if safe != raw:
                        write(path, safe)

    def _responses(self, name, prefix, metadata, images):
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_TOKEN")
        if not key:
            raise RuntimeError("Set OPENAI_API_KEY or OPENAI_TOKEN in the environment for the Luna route")
        content = [{"type": "input_text", "text": metadata["prompt"]}]
        content.extend({"type": "input_image", "image_url": "data:" + image["mime_type"] + ";base64," +
                        base64.b64encode(image["data"]).decode(), "detail": "auto"} for image in images)
        settings = metadata["settings"]
        body = {"model": metadata["model"], "instructions": INSTRUCTIONS,
                "input": [{"role": "user", "content": content}], "store": False,
                "reasoning": {"effort": settings["reasoning_effort"]},
                "max_output_tokens": settings["max_output_tokens"], "text": {"format": {"type": "json_object"}}}
        self.store.event(name, "running", {"activity": metadata["activity"], "provider": "luna", "model": metadata["model"],
                                          "settings": settings})
        response_path = prefix.with_suffix(".response.json")
        if response_path.exists():
            raw = read(response_path)
        else:
            start = time.monotonic()
            with httpx.Client(timeout=settings["timeout_seconds"], headers={"Authorization": "Bearer " + key}) as client:
                response = client.post(metadata["url"] + "/responses", json=body)
                response.raise_for_status()
            try:
                raw = response.json()
            except ValueError:
                raise ModelOutputError("Luna returned a non-JSON response body") from None
            if isinstance(raw, dict):
                raw["measured_latency_seconds"] = round(time.monotonic() - start, 3)
                write(response_path, _redact(raw))
        if not isinstance(raw, dict):
            raise ModelOutputError("Luna returned an invalid response object")
        if raw.get("status") != "completed":
            details = raw.get("incomplete_details")
            if isinstance(details, dict) and details.get("reason") == "max_output_tokens":
                raise OutputBudgetError(settings["max_output_tokens"], raw.get("usage"))
            raise ModelOutputError("Luna response did not complete; response preserved")
        output = raw.get("output")
        if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
            raise ModelOutputError("Luna returned malformed output items; response preserved")
        parts = []
        for item in output:
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
                raise ModelOutputError("Luna returned malformed message content; response preserved")
            parts.extend(content)
        if any(part.get("type") == "refusal" for part in parts):
            raise ModelOutputError("Luna declined this request; response preserved")
        if any(not isinstance(part.get("text"), str) for part in parts if part.get("type") == "output_text"):
            raise ModelOutputError("Luna returned malformed output text; response preserved")
        text = "".join(part.get("text", "") for part in parts if part.get("type") == "output_text")
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise ModelOutputError("Luna output is not valid JSON; response preserved") from None
        if not isinstance(value, dict):
            raise ModelOutputError("Luna must return one JSON object; response preserved")
        if _redact(value) != value:
            raise ModelOutputError("Model output contained credentials or inline image bytes; result omitted")
        self.store.event(name, "succeeded", {"latency_seconds": raw["measured_latency_seconds"],
                                            "usage": _redact(raw.get("usage", {}))})
        return value


class ActivityModels(Models):
    """Existing planning phases can use this facade without choosing providers."""

    def __init__(self, router, activity):
        self.router, self.activity, self.store = router, activity, router.store
        descriptions = router.describe_routes()
        planner, reviewer = descriptions[activity], descriptions["review"]
        self.fingerprint = digest({"adapter": ADAPTER, "planner": planner, "reviewer": reviewer,
                                   "limits": router.registry["limits"]})
        self.config = {**router.config, "planner_url": planner["url"], "reviewer_url": reviewer["url"],
                       "planner_model": planner.get("model", "server-discovered"),
                       "reviewer_model": reviewer.get("model", "server-discovered"),
                       "reasoning_effort": planner.get("reasoning_effort"),
                       "image_animation_route_identity": self.fingerprint,
                       "image_animation_activity": activity}
        self.identities = {}

    def call(self, role, name, prompt, *, image_path=None, reference_image_paths=None, max_tokens=7000):
        if role not in {"planner", "reviewer"}:
            raise ValueError("Image animation model roles must be planner or reviewer")
        activity = self.activity if role == "planner" else "review"
        return self.router._call(activity, role, name, prompt, image_path, reference_image_paths, max_tokens)

    def call_json(self, role, name, prompt, *, max_tokens, image_path=None, reference_image_paths=None):
        reference_image_paths = list(reference_image_paths or [])
        feedback = ""
        attempts = self.router.registry["limits"]["max_format_attempts"]
        for index in range(attempts):
            try:
                return self.call(role, name if not index else name + "-format-repair", prompt + feedback,
                                 max_tokens=max_tokens, image_path=image_path, reference_image_paths=reference_image_paths)
            except ModelOutputError as exc:
                if index + 1 == attempts or getattr(exc, "output_budget_exhausted", False):
                    raise
                feedback = "\nFORMAT RECOVERY: Return one complete compact JSON object with the requested fields."
                self.store.event(name, "format_retry", {"attempt": index + 1, "max_attempts": attempts})
