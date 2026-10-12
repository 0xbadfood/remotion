"""Provider registry and bounded, evidence-led escalation for individual assets.

Adapters own transport/lifecycle; this module owns routing and acceptance budgets.
Cloud adapters can implement the same checkpoint contract without changing plans.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from .core import digest, read, write

ADAPTERS = {"qwen_async", "imagestudio_sdapi", "openrouter_image"}
IMAGE_REVIEW_REASONS = {
    "unwanted_text": "Generated overlay captions, labels or gibberish typography; not ordinary writing on a required scene object.",
    "semantic_mismatch": "Wrong or missing essential subject/object/action, including an unwanted figure.",
    "factual_mismatch": "Demonstrable historical, geographical or scientific error.",
    "composition_obstruction": "Essential content is lost under the actual renderer overlay or crop.",
    "visual_artifact": "Malformed or incoherent visible content that makes the illustration unusable.",
    "prompt_ambiguity": "Ambiguous or contradictory scene wording caused the wrong interpretation.",
    "backend_corruption": "Blank, unreadable or technically corrupted output requiring backend diagnosis.",
    "uncertain": "Observed concern cannot be confidently classified; needs rechecking.",
}


def image_review_errors(review, *, require_inventory=False, reference_available=None):
    """New reviews must use the code-owned vocabulary, not free-form reasons."""
    if not isinstance(review, dict):
        return ["Expected one image-review JSON object"]
    errors = []
    if type(review.get("pass")) is not bool:
        errors.append("pass must be boolean")
    reasons = review.get("reason_keys")
    if (not isinstance(reasons, list) or any(not isinstance(reason, str) or reason not in IMAGE_REVIEW_REASONS for reason in reasons)):
        errors.append("reason_keys must be a list drawn only from: " + ", ".join(IMAGE_REVIEW_REASONS))
    elif len(reasons) != len(set(reasons)) or bool(reasons) != (review.get("pass") is False):
        errors.append("reason_keys must be unique, empty for acceptance and nonempty for rejection")
    for field in ("issues", "limitations"):
        value = review.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(field + " must be a list of strings")
    if review.get("pass") is False and not review.get("issues"):
        errors.append("Rejection needs concrete visible evidence in issues")
    if not isinstance(review.get("correction"), str):
        errors.append("correction must be a string")
    from .educational_review import inventory_errors, inventory_failures
    inventory_problems = inventory_errors(review, required=require_inventory, reference_available=reference_available)
    errors.extend(inventory_problems)
    if not inventory_problems and review.get('pass') is True and inventory_failures(review):
        errors.append('A component count or placement contradiction cannot pass; give concrete issues and correction')
    return errors


def bounded_int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def validate_registry(registry):
    if not isinstance(registry, dict) or registry.get("schema_version") != "infofilm.image-providers.v1":
        raise ValueError("Expected infofilm.image-providers.v1 settings")
    providers, policies = registry.get("providers"), registry.get("policies")
    if not isinstance(providers, dict) or not providers or not isinstance(policies, dict) or not policies:
        raise ValueError("Image settings need providers and policies")
    for name, provider in providers.items():
        if not re.fullmatch(r"[a-z0-9_-]+", name) or not isinstance(provider, dict):
            raise ValueError("Provider IDs must be safe names")
        if provider.get("adapter") not in ADAPTERS:
            raise ValueError(f"Unsupported image adapter for {name}: {provider.get('adapter')}")
        if not isinstance(provider.get("url"), str):
            raise ValueError(f"Provider {name} needs an HTTP URL")
        url = urlparse(provider["url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError(f"Provider {name} needs an HTTP URL without embedded credentials")
        settings = provider.get("settings", {})
        if not isinstance(settings, dict):
            raise ValueError(f"Provider {name} settings must be an object")
        bounded_int(settings.get("steps"), name + ".steps", 1, 100)
        if provider["adapter"] == "imagestudio_sdapi":
            from .image_studio import ENGINES
            if provider.get("engine") not in ENGINES:
                raise ValueError(f"Unsupported ImageStudio engine for {name}")
            bounded_int(settings.get("dimension_multiple"), name + ".dimension_multiple", 1, 128)
            cfg = settings.get("cfg_scale")
            if type(cfg) not in {float, int} or not math.isfinite(cfg) or not 0 <= cfg <= 30:
                raise ValueError(f"Invalid CFG for {name}")
            if type(provider.get("park_idle_services", False)) is not bool:
                raise ValueError(f"Invalid parking flag for {name}")
    for name, policy in policies.items():
        if not isinstance(policy, dict):
            raise ValueError(f"Policy {name} must be an object")
        tiers = policy.get("tiers")
        if not isinstance(tiers, list) or not 1 <= len(tiers) <= 4:
            raise ValueError(f"Policy {name} needs 1–4 ordered tiers")
        ids = []
        for tier in tiers:
            if not isinstance(tier, dict):
                raise ValueError(f"Policy {name} tiers must be objects")
            if tier.get("provider") not in providers:
                raise ValueError(f"Unknown provider in policy {name}")
            ids.append(tier["provider"])
            bounded_int(tier.get("max_attempts"), name + ".max_attempts", 1, 5)
        if len(ids) != len(set(ids)):
            raise ValueError("A policy cannot repeat a provider tier")
        bounded_int(policy.get("max_total_attempts"), name + ".max_total_attempts", 1, 20)
        bounded_int(policy.get("min_escalation_attempts"), name + ".min_escalation_attempts", 2, 5)
        confidence = policy.get("escalation_confidence")
        if type(confidence) not in {int, float} or not math.isfinite(confidence) or not 0.5 <= confidence <= 1:
            raise ValueError("Escalation confidence must be between 0.5 and 1")
    if registry.get("default_policy") not in policies:
        raise ValueError("Unknown default image provider policy")
    return registry


def provider_config(base, provider_id, provider):
    result = {**base, "image_provider_id": provider_id, "image_url": provider["url"].rstrip("/"),
              "image_steps": provider["settings"]["steps"]}
    result.pop("image_engine", None)
    if provider["adapter"] == "imagestudio_sdapi":
        result.update(image_engine=provider["engine"],
                      image_cfg_scale=provider["settings"]["cfg_scale"],
                      image_dimension_multiple=provider["settings"]["dimension_multiple"],
                      image_park_idle_services=provider.get("park_idle_services", False))
        # The Qwen adapter's endpoint is separate from the current tier endpoint.
        result["qwen_url"] = base.get("qwen_url", base["image_url"])
    elif provider["adapter"] == "openrouter_image":
        # Cloud premium tier: the model and key come from LLM settings (premium_image_endpoint).
        result.update(image_engine="openrouter")
        result["qwen_url"] = base.get("qwen_url", base["image_url"])
    return result


def check_candidate(path, store, asset_id):
    """Do not spend artistic retries on blank/undecodable backend output."""
    try:
        with Image.open(path) as image:
            image.load()
            if all(low == high for low, high in image.convert("RGB").getextrema()):
                raise ValueError("uniform blank image")
    except (OSError, ValueError) as exc:
        store.event(asset_id, "backend_failed", {"file": str(path), "error": str(exc)})
        raise RuntimeError(f"Image backend returned unusable output for {asset_id}: {exc}; diagnose before retrying") from exc


def has_meaningful_repairs(history, minimum_attempts=2):
    prompts = [item.get("settings", {}).get("prompt", "").strip() for item in history]
    filler = {"a", "an", "the", "and", "of", "for", "with", "in", "on", "to", "at", "by"}
    descriptions = [tuple(word for word in re.findall(r"[a-z0-9]+", prompt.casefold()) if word not in filler)
                    for prompt in prompts if prompt]
    return len(history) >= minimum_attempts and len(set(descriptions)) >= 2


def escalation_decision(review, history, config):
    """An LLM recommendation cannot choose a model or expand its own budget."""
    if review.get("pass") is True:
        return "repair_prompt"
    # Legacy cached reviews retain their original diagnosis. New structured
    # reasons are validated and never inferred from prose or substring matches.
    reasons = review.get("reason_keys", [])
    if "reason_keys" in review and image_review_errors(review):
        return "recheck"
    if "backend_corruption" in reasons:
        return "diagnose_backend"
    rule_escalation = "unwanted_text" in reasons
    diagnosis = review.get("diagnosis", {})
    if not isinstance(diagnosis, dict) and not rule_escalation:
        return "repair_prompt"
    if not rule_escalation:
        cause = diagnosis.get("cause")
        confidence = diagnosis.get("confidence")
        evidence = diagnosis.get("evidence")
        if (type(confidence) not in {float, int} or not math.isfinite(confidence)
                or not 0 <= confidence <= 1 or not isinstance(evidence, list)
                or not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence)):
            return "recheck"
        if confidence < config.get("image_escalation_confidence", .8):
            return "recheck"
        if cause == "backend_failure" and diagnosis.get("suggested_action") == "diagnose_backend":
            return "diagnose_backend"
        # A confident, evidenced provider diagnosis takes precedence over a stale
        # repair recommendation. Keep explicit uncertainty/backend requests separate.
        if cause != "provider_weakness" or diagnosis.get("suggested_action") not in {"escalate", "repair_prompt"}:
            return "repair_prompt" if cause == "prompt_weakness" else "recheck"
    # Compare prompts within this tier, not inherited attempts on another model.
    prior_count = config.get("image_prior_count", 0)
    current = history[prior_count:]
    # Even a small synonym change (seal -> stamp) can be meaningful. Ignore
    # punctuation, casing and articles rather than requiring a large edit distance.
    if not has_meaningful_repairs(current, config.get("image_min_escalation_attempts", 2)):
        return "recheck"
    return "escalate"


def attempt_history(config, store, library, asset, *, include_unreviewed=False):
    from .media import image_identity
    root = store.root / "images" / image_identity(config, library, asset)[:20]
    selected = root / "selected.json"
    selected_index = None
    if selected.exists():
        match = re.fullmatch(r"candidate-(\d+)\.png", Path(read(selected)["file"]).name)
        if match:
            selected_index = int(match[1])
    history = []
    for index in range(max(config["image_max_attempts"], (selected_index or 0) + 1)):
        review, provider = root / f"review-{index}.json", root / f"provider-{index}.json"
        if not review.exists():
            if include_unreviewed and provider.exists():
                record = read(provider)
                history.append({"provider": config["image_provider_id"], "tier": config["image_tier"],
                    "attempt": index + 1, "settings": record.get("params", {}), "review": None,
                    "submission_state": record.get("state"), "candidate": str(root / f"candidate-{index}.png")})
            break
        history.append({"provider": config["image_provider_id"], "tier": config["image_tier"],
                        "attempt": index + 1, "settings": read(provider).get("params", {}) if provider.exists() else {},
                        "review": read(review), "candidate": str(root / f"candidate-{index}.png")})
        gate = {**config, "image_prior_count": len(config.get("image_prior_attempts", {}).get(asset["id"], []))}
        combined = config.get("image_prior_attempts", {}).get(asset["id"], []) + history
        if index == selected_index:
            break
        if selected_index is None and escalation_decision(history[-1]["review"], combined, gate) == "escalate" and config["image_has_next_tier"]:
            break
    return history


def generate_with_providers(config, store, library, models, *, on_progress=None):
    from .media import generate_images_single
    registry = validate_registry(config["image_provider_registry"])
    policy_name = config.get("image_provider_policy", registry["default_policy"])
    if policy_name not in registry["policies"]:
        raise ValueError(f"Unknown image provider policy: {policy_name}")
    policy = registry["policies"][policy_name]
    assets = library["assets"]
    ids = [asset["id"] for asset in assets]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in ids):
        raise ValueError("Image assets need unique safe IDs")
    snapshot = {"policy_name": policy_name, "policy": policy,
                "providers": {tier["provider"]: registry["providers"][tier["provider"]] for tier in policy["tiers"]},
                "width": config["image_width"], "height": config["image_height"]}
    policy_path = store.root / "image-provider-policy.json"
    if policy_path.exists() and read(policy_path) != snapshot:
        raise ValueError("This run has a different saved image provider policy; use its frozen settings or a new project")
    write(policy_path, snapshot)
    previous_report_path = store.root / "image-escalation-report.json"
    previous_report = read(previous_report_path) if previous_report_path.exists() else {}
    checkpoints = {}
    output, histories, states = {}, {name: [] for name in ids}, {}
    for asset in assets:
        checkpoint = store.root / "images/provider-selections" / (digest({"asset": asset, "style": library["style"], "routing": snapshot})[:20] + ".json")
        checkpoints[asset["id"]] = checkpoint
        if checkpoint.exists():
            value = read(checkpoint)
            path = Path(value["file"])
            if value.get("asset") != asset["id"] or value.get("review", {}).get("pass") is not True or not path.is_file() or digest(path.read_bytes()) != value.get("sha256"):
                raise ValueError(f"Accepted provider checkpoint is invalid: {checkpoint}")
            output[asset["id"]] = value
            histories[asset["id"]] = previous_report.get("histories", {}).get(asset["id"], [])
            states[asset["id"]] = {"status": "accepted", "provider": value.get("provider_id"), "total_attempts": value.get("total_attempts")}
    def save_report():
        write(store.root / "image_manifest.json", output)
        write(store.root / "image-escalation-report.json", {"policy": policy_name, "assets": states, "histories": histories})
    save_report()
    store.event("image-providers", "planned", {"planned_images": len(assets), "reused_images": len(output),
        "policy": policy_name, "tiers": [tier["provider"] for tier in policy["tiers"]], "max_total_attempts": policy["max_total_attempts"]})
    for tier_index, tier in enumerate(policy["tiers"]):
        remaining = []
        for asset in assets:
            name = asset["id"]
            if name in output:
                continue
            if tier_index and states.get(name, {}).get("action") != "escalate":
                continue
            available = policy["max_total_attempts"] - len(histories[name])
            if available <= 0:
                states[name] = {**states.get(name, {}), "status": "budget_exhausted", "action": "stop"}
                continue
            remaining.append((asset, min(tier["max_attempts"], available)))
        # Batch equal-budget assets to avoid loading/unloading a model per image.
        for budget in sorted({count for _, count in remaining}, reverse=True):
            batch = [asset for asset, count in remaining if count == budget]
            current = provider_config(config, tier["provider"], registry["providers"][tier["provider"]])
            current.update(image_tier=tier_index, image_max_attempts=budget,
                           image_planned_total=len(assets),
                           image_continue_on_exhaustion=True, image_manifest_seed=output,
                           image_prior_attempts={asset["id"]: list(histories[asset["id"]]) for asset in batch},
                           image_escalation_confidence=policy["escalation_confidence"],
                           image_min_escalation_attempts=policy["min_escalation_attempts"],
                           image_has_next_tier=tier_index + 1 < len(policy["tiers"]))
            for asset in batch:
                name = asset["id"]
                if tier_index:
                    store.event(name, "escalating", {"from_provider": histories[name][-1]["provider"], "to_provider": tier["provider"],
                        "tier": tier_index, "tier_attempt": 0, "total_attempts": len(histories[name]),
                        "max_attempts": budget, "diagnosis": histories[name][-1]["review"].get("diagnosis")})
            try:
                result = generate_images_single(current, store, {**library, "assets": batch}, models, on_progress=on_progress)
                for asset in batch:
                    name = asset["id"]
                    old_count = len(histories[name])
                    histories[name].extend(attempt_history(current, store, library, asset))
                    if name in result:
                        value = {**result[name], "provider_id": tier["provider"], "tier": tier_index,
                                 "total_attempts": len(histories[name]) or result[name].get("total_attempts", 1)}
                        output[name] = value
                        write(checkpoints[name], value)
                        states[name] = {"status": "accepted", "provider": tier["provider"], "total_attempts": value["total_attempts"]}
                    else:
                        review = histories[name][-1]["review"]
                        action = escalation_decision(review, histories[name], {**current, "image_prior_count": old_count})
                        if not current["image_has_next_tier"]:
                            action = "stop"
                        states[name] = {"status": "review_exhausted", "provider": tier["provider"],
                                        "total_attempts": len(histories[name]), "action": action, "diagnosis": review.get("diagnosis")}
            except Exception as exc:
                # Partial acceptances are already durably checkpointed by the adapter.
                output.update(read(store.root / "image_manifest.json"))
                for asset in batch:
                    name = asset["id"]
                    histories[name].extend(attempt_history(current, store, library, asset, include_unreviewed=True))
                    if name in output:
                        write(checkpoints[name], output[name])
                        states[name] = {"status": "accepted", "provider": output[name].get("provider_id"),
                                        "total_attempts": len(histories[name]) or output[name].get("total_attempts", 1)}
                    else:
                        states[name] = {"status": "backend_or_adapter_failed", "provider": tier["provider"],
                                        "total_attempts": len(histories[name]), "error": str(exc)}
                save_report()
                raise
            save_report()
    missing = [name for name in ids if name not in output]
    save_report()
    if missing:
        store.event("image-providers", "exhausted", {"assets": missing, "report": str(store.root / "image-escalation-report.json")})
        raise RuntimeError("Image assets still need review after bounded provider recovery: " + ", ".join(missing))
    return output
