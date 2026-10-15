"""Versioned production image recovery: full resolution, one provider, preserve meaning.

Legacy snapshots retain their previous routing. A pass ends this loop; failed
scenes are repaired or simplified without presenting review history to the critic.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path

import httpx
from PIL import Image

from . import media
from .composition import asset_composition
from .core import digest, gpu_lease, read, write, write_bytes
from .image_providers import IMAGE_REVIEW_REASONS, image_review_errors, provider_config, check_candidate
from .image_pool import image_pool, in_flight
from .image_studio import park_idle_gpu_services

PROTOCOL = "infofilm.image-message-recovery.v1"
SETTINGS_KEYS = ("image_recovery_mode", "image_recovery_provider", "image_recovery_max_attempts", "image_terminal_recovery")

class RepairContractError(ValueError):
    pass


def negative_wording(prompt):
    return bool(re.search(r"\b(?:no|not|without|excluding|avoiding)\b", prompt, re.I))


def clean_repair(value):
    """Normalize a mistakenly repeated adapter wrapper before comparing scenes.

    The raw model response remains archived by Models.call. Only the Scene field
    is used; styling/layout remain under the immutable adapter contract.
    """
    if not isinstance(value, dict):
        return value
    value = dict(value)
    prompt = value.get("scene_prompt", value.get("prompt"))
    if isinstance(prompt, str):
        match = re.search(r"(?:^|\n)\s*Scene(?: illustration)?:\s*", prompt, re.I)
        if match:
            prompt = prompt[match.end():]
        if re.search(r"(?:^|\n)\s*(?:Setting|Visual treatment|Composition|Shared visual style):", prompt, re.I):
            prompt = "" # Ambiguous wrapper: request a structured scene-only repair.
        value["prompt"] = prompt.strip()
    return value


def layout_direction(case):
    contract = asset_composition(case["library"], case["library"]["assets"][0])
    # Translate the actual renderer's spatial contract into natural visual
    # direction. Do not ask the image generator to manufacture an overlay.
    density = [s.get("copy_density") for s in case["library"].get("scene_contexts_by_asset", {}).get(case["asset_id"], [])]
    if density and all(d == "minimal" for d in density):
        return "Natural full-frame composition. Essential objects remain clearly visible above the lower quarter; upper-right background is subdued."
    direction = contract["generation_direction"]
    if "right" in direction.lower():
        return "Group essential content in the right half, with subdued natural surroundings on the left and upper right."
    return "Natural full-frame composition with clearly visible essential objects and subdued surroundings."


def generation_prompt(case, scene):
    from .person_references import reference_addendum
    a = case["message_annotation"]
    # Semantic guardrails belong to the planner contract. Do not forward
    # abstract legal explanations or lists of unwanted objects to the generator.
    return ("Setting: " + a["setting"] +
            "\nVisual treatment: " + case["library"]["assets"][0]["visual_style"] + "\nComposition: " + layout_direction(case) +
            "\nScene: " + scene + reference_addendum(case['library']['assets'][0]))


def review_errors(value, message, *, require_inventory=False, reference_available=None):
    errors = image_review_errors(value, require_inventory=require_inventory, reference_available=reference_available)
    if not isinstance(value, dict):
        return errors
    if value.get("visual_message") != message:
        errors.append("Echo the immutable visual_message exactly")
    allowed = {"none"} if value.get("pass") is True else {"targeted", "simplify", "diagnose_backend"}
    if value.get("repair_mode") not in allowed:
        errors.append("Acceptance needs none; rejection needs targeted, simplify or diagnose_backend")
    if value.get("pass") is False and (not isinstance(value.get("mode_reason"), str) or not value["mode_reason"].strip()):
        errors.append("mode_reason must explain the concept/repair choice")
    if value.get("pass") is True and (value.get("issues") or value.get("correction")):
        errors.append("Acceptance must not contain blocking issues or a correction")
    if value.get("pass") is False and not str(value.get("correction", "")).strip():
        errors.append("Rejection needs actionable correction guidance")
    reasons = value.get("reason_keys")
    reasons = reasons if isinstance(reasons, list) else []
    if "backend_corruption" in reasons and value.get("repair_mode") != "diagnose_backend":
        errors.append("Technical corruption needs backend diagnosis")
    if value.get("repair_mode") == "diagnose_backend" and "backend_corruption" not in reasons:
        errors.append("Backend diagnosis needs concrete technical corruption")
    return errors


def current_image_review(models, case, candidate, prompt, name, max_json_attempts=2):
    from .educational_review import review_guidance, component_checks_enabled, normalize_inventory
    from .person_references import reference_review_kwargs
    asset = case['library']['assets'][0]
    reference_kwargs = reference_review_kwargs(asset)
    message = case["visual_message"]
    packet = {"visual_message": message, "current_prompt": prompt}
    task = review_guidance(asset) + (
        "Inspect the attached actual image against only this current prompt and visual_message. "
        "There is no prior review history. Judge concrete visible meaning, factual contradictions, usability and framing. "
        "The image supports narration; it need not prove causation or encode every date, number or legal detail by itself. "
        "Accept a useful grounded illustration. Do not reject cosmetic preferences or speculate about unseen details. "
        "Titles, captions and labels are added by the video compositor: reject generated overlay lettering, title panels "
        "or solid banner blocks, but do not reject ordinary incidental writing on a required period object automatically. "
        "For symbolic imagery distinguish metaphors from literal claims. An optional object, map or exact character likeness "
        "is not essential just because a previous scene description chose it. Do not excuse wrong era, modern political "
        "borders standing in for a historical empire, or demonstrably wrong troop identity. "
        "On rejection choose targeted only when the current depiction remains useful and a small concrete correction "
        "has a clear path: ambiguous noun, wrong prop/clothing or obscured detail. Choose simplify immediately when the "
        "depiction is overloaded, relies on many exact relationships or distracting details, or remains unclear. "
        "Explain what informative meaning must survive; let the planner choose a simpler subject-specific depiction. "
        "Prefer affirmative corrections such as plain parchment surfaces over lists of excluded objects. "
        "Do not diagnose provider weakness or recommend model escalation from this single image. Technical corruption "
        "uses diagnose_backend. Return {visual_message:string copied exactly, pass:boolean, reason_keys:[allowed key], "
        "issues:[concrete visible defect string], limitations:[uncertain/minor observation string], "
        "repair_mode:'none'|'targeted'|'simplify'|'diagnose_backend', mode_reason:string, correction:string, "
        "message_marks:number 0..50 for conveying the informative meaning, prompt_marks:number 0..30 for matching the "
        "requested concrete scene, usability_marks:number 0..20 for a coherent, usable illustration}. "
        "Marks are independent of pass/fail: a failed image still gets a meaningful score, used to rank takes. "
        "A pass has empty issues/reason_keys/correction and repair_mode none. A failure needs evidence and correction.\n"
        "REASON KEYS:\n" + json.dumps(IMAGE_REVIEW_REASONS) + "\nCURRENT INPUT:\n" + json.dumps(packet)
    )
    if reference_kwargs:
        task = ("The first image is the generated scene; the second is a labeled, sourced appearance reference card. "
                "Compare observable appearance (face shape, hair, distinctive features) in the requested treatment, "
                "allowing pose, scene lighting and legitimate period-specific clothing changes. Do not infer identity "
                "from pixels or demand a pixel copy. Treat a materially different depiction of a planned named figure "
                "as semantic_mismatch; minor uncertain detail goes in limitations. Card labels belong only to the "
                "reference. Planned people: " + json.dumps(asset.get('people_in_frame', [])) + "\n" + task)
    feedback = ""
    for index in range(max_json_attempts):
        try:
            value = models.call("reviewer", name + f"-json-{index}", task + feedback, image_path=candidate,
                                max_tokens=2200, **reference_kwargs)
        except ValueError as exc:
            errors = ["Return one valid JSON review object: " + str(exc)]
        else:
            value = normalize_inventory(value)
            errors = review_errors(value, message, require_inventory=component_checks_enabled(asset), reference_available=bool(reference_kwargs))
            from .image_terminal import mark_errors, total_marks
            errors += mark_errors(value)
        if not errors:
            return {**value, 'score': total_marks(value)}
        feedback = "\nCorrect the response schema for this same current image. Errors: " + json.dumps(errors)
    raise RepairContractError("Current-image review JSON contract failed: " + "; ".join(errors))


def normalized_scene(text):
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def repair_errors(value, case, last_scene, review):
    if not isinstance(value, dict):
        return ["Expected one replacement-prompt object"]
    errors = []
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not 40 <= len(prompt.strip()) <= 3500:
        errors.append("Need a complete scene description of 40–3500 characters")
    if isinstance(value.get("prompt"), str) and normalized_scene(value["prompt"]) == normalized_scene(last_scene):
        errors.append("Punctuation or whitespace changes are not a meaningful scene repair")
    if value.get("visual_message") != case["visual_message"]:
        errors.append("Preserve and echo visual_message exactly")
    if value.get("repair_mode") not in {"targeted", "simplify"}:
        errors.append("Choose targeted or simplify")
    if review["repair_mode"] == "simplify" and value.get("repair_mode") != "simplify":
        errors.append("Reviewer requested simplification; choose a simpler depiction")
    simplified = value.get("simplifications")
    if not isinstance(simplified, list) or any(not isinstance(s, str) or not s.strip() for s in simplified):
        errors.append("simplifications must list removed optional complexity")
    elif value.get("repair_mode") == "simplify" and not simplified:
        errors.append("Simplification needs a concrete removed/replaced complexity")
    if not isinstance(value.get("message_preserved"), str) or not value["message_preserved"].strip():
        errors.append("Explain how the new depiction preserves the message")
    return errors


def repair_scene(models, case, last_scene, last_prompt, review, name, max_json_attempts=2):
    asset = case["library"]["assets"][0]
    facts = [f for f in case["source_context"]["facts"] if f["id"] in asset.get("fact_ids", [])]
    context = {"visual_message": case["visual_message"], "current_scene": last_scene,
               "current_prompt": last_prompt, "latest_review": review,
               "essential_constraints": case["message_annotation"]["essential_constraints"],
               "source_facts": facts}
    if asset.get('people_in_frame'):
        context['people_in_frame'] = asset['people_in_frame']
        context['reference_card'] = asset.get('reference_card')
    task = (
        "Replace a failed educational image scene while preserving its immutable visual_message and supplied facts. "
        "Use the latest actual review only. Choose targeted if a small concrete correction can rescue a useful concept. "
        "Prefer simplify when the composition is incidental, overloaded or hard to interpret; you may simplify even if "
        "the reviewer suggested targeted. A simplify recommendation must yield an alternative with less optional complexity. "
        "Change the depiction, not its informative relationship. Keep it subject-specific rather than a generic symbolic "
        "object. A narrative explanation does not demand that a single image prove causality or every numerical detail. "
        "Exact counts essential to the message remain binding if depicted. Optional maps, split panels, characters, "
        "diagrams, lettering, decorative objects and simultaneous actions may be replaced. Character likeness and "
        "continuity are not requirements. Preserve era, geographical intent, correct identities and the causal meaning. "
        "Resolve ambiguous nouns with concrete alternatives (document stamp or wax imprint for a legal seal). "
        "Write a complete positive scene description with desired objects and natural plain surfaces. Return scene wording "
        "only; the adapter retains setting/style/layout. Do not mention titles, banners, text overlays or unwanted objects. "
        "Prefer positive descriptions; plain surfaces are often clearer than exclusions. "
        "A useful scene is not rejected merely for using ordinary negative wording. "
        "Return {visual_message:string copied exactly, repair_mode:'targeted'|'simplify', prompt:string, "
        "simplifications:[string], message_preserved:string}. The replacement must materially differ from current_scene.\n"
        "INPUT:\n" + json.dumps(context)
    )
    if asset.get('people_in_frame'):
        task = task.replace("Character likeness and continuity are not requirements.",
                            "The listed named figures and their sourced appearance references are binding. "
                            "Keep this visible cast; simplify surroundings, props or action rather than replacing "
                            "a named figure with an invented face or deleting them. The adapter preserves the contact card.")
    feedback = ""
    for index in range(max_json_attempts):
        try:
            value = clean_repair(models.call("planner", name + f"-json-{index}", task + feedback, max_tokens=2200))
        except ValueError as exc:
            errors = ["Return one valid JSON replacement object: " + str(exc)]
        else:
            errors = repair_errors(value, case, last_scene, review)
        if not errors:
            value["prompt"] = value["prompt"].strip()
            if negative_wording(value["prompt"]):
                original = value
                try:
                    cleaned = clean_repair(models.call("planner", name + "-wording", task + "\nRewrite this valid replacement using affirmative desired surfaces where practical; preserve its depiction. " + json.dumps(value), max_tokens=2200))
                    if not repair_errors(cleaned, case, last_scene, review):
                        value = cleaned
                except (ValueError, json.JSONDecodeError):
                    value = original
                value["wording_warning"] = negative_wording(value["prompt"])
            return value
        feedback = "\nPrevious replacement failed validation; return a corrected replacement. Errors: " + json.dumps(errors)
    raise RepairContractError("Replacement blocked before another image: " + "; ".join(errors))


@contextlib.contextmanager
def provider_session(config, store):
    if config.get("image_engine") == "openrouter":           # cloud tier: nothing to lease, load or unload
        yield
        return
    with gpu_lease(config, "images"), contextlib.ExitStack() as stack:
        studio = bool(config.get("image_engine"))
        if studio and config.get("image_park_idle_services"):
            stack.enter_context(park_idle_gpu_services(config, store))
        else:
            from .gpu_release import release_vibevoice
            release_vibevoice(config, store, request_fn=media.request)
            if studio:
                from .image_studio import release_qwen
                release_qwen(config)
                try:
                    yield
                finally:
                    media.request(config, "image", "POST", "/api/unload")
                return
            media.request(config, "image", "POST", "/api/load")
            deadline = time.monotonic() + 900
            while True:
                status = media.request(config, "image", "GET", "/api/status")
                if status["state"] == "loaded":
                    break
                if status["state"] == "error" or time.monotonic() > deadline:
                    raise RuntimeError(f"Image provider failed to load: {status}")
                time.sleep(2)
        try:
            yield
        finally:
            media.request(config, "image", "POST", "/api/unload")


def render_take(config, store, root, params, label):
    """Reuse submission reconciliation: resume jobs, never repeat an unknown POST."""
    from .image_studio import render_studio as studio_render
    root.mkdir(parents=True, exist_ok=True)
    candidate, provider = root / "candidate-0.png", root / "provider-0.json"
    if config.get("image_engine"):
        studio_render(config, store, root, 0, params)
    else:
        if provider.exists() and read(provider)["params"] != params:
            raise ValueError("Saved image submission settings changed")
        if not candidate.exists():
            if provider.exists() and read(provider).get("state") == "submission_started" and not read(provider).get("job_id"):
                # The POST never confirmed a job id (e.g. cut off by a power failure). The Qwen factory is local and
                # unbilled, so submitting again is safe; billed cloud tiers never take this path.
                store.event(label, "unconfirmed_submission_retried", {"provider_record": str(provider)})
                provider.unlink()
            with in_flight(config):
                if not provider.exists():
                    write(provider, {"state": "submission_started", "params": params})
                    from .person_references import submit_qwen_job
                    result = submit_qwen_job(config, params, media.request)
                    write(provider, {"state": "submitted", "params": params, "job_id": result["id"]})
                job_id = read(provider).get("job_id")
                if not job_id:
                    raise RuntimeError("Unknown Qwen submission outcome; reconcile before resubmitting")
                from .image_jobs import wait_qwen_job
                result = wait_qwen_job(config, store, provider, label, request_fn=media.request, wait_fn=media.wait_job)
            with httpx.Client(timeout=180) as client:
                response = client.get(config["image_url"] + result["url"])
                response.raise_for_status()
                write_bytes(candidate, response.content)
            write(root / "provider-result-0.json", result)
    check_candidate(candidate, store, label)
    with Image.open(candidate) as image:
        if image.size != (int(params['width']), int(params['height'])):
            raise ValueError(f"Provider returned {image.size}, expected requested preview/final dimensions")
    return candidate


def intact_checkpoint(state, root, store, case):
    """The saved message loop, or a fresh one when a saved take's image is missing or changed (a power cut emptied it):
    the asset starts again instead of stopping the chapter, since images never stop a film."""
    for take in state["takes"] + ([state["pending_review"]] if state.get("pending_review") else []):
        path = Path(take["candidate"])
        if not path.is_file() or digest(path.read_bytes()) != take["sha256"]:
            store.event(case.get("asset_id", "image"), "checkpoint_reset", {"reason": "saved candidate missing or changed",
                                                                           "candidate": str(path)})
            if (root / "takes").exists():     # old provider records must not be reused for the new takes
                (root / "takes").rename(root / f"takes-reset-{time.time_ns()}")
            return {"visual_message": state["visual_message"], "current_scene": case["message_annotation"]["initial_scene"],
                    "takes": [], "status": "running", "repairs": []}
    return state


def run_message_loop(config, store, root, case, models, settings, progress):
    message = case["visual_message"]
    state_path = root / "message-state.json"
    state = read(state_path) if state_path.exists() else {
        "visual_message": message, "current_scene": case["message_annotation"]["initial_scene"],
        "takes": [], "status": "running", "repairs": []}
    if state["visual_message"] != message:
        raise ValueError("Checkpoint message changed")
    state = intact_checkpoint(state, root, store, case)
    if state["status"] != "running":
        return state
    write(state_path, state)

    def finish_repair():
        if not state.get("pending_repair"):
            return
        last = state["takes"][-1]
        repair = repair_scene(models, case, last["scene"], last["settings"]["prompt"], last["review"],
                              f"message-repair-{case['asset_id']}-{root.name}-{len(state['takes'])}", settings["max_json_attempts"])
        errors = repair_errors(repair, case, last["scene"], last["review"])
        if errors:
            raise RepairContractError("Replacement blocked before another image: " + "; ".join(errors))
        write(root / "takes" / f"take-{len(state['takes']) - 1:02d}" / "repair.json", repair)
        state["repairs"].append(repair)
        state["current_scene"] = repair["prompt"]
        state.pop("pending_repair")
        write(state_path, state)
        store.event(case["asset_id"], "scene_replaced", {**progress, **repair})

    # Resume a pending planner call without reopening the GPU or re-rendering
    # the already rejected candidate.
    finish_repair()
    while len(state["takes"]) < settings["max_image_attempts"]:
        ordinal = len(state["takes"])
        take_root = root / "takes" / f"take-{ordinal:02d}"
        scene = state["current_scene"]
        params = {"prompt": generation_prompt(case, scene), "width": str(settings["full_size"][0]),
                  "height": str(settings["full_size"][1]), "steps": str(config["image_steps"]),
                  "guidance": "0", "seed": str(int(root.name[:7], 16) + ordinal), "output_format": "png"}
        from .person_references import bind_reference_params
        params = bind_reference_params(params, case['library']['assets'][0])
        request_contract = {"visual_message": message, "scene": scene, "provider": settings["provider"], "params": params}
        contract_path = take_root / "request-contract.json"
        if contract_path.exists() and read(contract_path) != request_contract:
            raise ValueError("Saved submission contract changed")
        write(contract_path, request_contract)
        store.event(case["asset_id"], "rendering", {**progress, "attempt": ordinal + 1,
                    "max_attempts": settings["max_image_attempts"], "provider": settings["provider"],
                    "dimensions": settings["full_size"], "steps": config["image_steps"], "visual_message": message})
        candidate = render_take(config, store, take_root, params, f"{case['asset_id']} {progress['position']}/{progress['planned_images']} take {ordinal+1}/{settings['max_image_attempts']}")
        take = {"visual_message": message, "provider": settings["provider"], "stage": "final", "scene": scene,
                "settings": params, "candidate": str(candidate), "sha256": digest(candidate.read_bytes())}
        # Generation is complete even if the critic returns invalid JSON. Keep
        # this candidate available for resumed review and terminal scoring.
        state['pending_review'] = take
        write(state_path, state)
        review_path = take_root / "review.json"
        if review_path.exists():
            review = read(review_path)
            if review_errors(review, message):
                raise RepairContractError("Cached review fails the message contract")
        else:
            review = current_image_review(models, case, candidate, params["prompt"], f"message-review-{case['asset_id']}-{root.name}-{ordinal}",
                                          settings["max_json_attempts"])
            write(review_path, review)
        take['review'] = review
        state["takes"].append(take)
        state.pop('pending_review', None)
        store.event(case["asset_id"], "accepted" if review["pass"] else "rejected", {
            **progress, "attempt": ordinal + 1, "visual_message": message, "repair_mode": review["repair_mode"],
            "reason_keys": review["reason_keys"], "issues": review["issues"], "correction": review["correction"]})
        if review["pass"]:
            state.update(status="accepted", selected={"visual_message": message, "asset": case["asset_id"],
                "file": str(candidate), "sha256": take["sha256"], "prompt": params["prompt"], "settings": params, "review": review,
                "provider_id": settings["provider"], "total_attempts": ordinal + 1})
            asset = case['library']['assets'][0]
            if 'people_in_frame' in asset:
                state['selected'].update({k: asset[k] for k in ['people_in_frame', 'person_reference_status', 'reference_card', 'reference_faces'] if k in asset})
        elif review["repair_mode"] == "diagnose_backend":
            state["status"] = "backend_error"
        elif ordinal + 1 >= settings["max_image_attempts"]:
            state["status"] = "exhausted"
        else:
            state["pending_repair"] = True
        write(state_path, state)
        if state["status"] != "running":
            break
        finish_repair()
    return state


def generate_message_images(config, store, library, models, *, on_progress=None):
    from .image_terminal import recover, selectable, terminal_settings, needs_generation, terminal_candidates
    terminal = terminal_settings(config)
    registry = config.get("image_provider_registry", {})
    provider_id = config.get("image_recovery_provider", "qwen_local")
    if provider_id not in registry.get("providers", {}):
        raise ValueError("Unknown image recovery provider")
    maximum = config.get("image_recovery_max_attempts", 3)
    if type(maximum) is not int or not 1 <= maximum <= 5:
        raise ValueError("image_recovery_max_attempts must be 1–5")
    current = provider_config(config, provider_id, registry["providers"][provider_id])
    settings = {"provider": provider_id, "max_image_attempts": maximum,
                "max_json_attempts": 2, "full_size": [current["image_width"], current["image_height"]]}
    policy = {"protocol": PROTOCOL, "settings": settings, "provider": registry["providers"][provider_id]}
    policy_path = store.root / "image-recovery-policy.json"
    if policy_path.exists() and read(policy_path) != policy:
        raise ValueError("Saved image recovery policy changed; archive the prior image revision first")
    write(policy_path, policy)
    source = media.image_source_context(store.root, library)
    total = len(library["assets"])
    previous = read(store.root / 'image-recovery-report.json') if (store.root / 'image-recovery-report.json').exists() else {}
    previous_failures = {row['checkpoint']: row for row in previous.get('unresolved', []) if row.get('checkpoint')}
    output, pending, unresolved, rescue_pending = {}, [], [], []
    for position, asset in enumerate(library["assets"], 1):
        for field in ("visual_message", "setting", "visual_style", "prompt"):
            if not isinstance(asset.get(field), str) or not asset[field].strip():
                raise ValueError(f"{asset['id']} missing {field}")
        case = {"asset_id": asset["id"], "visual_message": asset["visual_message"],
                "library": {**library, "assets": [asset]}, "source_context": source,
                "message_annotation": {"setting": asset["setting"], "initial_scene": asset["prompt"],
                    "essential_constraints": "Preserve the message, sourced era, geographical intent and essential facts."}}
        identity = digest({"policy": policy, "asset": asset,
                           "composition": asset_composition(library, asset), "source_context": source})
        root = store.root / "images" / identity[:20]
        selected = root / "selected.json"
        if selected.exists():
            value = read(selected)
            if (value.get("visual_message") != asset["visual_message"] or value.get("asset") != asset["id"]
                    or not selectable(value)
                    or digest(Path(value["file"]).read_bytes()) != value.get("sha256")):
                raise ValueError("Accepted image checkpoint changed")
            output[asset["id"]] = value
        else:
            saved = read(root / 'message-state.json') if (root / 'message-state.json').exists() else None
            prior = previous_failures.get(str(root), {})
            if terminal['enabled'] and saved and terminal_candidates(saved) and (
                    saved.get('status') in {'exhausted', 'best_available'} or prior.get('status') == 'repair_blocked'):
                rescue_pending.append((position, case, root))
            else:
                pending.append((position, case, root))
    write(store.root / "image_manifest.json", output)
    store.event("images", "planned", {"planned_images": total, "accepted_images": len(output),
                "needs_review_images": sum(bool(v.get('needs_review')) for v in output.values()),
                "remaining_images": len(pending)+len(rescue_pending), "max_attempts": maximum, "provider": provider_id,
                "recovery_mode": "message_v1"})
    if not pending and not rescue_pending:
        return output
    # Acquire the standard lease only when standard work remains. Premium
    # recovery happens after this context exits, never under a nested GPU lock.
    session = provider_session(current, store) if pending else contextlib.nullcontext()
    pool = image_pool(current) if pending else None
    lock = pool.lock if pool else None

    def produce(position, case, root):
        """One asset's message loop. Assets are independent (references are bound beforehand), so the image pool runs
        several at once; the shared manifest, lists and progress are updated under the pool's lock."""
        with lock:
            progress = {"position": position, "planned_images": total,
                        "completed_images": len(output)+len(unresolved), "accepted_images": len(output),
                        "blocked_images": len(unresolved), "remaining_images": total-len(output)-len(unresolved)}
        try:
            state = run_message_loop(current, store, root, case, models, settings, progress)
            with lock:
                if state["status"] == "accepted":
                    value = {**state["selected"], "recovery_protocol": PROTOCOL}
                    write(root / "selected.json", value)
                    output[case["asset_id"]] = value
                    write(store.root / "image_manifest.json", output)
                elif state["status"] == "backend_error":
                    raise RuntimeError(f"{case['asset_id']} needs backend diagnosis")
                elif terminal['enabled']:
                    rescue_pending.append((position, case, root))
                else:
                    unresolved.append({"asset": case["asset_id"], "status": state["status"],
                                       "attempts": len(state["takes"]), "checkpoint": str(root)})
        except RepairContractError as exc:
            # Keep the pending review/repair checkpoint resumable; continue other assets.
            with lock:
                if terminal['enabled'] and (root / 'message-state.json').exists() and terminal_candidates(read(root / 'message-state.json')):
                    rescue_pending.append((position, case, root))
                else:
                    unresolved.append({"asset": case["asset_id"], "status": "repair_blocked",
                                       "error": str(exc), "checkpoint": str(root)})
                queued = any(c['asset_id'] == case['asset_id'] for _, c, _ in rescue_pending)
            store.event(case["asset_id"], "repair_blocked", {**progress, "error": str(exc),
                        "terminal_recovery_queued": queued})
        with lock:
            store.event("images", "progress", {**progress, "completed_images": len(output)+len(unresolved),
                        "accepted_images": len(output), "blocked_images": len(unresolved),
                        "remaining_images": total-len(output)-len(unresolved)})
            if on_progress:
                on_progress()

    with session:
        if pending:
            store.event("images", "pool", {"workers": min(pool.size, len(pending)), "pool_size": pool.size})
            pool.run(pending, lambda job: produce(*job))
    rescue_pending.sort(key=lambda job: job[0])
    order = {case['asset_id']: position for position, case, _ in pending}
    unresolved.sort(key=lambda row: order.get(row['asset'], 0))
    premium_session = contextlib.nullcontext()
    if rescue_pending and any(needs_generation(config, root, case, terminal) for _,case,root in rescue_pending):
        premium_config = provider_config(config, terminal['provider'], registry['providers'][terminal['provider']])
        premium_session = provider_session(premium_config, store)
    with premium_session:
        for position, case, root in rescue_pending:
            value = {**recover(config, store, root, case, models, terminal, session_managed=True), 'recovery_protocol': PROTOCOL}
            write(root / 'selected.json', value)
            output[case['asset_id']] = value
            write(store.root / 'image_manifest.json', output)
            store.event('images', 'progress', {'position': position, 'planned_images': total,
                 'completed_images': len(output)+len(unresolved), 'accepted_images': len(output),
                 'needs_review_images': sum(bool(v.get('needs_review')) for v in output.values()),
                 'remaining_images': total-len(output)-len(unresolved), 'terminal_recovery': True})
            if on_progress:
                on_progress()
    write(store.root / "image-recovery-report.json", {"protocol": PROTOCOL, "planned_images": total,
                "accepted_images": len(output), "unresolved": unresolved,
                "needs_review": [key for key,value in output.items() if value.get('needs_review')]})
    if unresolved:
        raise RuntimeError("Images need review: " + ", ".join(item["asset"] for item in unresolved))
    return output
