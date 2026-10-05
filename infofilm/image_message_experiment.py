"""Full-resolution, message-preserving image repair pilot. Prepare unless --execute.

Reuses the benchmark's provider transport, GPU lease and timing instrumentation;
does not change production generation or the frozen ladder experiment.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import time
from pathlib import Path

from . import media
from .composition import asset_composition
from .core import ROOT, Store, configuration, digest, read, write
from .image_experiment import (MeasuredModels, case_metrics, model_usage,
                               measure_generation, provider_session,
                               record_metric, render_take, validate_corpus)
from .image_providers import IMAGE_REVIEW_REASONS, image_review_errors, provider_config

PROTOCOL = "infofilm.image-message-repair-experiment.v1"


class RepairContractError(ValueError):
    pass


def annotated_corpus(parent, annotations):
    cases = validate_corpus(parent)
    if annotations.get("schema_version") != "infofilm.manual-visual-messages.v1":
        raise ValueError("Need manual visual-message annotations")
    entries = annotations.get("cases", {})
    if set(entries) != {c["case_id"] for c in cases}:
        raise ValueError("Manual annotations must cover exactly the frozen cases")
    result = copy.deepcopy(parent)
    for case in result["cases"]:
        annotation = entries[case["case_id"]]
        for key in ["visual_message", "setting", "essential_constraints", "initial_scene"]:
            if not isinstance(annotation.get(key), str) or not 20 <= len(annotation[key].strip()) <= 2000:
                raise ValueError(f"{case['case_id']} needs a meaningful manual {key}")
        case["visual_message"] = annotation["visual_message"]
        case["message_annotation"] = copy.deepcopy(annotation)
        case["library"]["assets"][0]["visual_message"] = annotation["visual_message"]
        for scene in case["library"].get("scene_contexts_by_asset", {}).get(case["asset_id"], []):
            scene["visual_message"] = annotation["visual_message"]
    result.update(parent_corpus_sha256=digest(parent), parent_case_sha256=parent["case_sha256"],
                  annotation_schema=annotations["schema_version"], annotations_sha256=digest(annotations),
                  annotation_method=annotations["annotation_method"], case_sha256=digest(result["cases"]))
    return result


def validate_messages(corpus):
    cases = validate_corpus(corpus)
    for case in cases:
        message = case.get("visual_message")
        annotation = case.get("message_annotation", {})
        if (not isinstance(message, str) or not 20 <= len(message) <= 2000 or
                annotation.get("visual_message") != message or
                case["library"]["assets"][0].get("visual_message") != message):
            raise ValueError("Visual message must be present and identical across case and asset")
        for key in ["setting", "essential_constraints", "initial_scene"]:
            if not isinstance(annotation.get(key), str) or not annotation[key].strip():
                raise ValueError("Missing manual scene/context annotation")
        for scene in case["library"].get("scene_contexts_by_asset", {}).get(case["asset_id"], []):
            if scene.get("visual_message") != message:
                raise ValueError("Scene visual message differs from its asset")
    return cases


def visual_style(case):
    # Preserve subject treatment/mood without forwarding chapter titles, overlay
    # instructions, speculative props or the original mixed style wrapper.
    style = case["library"].get("style", "").lower()
    if style == "editorial":
        return "Historical editorial illustration with period textures and a restrained, serious tone."
    if "warm" in style or "golden-hour" in style:
        return "Cinematic historical illustration, warm natural lighting, earth tones with restrained gold accents."
    return "Cinematic historical illustration, serious reflective mood, natural lighting and muted earth tones."


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
    a = case["message_annotation"]
    # Semantic guardrails belong to the planner contract. Do not forward
    # abstract legal explanations or lists of unwanted objects to the generator.
    return ("Setting: " + a["setting"] +
            "\nVisual treatment: " + visual_style(case) + "\nComposition: " + layout_direction(case) +
            "\nScene: " + scene)


def initial_seed(config, case):
    # Adding message metadata must not silently change the initial benchmark seed.
    library = copy.deepcopy(case["library"])
    library["assets"][0].pop("visual_message", None)
    return int(media.image_identity(config, library, library["assets"][0])[:7], 16)


def review_errors(value, message):
    errors = image_review_errors(value)
    if not isinstance(value, dict):
        return errors
    if value.get("visual_message") != message:
        errors.append("Echo the immutable visual_message exactly")
    allowed = {"none"} if value.get("pass") is True else {"targeted", "simplify", "diagnose_backend"}
    if value.get("repair_mode") not in allowed:
        errors.append("Acceptance needs none; rejection needs targeted, simplify or diagnose_backend")
    if not isinstance(value.get("mode_reason"), str) or not value["mode_reason"].strip():
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
    message = case["visual_message"]
    packet = {"visual_message": message, "current_prompt": prompt}
    task = (
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
        "Do not diagnose provider weakness or recommend model escalation from this single image. Technical corruption "
        "uses diagnose_backend. Return {visual_message:string copied exactly, pass:boolean, reason_keys:[allowed key], "
        "issues:[concrete visible defect string], limitations:[uncertain/minor observation string], "
        "repair_mode:'none'|'targeted'|'simplify'|'diagnose_backend', mode_reason:string, correction:string}. "
        "A pass has empty issues/reason_keys/correction and repair_mode none. A failure needs evidence and correction.\n"
        "REASON KEYS:\n" + json.dumps(IMAGE_REVIEW_REASONS) + "\nCURRENT INPUT:\n" + json.dumps(packet)
    )
    feedback = ""
    for index in range(max_json_attempts):
        value = models.call("reviewer", name + f"-json-{index}", task + feedback, image_path=candidate, max_tokens=2200)
        errors = review_errors(value, message)
        if not errors:
            return value
        feedback = "\nCorrect the response schema for this same current image. Errors: " + json.dumps(errors)
    raise RepairContractError("Current-image review JSON contract failed: " + "; ".join(errors))


def normalized_scene(text):
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def repair_errors(value, case, last_scene, review):
    if not isinstance(value, dict):
        return ["Expected one replacement-prompt object"]
    errors = media.replacement_prompt_errors(value, last_scene)
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
        "Do not append prohibitions or use exclusion clauses such as no, not or without. "
        "Return {visual_message:string copied exactly, repair_mode:'targeted'|'simplify', prompt:string, "
        "simplifications:[string], message_preserved:string}. The replacement must materially differ from current_scene.\n"
        "INPUT:\n" + json.dumps(context)
    )
    feedback = ""
    for index in range(max_json_attempts):
        value = models.call("planner", name + f"-json-{index}", task + feedback, max_tokens=2200)
        errors = repair_errors(value, case, last_scene, review)
        if not errors:
            value["prompt"] = value["prompt"].strip()
            return value
        feedback = "\nPrevious replacement failed validation; return a corrected replacement. Errors: " + json.dumps(errors)
    raise RepairContractError("Replacement blocked before another image: " + "; ".join(errors))


def run_message_loop(config, store, case, models, settings):
    message = case["visual_message"]
    state_path = store.root / "message-state.json"
    state = read(state_path) if state_path.exists() else {
        "visual_message": message, "current_scene": case["message_annotation"]["initial_scene"],
        "takes": [], "status": "running", "repairs": []}
    if state["visual_message"] != message:
        raise ValueError("Checkpoint message changed")
    for take in state["takes"]:
        if digest(Path(take["candidate"]).read_bytes()) != take["sha256"]:
            raise ValueError("Saved candidate missing or changed")
    if state["status"] != "running":
        return state
    write(state_path, state)

    def finish_repair():
        if not state.get("pending_repair"):
            return
        last = state["takes"][-1]
        repair = repair_scene(models, case, last["scene"], last["settings"]["prompt"], last["review"],
                              f"message-repair-{len(state['takes'])}", settings["max_json_attempts"])
        errors = repair_errors(repair, case, last["scene"], last["review"])
        if errors:
            raise RepairContractError("Replacement blocked before another image: " + "; ".join(errors))
        write(store.root / "takes" / f"take-{len(state['takes']) - 1:02d}" / "repair.json", repair)
        state["repairs"].append(repair)
        state["current_scene"] = repair["prompt"]
        state.pop("pending_repair")
        write(state_path, state)
        store.event(case["asset_id"], "scene_replaced", repair)

    # Resume a pending planner call without reopening the GPU or re-rendering
    # the already rejected candidate.
    finish_repair()
    with provider_session(config, store):
        while len(state["takes"]) < settings["max_image_attempts"]:
            ordinal = len(state["takes"])
            root = store.root / "takes" / f"take-{ordinal:02d}"
            scene = state["current_scene"]
            params = {"prompt": generation_prompt(case, scene), "width": str(settings["full_size"][0]),
                      "height": str(settings["full_size"][1]), "steps": str(config["image_steps"]),
                      "guidance": "0", "seed": str(initial_seed(config, case) + ordinal), "output_format": "png"}
            request_contract = {"visual_message": message, "scene": scene, "provider": settings["provider"], "params": params}
            contract_path = root / "request-contract.json"
            if contract_path.exists() and read(contract_path) != request_contract:
                raise ValueError("Saved submission contract changed")
            write(contract_path, request_contract)
            store.event(case["asset_id"], "rendering", {"attempt": ordinal + 1,
                        "max_attempts": settings["max_image_attempts"], "provider": settings["provider"],
                        "dimensions": settings["full_size"], "steps": config["image_steps"], "visual_message": message})
            candidate = render_take(config, store, root, params)
            review_path = root / "review.json"
            if review_path.exists():
                review = read(review_path)
                if review_errors(review, message):
                    raise RepairContractError("Cached review fails the message contract")
            else:
                review = current_image_review(models, case, candidate, params["prompt"], f"message-review-{ordinal}",
                                              settings["max_json_attempts"])
                write(review_path, review)
            take = {"visual_message": message, "provider": settings["provider"], "stage": "final", "scene": scene,
                    "settings": params, "review": review, "candidate": str(candidate), "sha256": digest(candidate.read_bytes())}
            state["takes"].append(take)
            store.event(case["asset_id"], "accepted" if review["pass"] else "rejected", {
                "attempt": ordinal + 1, "visual_message": message, "repair_mode": review["repair_mode"],
                "reason_keys": review["reason_keys"], "issues": review["issues"], "correction": review["correction"]})
            if review["pass"]:
                state.update(status="accepted", selected={"visual_message": message, "asset": case["asset_id"],
                    "file": str(candidate), "sha256": take["sha256"], "settings": params, "review": review,
                    "provider_id": settings["provider"], "total_attempts": ordinal + 1})
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


def run_case(case, config, output, settings):
    root = Path(output) / case["case_id"]
    result_path = root / "result.json"
    if result_path.exists():
        result = read(result_path)
        if result.get('visual_message') != case['visual_message']:
            raise ValueError("Saved result message changed")
        if not result.get("resumable"):
            selected = result.get("selected")
            if selected and digest(Path(selected["file"]).read_bytes()) != selected["sha256"]:
                raise ValueError("Selected candidate missing or changed")
            return result
    store = Store(root)
    try:
        write(root / "case.json", case)
        models = MeasuredModels(config, store)
        error = None
        start = time.monotonic()
        try:
            with measure_generation(root):
                state = run_message_loop(config, store, case, models, settings)
        except Exception as exc:
            error = str(exc)
            state = read(root / "message-state.json") if (root / "message-state.json").exists() else {"status": "interrupted", "takes": [], "repairs": []}
            if isinstance(exc, RepairContractError):
                state.update(status="repair_blocked", error=error)
                write(root / "message-state.json", state)
        finally:
            record_metric(root, "wall", time.monotonic() - start)
        records = [json.loads(line) for line in (root / "metrics.jsonl").read_text().splitlines()]
        result = {"case_id": case["case_id"], "asset_id": case["asset_id"], "visual_message": case["visual_message"],
                  "historical_status": case["historical_status"], "status": state["status"],
                  "accepted": state["status"] == "accepted", "selected": state.get("selected"),
                  "attempts": len(state["takes"]), "takes": state["takes"], "repairs": state["repairs"],
                  "error": error, "resumable": state["status"] in {"running", "interrupted"},
                  "submission_records": len(list(root.glob("takes/*/provider-0.json"))),
                  "pipeline_wall_seconds": round(sum(r["seconds"] for r in records if r["kind"] == "wall"), 3),
                  "audit_policy": "No automatic audit after acceptance; pass ends the loop. Human review is pending.",
                  **case_metrics(root), **model_usage(root)}
        write(result_path, result)
        return result
    finally:
        store.db.close()


def validate_settings(settings, config):
    if settings.get("provider") not in config["image_provider_registry"]["providers"]:
        raise ValueError("Unknown single image provider")
    size = settings.get("full_size")
    if not isinstance(size, list) or len(size) != 2 or any(type(n) is not int or not 512 <= n <= 2816 or n % 16 for n in size):
        raise ValueError("Full size needs two 512–2816 pixel dimensions divisible by 16")
    for key, maximum in [("max_image_attempts", 5), ("max_json_attempts", 3)]:
        if type(settings.get(key)) is not int or not 1 <= settings[key] <= maximum:
            raise ValueError(f"Invalid bounded {key}")
    if settings.get("audit_after_acceptance") is not False:
        raise ValueError("This protocol ends on acceptance; audit_after_acceptance must be false")


def prepare(corpus, config, settings, cases, output):
    validate_messages(corpus)
    validate_settings(settings, config)
    all_cases = {c['case_id']: c for c in corpus['cases']}
    if not cases or any(c != all_cases.get(c['case_id']) for c in cases) or len({c['case_id'] for c in cases}) != len(cases):
        raise ValueError("Selected cases must be unique members of the annotated corpus")
    output = Path(output).resolve()
    for case in corpus["cases"]:
        source = Path(case["source_project"]).resolve()
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError("Experiment output must be separate from production projects")
    # An extension must also preserve the earlier experiment artifacts.
    frozen_experiment = (ROOT / "artifacts/image-ladder-experiment").resolve()
    if output == frozen_experiment or output.is_relative_to(frozen_experiment) or frozen_experiment.is_relative_to(output):
        raise ValueError("Use a separate output from the frozen ladder experiment")
    current = provider_config(config, settings["provider"], config["image_provider_registry"]["providers"][settings["provider"]])
    current.update(image_width=settings["full_size"][0], image_height=settings["full_size"][1])
    plan = {"protocol": PROTOCOL, "corpus_sha256": digest(corpus), "parent_corpus_sha256": corpus["parent_corpus_sha256"],
            "case_ids": [c["case_id"] for c in cases], "settings": settings,
            "provider": config["image_provider_registry"]["providers"][settings["provider"]],
            "initial_seed_by_case": {c['case_id']: initial_seed(current, c) for c in cases},
            "runtime": {k: current[k] for k in ["image_url", "tts_url", "planner_url", "reviewer_url", "gpu_lock"] if k in current},
            "code_sha256": {n: digest((ROOT / "infofilm" / n).read_bytes()) for n in [
                "image_message_experiment.py", "image_experiment.py", "media.py", "composition.py", "image_providers.py", "image_studio.py", "llm.py", "core.py"]},
            "review_input": "Current submitted prompt, immutable visual_message and actual image only; no repair history or provider identity.",
            "routing": "Full resolution throughout; one provider, targeted repair or simpler alternative; pass ends the loop.",
            "comparison_limits": "Initial scene wording and shared wrapper were cleaned manually, reviewer contract and budget changed. Earlier ladder/historical results are unmatched references; human quality review remains necessary."}
    plan_path = output / "plan.json"
    if plan_path.exists() and read(plan_path) != plan and any(p.name not in {"plan.json", "corpus.json"} for p in output.iterdir()):
        raise ValueError("Frozen executed plan differs; choose a new output directory")
    write(plan_path, plan)
    write(output / "corpus.json", corpus)
    return current, plan_path


def summarize(results, planned):
    usage = {}
    for r in results:
        for role, counts in r["llm_usage"].items():
            totals = usage.setdefault(role, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
            for key in totals:
                totals[key] += counts.get(key, 0)
    return {"planned_cases": planned, "completed_cases": len(results), "accepted": sum(r["accepted"] for r in results),
            "unresolved": sum(not r["accepted"] for r in results), "reviewed_candidates": sum(r["attempts"] for r in results),
            "submission_records": sum(r["submission_records"] for r in results),
            "statuses": {s: sum(r["status"] == s for r in results) for s in ["accepted", "exhausted", "repair_blocked", "backend_error", "running", "interrupted"]},
            "repairs": {mode: sum(p["repair_mode"] == mode for r in results for p in r["repairs"]) for mode in ["targeted", "simplify"]},
            "llm_usage": usage, "human_review": "pending; acceptance is an automated signal, not verified ground truth",
            **{k: round(sum(r[k] for r in results), 3) for k in ["generation_seconds", "review_seconds", "repair_seconds", "pipeline_wall_seconds"]}}


def save_gallery(output, results):
    output = Path(output).resolve()
    body = ['<!doctype html><meta charset="utf-8"><title>Message-preserving repair pilot</title>',
            '<style>body{font:16px system-ui;max-width:1100px;margin:30px auto;padding:16px}article{border-top:1px solid #bbb;padding:20px 0}img{max-width:100%;width:850px}pre{white-space:pre-wrap}</style>',
            '<h1>Message-preserving repair pilot</h1><p>Automated acceptance; human review pending. Rejected final candidates are labeled.</p>']
    for r in results:
        last = r['takes'][-1] if r['takes'] else None
        selected = r.get('selected')
        path = selected['file'] if selected else last['candidate'] if last else None
        body.append('<article><h2>' + html.escape(r['case_id'] + ' — ' + r['status']) + '</h2><p><strong>Message:</strong> ' + html.escape(r['visual_message']) + '</p>')
        if path:
            body.append('<img loading="lazy" src="' + html.escape(os.path.relpath(path, output), quote=True) + '" alt="' + html.escape(r['case_id'], quote=True) + '">')
        body.append('<p>' + str(r['attempts']) + ' reviewed candidates; ' + html.escape(' → '.join(p['repair_mode'] for p in r['repairs'])) + '</p><a href="' + r['case_id'] + '/result.json">Case record</a></article>')
    (output / 'gallery.html').write_text('\n'.join(body))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-corpus', type=Path, default=ROOT / 'pilots/image-ladder/cases.json')
    parser.add_argument('--annotations', type=Path, default=ROOT / 'pilots/image-message-repair/messages.json')
    parser.add_argument('--settings', type=Path, default=ROOT / 'pilots/image-message-repair/settings.json')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/image-message-repair-experiment')
    parser.add_argument('--cases', nargs='+')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    corpus = annotated_corpus(read(args.parent_corpus), read(args.annotations))
    cases = validate_messages(corpus)
    if args.cases:
        if set(args.cases) - {c['case_id'] for c in cases}:
            parser.error('Unknown case ID')
        cases = [c for c in cases if c['case_id'] in args.cases]
    settings, config = read(args.settings), configuration(args.config)
    current, plan_path = prepare(corpus, config, settings, cases, args.output)
    print(json.dumps({'cases': len(cases), 'execute': args.execute, 'plan': str(plan_path), 'max_image_attempts': settings['max_image_attempts']}), flush=True)
    if not args.execute:
        return
    results = []
    for index, case in enumerate(cases, 1):
        print('[message-experiment] ' + json.dumps({'position': index, 'total': len(cases), 'case_id': case['case_id'], 'visual_message': case['visual_message']}), flush=True)
        result = run_case(case, current, args.output, settings)
        results.append(result)
        print('[case-result] ' + json.dumps({k: result[k] for k in ['case_id', 'status', 'attempts', 'generation_seconds', 'review_seconds', 'repair_seconds', 'error']}), flush=True)
        write(args.output / 'summary.json', summarize(results, len(cases)))
        save_gallery(args.output, results)
    print(json.dumps(summarize(results, len(cases)), indent=2))


if __name__ == '__main__':
    main()
