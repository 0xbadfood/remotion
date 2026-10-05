"""Isolated matched experiments for full-resolution repair versus a sniff ladder.

Production artifacts are read only. No provider calls happen without --execute.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import html
import json
import re
import statistics
import time
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from . import media
from .composition import asset_composition
from .core import ROOT, Store, configuration, digest, gpu_lease, read, write
from .image_providers import check_candidate, escalation_decision, has_meaningful_repairs, provider_config, validate_registry
from .image_studio import park_idle_gpu_services, render_studio
from .llm import Models

SCHEMA = "infofilm.image-experiment-corpus.v1"
PROTOCOL = "infofilm.image-ladder-experiment.v1"


def collect_cases(search_root, count=20):
    groups = {}
    active_projects = set()
    for status_path in Path(search_root).rglob("longform-status.json"):
        for chapter in read(status_path).get("chapters", []):
            if chapter.get("project") and chapter.get("status") not in {"succeeded", "failed"}:
                active_projects.add(Path(chapter["project"]).resolve())
    for library_path in sorted(Path(search_root).rglob("scene-image-prompts.json")):
        project = library_path.parent
        if project.resolve() in active_projects:
            continue
        library = read(library_path)
        report_path = project / "image-escalation-report.json"
        report = read(report_path) if report_path.exists() else {}
        config_path = project / "config-snapshot.json"
        if not config_path.exists():
            continue
        config = read(config_path)
        candidates = []
        for asset in library["assets"]:
            history = report.get("histories", {}).get(asset["id"], [])
            if not history:
                root = project / "images" / media.image_identity(config, library, asset)[:20]
                paths = [p for p in root.glob("review-*.json") if re.fullmatch(r"review-\d+", p.stem)]
                for review_path in sorted(paths, key=lambda p: int(p.stem.split("-")[-1])):
                    index = int(review_path.stem.split("-")[-1])
                    provider_path = root / f"provider-{index}.json"
                    if provider_path.exists():
                        history.append({"provider": "qwen_local", "tier": 0, "attempt": index + 1,
                            "settings": read(provider_path)["params"], "review": read(review_path),
                            "candidate": str((root / f"candidate-{index}.png").resolve())})
            reviewed = [item for item in history if isinstance(item.get("review"), dict)]
            if len(reviewed) < 2 or reviewed[0]["review"].get("pass") is not False:
                continue
            recovered = any(item["review"].get("pass") is True for item in reviewed)
            subset = copy.deepcopy(library)
            subset["assets"] = [copy.deepcopy(asset)]
            subset["scene_contexts_by_asset"] = {asset["id"]: library.get("scene_contexts_by_asset", {}).get(asset["id"], [])}
            # Replay the failed initial scene, never the successful repair.
            submitted = reviewed[0]["settings"].get("prompt", "")
            if "Scene illustration: " in submitted:
                subset["assets"][0]["prompt"] = submitted.split("Scene illustration: ", 1)[1]
            case = {"source_project": str(project.resolve()), "asset_id": asset["id"],
                    "library": subset, "source_context": media.image_source_context(project, subset),
                    "initial_submitted_prompt": submitted,
                    "historical_status": "recovered" if recovered else "unresolved",
                    "historical_attempts": len(reviewed), "historical_history": reviewed,
                    "provenance": {str(p.resolve()): digest(p.read_bytes()) for p in
                        [library_path, config_path, project / "evidence-final.json", project / "creative-brief.json"] if p.exists()}}
            case["case_id"] = project.name + "--" + asset["id"]
            candidates.append(case)
        candidates.sort(key=lambda c: (c["historical_status"] != "unresolved", -c["historical_attempts"], c["asset_id"]))
        if candidates:
            groups[project.name] = candidates
    # Prioritize exhausted assets and multi-repair recoveries, with project
    # round-robin inside each difficulty bucket. Active chapters are excluded.
    selected = []
    for bucket in range(3):
        pools = {}
        for name, group in groups.items():
            pools[name] = [c for c in group if (0 if c['historical_status'] == 'unresolved' else 1 if c['historical_attempts'] >= 3 else 2) == bucket]
        while len(selected) < count and any(pools.values()):
            for group in pools.values():
                if group and len(selected) < count:
                    selected.append(group.pop(0))
    if len(selected) != count:
        raise ValueError(f"Only {len(selected)} eligible difficult assets; need {count}")
    for index, case in enumerate(selected, 1):
        case["case_id"] = f"T{index:02d}"
    return {"schema_version": SCHEMA, "selection": "Exclude active chapters; unresolved first, then recovered with 3+ reviewed takes, then 2-take recoveries; project round-robin within each bucket; initial take must fail",
            "case_count": count, "cases": selected, "case_sha256": digest(selected),
            "limitations": "Failure-enriched local historical subjects; not a representative success-rate sample or human acceptance."}


def validate_corpus(corpus):
    if corpus.get("schema_version") != SCHEMA or corpus.get("case_sha256") != digest(corpus.get("cases", [])):
        raise ValueError("Corpus schema/hash mismatch")
    cases = corpus["cases"]
    if corpus["case_count"] != len(cases) or len({c["case_id"] for c in cases}) != len(cases):
        raise ValueError("Corpus count or IDs are invalid")
    for case in cases:
        assets = case.get('library', {}).get('assets', [])
        if (not re.fullmatch(r'T\d{2,4}', case['case_id']) or len(assets) != 1 or
                assets[0].get('id') != case.get('asset_id') or not re.fullmatch(r'[A-Za-z0-9_-]+', case['asset_id'])):
            raise ValueError('Corpus cases need safe IDs and exactly one matching asset')
    return cases


def record_metric(root, kind, seconds, **detail):
    with (Path(root) / "metrics.jsonl").open("a") as handle:
        handle.write(json.dumps({"kind": kind, "seconds": round(seconds, 6), **detail}) + "\n")


class MeasuredModels(Models):
    def call(self, role, name, prompt, **kwargs):
        start = time.monotonic()
        try:
            return super().call(role, name, prompt, **kwargs)
        finally:
            kind = "independent_audit" if name.startswith("independent-") else "review" if kwargs.get("image_path") else "repair"
            record_metric(self.store.root, kind, time.monotonic() - start, role=role, name=name)


@contextlib.contextmanager
def measure_generation(root):
    """Measure the same adapter operations for both strategies, including errors."""
    originals = media.request, media.wait_job, render_studio
    def measured(fn, *args, **kwargs):
        start = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            record_metric(root, "generation", time.monotonic() - start)
    def request(*args, **kwargs):
        if args[1:4] == ("image", "POST", "/api/generate"):
            return measured(originals[0], *args, **kwargs)
        return originals[0](*args, **kwargs)
    def wait(*args, **kwargs):
        return measured(originals[1], *args, **kwargs) if args[1] == "image" else originals[1](*args, **kwargs)
    def studio(*args, **kwargs):
        return measured(originals[2], *args, **kwargs)
    with patch("infofilm.media.request", side_effect=request), patch("infofilm.media.wait_job", side_effect=wait), \
         patch("infofilm.image_studio.render_studio", side_effect=studio):
        yield


@contextlib.contextmanager
def provider_session(config, store):
    with gpu_lease(config), contextlib.ExitStack() as stack:
        studio = bool(config.get("image_engine"))
        if studio and config.get("image_park_idle_services"):
            stack.enter_context(park_idle_gpu_services(config, store))
        else:
            health = media.request(config, "tts", "GET", "/health")
            if health.get("active_job") or health.get("queued_or_running_jobs"):
                raise RuntimeError("VibeVoice has active work; benchmark will not unload it")
            if health.get("model_loaded"):
                result = media.request(config, "tts", "POST", "/unload")
                if result.get("model_loaded") or result.get("unload_disabled"):
                    raise RuntimeError("VibeVoice did not release GPU memory")
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


def render_take(config, store, root, params):
    """Reuse submission reconciliation: resume jobs, never repeat an unknown POST."""
    from .image_studio import render_studio as studio_render
    root.mkdir(parents=True, exist_ok=True)
    candidate, provider = root / "candidate-0.png", root / "provider-0.json"
    if config.get("image_engine"):
        studio_render(config, store, root, 0, params)
    else:
        if provider.exists() and read(provider)["params"] != params:
            raise ValueError("Saved experiment submission settings changed")
        if not candidate.exists():
            if not provider.exists():
                write(provider, {"state": "submission_started", "params": params})
                result = media.request(config, "image", "POST", "/api/generate", data=params)
                write(provider, {"state": "submitted", "params": params, "job_id": result["id"]})
            job_id = read(provider).get("job_id")
            if not job_id:
                raise RuntimeError("Unknown Qwen submission outcome; reconcile before resubmitting")
            result = media.wait_job(config, "image", "/api/jobs/" + job_id, {"complete"}, {"error"}, store, root.name)
            with httpx.Client(timeout=180) as client:
                response = client.get(config["image_url"] + result["url"])
                response.raise_for_status()
                temporary = candidate.with_suffix(".tmp.png")
                temporary.write_bytes(response.content)
                temporary.replace(candidate)
            write(root / "provider-result-0.json", result)
    check_candidate(candidate, store, root.name)
    with Image.open(candidate) as image:
        if image.size != (int(params['width']), int(params['height'])):
            raise ValueError(f"Provider returned {image.size}, expected requested preview/final dimensions")
    return candidate


def ladder_transition(stage, review, action, *, has_next_tier, attempts_left, exhaustion_fallback=False):
    if review.get("pass") is True:
        return "accepted" if stage == "final" else "final"
    if "backend_corruption" in review.get("reason_keys", []) or action == "diagnose_backend":
        return "backend_error"
    # Low-step artifacts cannot establish a provider limitation.
    if stage != "draft" and action == "escalate" and has_next_tier:
        return "escalate"
    if not attempts_left:
        if stage != "draft" and has_next_tier and exhaustion_fallback:
            return "escalate"
        return "exhausted"
    if stage == "final" and set(review.get("reason_keys", [])) <= {"visual_artifact"}:
        return "final"
    return "preview"


def run_ladder(config, store, library, models, settings):
    registry = config["image_provider_registry"]
    policy = registry["policies"][config.get("image_provider_policy", registry["default_policy"]) ]
    asset = library["assets"][0]
    context = media.image_source_context(store.root, library)
    state_path = store.root / "ladder-state.json"
    state = read(state_path) if state_path.exists() else {"tier": 0, "stage": "draft", "prompt": asset["prompt"], "takes": [], "status": "running"}
    if state.get("pending_transition"):
        previous_root = store.root / "ladder-takes" / f"take-{len(state['takes']) - 1:02d}"
        finish_transition(state, state_path, models, library, asset, previous_root, context)
    if state["status"] == "accepted":
        selected = state['selected']
        if not Path(selected['file']).is_file() or digest(Path(selected['file']).read_bytes()) != selected['sha256']:
            raise ValueError('Accepted ladder checkpoint is missing or changed')
        return {asset["id"]: state["selected"]}
    if state["status"] == "backend_error":
        raise RuntimeError("Ladder image needs backend diagnosis; inspect the saved take and review")
    if state["status"] != "running":
        return {}
    for tier_index in range(state["tier"], len(policy["tiers"])):
        if state["status"] != "running":
            break
        tier = policy["tiers"][tier_index]
        current = provider_config(config, tier["provider"], registry["providers"][tier["provider"]])
        has_next = tier_index + 1 < len(policy["tiers"])
        with provider_session(current, store):
            while state["status"] == "running" and state["tier"] == tier_index:
                tier_takes = [t for t in state["takes"] if t["tier"] == tier_index]
                ordinal = len(state["takes"])
                if ordinal >= policy["max_total_attempts"] or len(tier_takes) >= tier["max_attempts"]:
                    state["status"] = "exhausted"
                    write(state_path, state)
                    break
                stage = state["stage"]
                root = store.root / "ladder-takes" / f"take-{ordinal:02d}"
                width, height = settings["full_size"] if stage == "final" else settings["preview_size"]
                steps = settings["low_steps"].get(tier["provider"], 8) if stage == "draft" else current["image_steps"]
                seed = int(media.image_identity(current, library, asset)[:7], 16) + len(tier_takes)
                params = {"prompt": "Composition: " + asset_composition(library, asset)["generation_direction"] +
                    "\nShared visual style: " + library["style"] +
                    ("\nSubject context, governing era and setting: " + context["subject"] if context["subject"] else "") +
                    "\nScene illustration: " + state["prompt"], "width": str(width), "height": str(height),
                    "steps": str(steps), "guidance": "0", "seed": str(seed), "output_format": "png"}
                if current.get("image_engine"):
                    params["seed"] = str(int(digest({"asset": asset, "style": library["style"]})[:7], 16) + len(tier_takes))
                store.event(asset['id'], 'ladder_rendering', {'stage': stage, 'provider': tier['provider'],
                    'dimensions': [width, height], 'steps': steps, 'tier_attempt': len(tier_takes) + 1,
                    'total_attempt': ordinal + 1, 'max_total_attempts': policy['max_total_attempts']})
                candidate = render_take(current, store, root, params)
                if (root / "review.json").exists():
                    review = read(root / "review.json")
                else:
                    review = media.review_scene_image(models, library, asset, candidate, params,
                        f"ladder-review-{ordinal}", context, attempt_history=state["takes"],
                        provider_id=tier["provider"], review_stage="final" if stage == "final" else "preview")
                    write(root / "review.json", review)
                take = {"tier": tier_index, "provider": tier["provider"], "stage": stage,
                        "settings": params, "review": review, "candidate": str(candidate), "sha256": digest(candidate.read_bytes())}
                state["takes"].append(take)
                qualified = [t for t in state["takes"] if t["tier"] == tier_index and t["stage"] != "draft"]
                gate = {"image_min_escalation_attempts": policy["min_escalation_attempts"],
                        "image_escalation_confidence": policy["escalation_confidence"]}
                action = escalation_decision(review, qualified, gate) if stage != "draft" else "recheck"
                # A preview needs a remaining final-render slot. Do not spend
                # the last candidate on a preview that cannot be delivered.
                returns_to_preview = stage != 'final' or not set(review.get('reason_keys', [])) <= {'visual_artifact'}
                reserve = 1 if returns_to_preview and review['pass'] is False else 0
                remaining = (len(state["takes"]) + reserve < policy["max_total_attempts"] and
                             len(tier_takes) + 1 + reserve < tier["max_attempts"])
                fallback_ready = (settings.get('exhaustion_fallback', False) and
                                  has_meaningful_repairs(qualified, policy['min_escalation_attempts']))
                next_stage = ladder_transition(stage, review, action, has_next_tier=has_next and len(state["takes"]) < policy["max_total_attempts"],
                                               attempts_left=remaining, exhaustion_fallback=fallback_ready)
                if next_stage == 'escalate':
                    take['escalation_trigger'] = 'qualified_provider_failure' if action == 'escalate' else 'exhaustion_fallback'
                store.event(asset["id"], "ladder_reviewed", {"stage": stage, "dimensions": [width, height],
                    "steps": steps, "pass": review["pass"], "reason_keys": review["reason_keys"],
                    "issues": review.get('issues', []), "correction": review.get('correction', ''),
                    "action": next_stage, "total_attempts": len(state["takes"]), "provider": tier["provider"],
                    "escalation_trigger": take.get('escalation_trigger')})
                if next_stage == "accepted":
                    state.update(status="accepted", selected={"asset": asset["id"], "file": str(candidate),
                        "sha256": take["sha256"], "review": review, "settings": params,
                        "provider_id": tier["provider"], "total_attempts": len(state["takes"])})
                elif next_stage in {"backend_error", "exhausted"}:
                    state["status"] = next_stage
                else:
                    # Checkpoint the pending transition before a repair/model call.
                    state["pending_transition"] = next_stage
                write(state_path, state)
                if "pending_transition" in state:
                    finish_transition(state, state_path, models, library, asset, root, context)
    manifest = {asset["id"]: state["selected"]} if state["status"] == "accepted" else {}
    write(store.root / "image_manifest.json", manifest)
    if state["status"] == "backend_error":
        raise RuntimeError("Ladder image needs backend diagnosis; inspect the saved take and review")
    return manifest


def finish_transition(state, state_path, models, library, asset, root, context):
    next_stage = state.pop("pending_transition")
    if state["takes"][-1]["review"]["pass"] is False:
        state["prompt"] = media.repair_image_prompt(models, library, asset, root, 0,
            {**context, "previous_provider_attempts": state["takes"]})
    if next_stage == "escalate":
        state["tier"] += 1
        state["stage"] = "draft"
    else:
        state["stage"] = next_stage
    write(state_path, state)


def case_metrics(root):
    values = [json.loads(line) for line in (Path(root) / "metrics.jsonl").read_text().splitlines()] if (Path(root) / "metrics.jsonl").exists() else []
    return {kind + "_seconds": round(sum(v["seconds"] for v in values if v["kind"] == kind), 3)
            for kind in ["generation", "review", "repair", "independent_audit"]}


def baseline_takes(root):
    report = read(root / "image-escalation-report.json") if (root / "image-escalation-report.json").exists() else {}
    return [{**take, "stage": "final"} for values in report.get("histories", {}).values() for take in values]


def model_usage(root):
    usage, identities = {}, {}
    for response_path in (Path(root) / 'requests').glob('*.response.json'):
        request_path = response_path.with_name(response_path.name.replace('.response.json', '.request.json'))
        if not request_path.exists():
            continue
        request, response = read(request_path), read(response_path)
        role = request['role']
        identities.setdefault(role, set()).add(request['model'])
        totals = usage.setdefault(role, {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0})
        totals['calls'] += 1
        for key in ['prompt_tokens', 'completion_tokens']:
            totals[key] += response.get('usage', {}).get(key, 0)
    return {'model_identities': {k: sorted(v) for k, v in identities.items()}, 'llm_usage': usage}


def resolution_details(strategy, selected, takes, independent_pass):
    provider = selected.get('provider_id') if selected else None
    selected_takes = [t for t in takes if t.get('provider') == provider]
    gates = [t['stage'] for t in selected_takes if t.get('stage') in {'draft', 'preview'} and t['review']['pass'] is True]
    route = 'solved_through_flux' if provider == 'flux_dev_local' else 'solved_through_ladder' if strategy == 'ladder' else 'solved_through_current'
    return {'loop_resolution': route if selected else 'unresolved',
            'verified_resolution': route if selected and independent_pass is True else 'unresolved' if independent_pass is False or not selected else 'audit_pending',
            'preview_approval_stage': gates[-1] if gates else None,
            'stage_path': [t.get('stage', 'final') for t in takes],
            'full_resolution_attempts': sum(t.get('stage', 'final') == 'final' for t in takes),
            'provider_path': list(dict.fromkeys(t.get('provider') for t in takes)),
            'escalation_triggers': [t['escalation_trigger'] for t in takes if t.get('escalation_trigger')]}


def run_case(case, config, output, strategy, settings):
    root = Path(output) / strategy / case["case_id"]
    root.mkdir(parents=True, exist_ok=True)
    if (root / "result.json").exists():
        result = read(root / "result.json")
        if result.get("selected") and digest(Path(result["selected"]["file"]).read_bytes()) != result["selected"]["sha256"]:
            raise ValueError("Selected experiment image changed")
        if not result.get('resumable'):
            return result
    store = Store(root)
    models = MeasuredModels(config, store)
    context = case["source_context"]
    write(root / "project.json", {"subject": context["subject"]})
    write(root / "evidence-final.json", {"title": context["evidence_title"], "facts": context["facts"]})
    write(root / "creative-brief.json", context["creative_brief"])
    write(root / "scene-image-prompts.json", case["library"])
    start = time.monotonic()
    error = None
    try:
        with measure_generation(root):
            if strategy == "current":
                manifest = media.generate_images(config, store, case["library"], models)
            else:
                manifest = run_ladder(config, store, case["library"], models, settings)
    except Exception as exc:
        error = str(exc)
        manifest = read(root / "image_manifest.json") if (root / "image_manifest.json").exists() else {}
    finally:
        record_metric(root, "wall", time.monotonic() - start)
    selected = manifest.get(case["asset_id"])
    audit = None
    audit_error = None
    if selected:
        try:
            audit = media.review_scene_image(models, case["library"], case["library"]["assets"][0],
                Path(selected["file"]), selected["settings"], "independent-final", context,
                attempt_history=[], provider_id="withheld")
        except Exception as exc:
            audit_error = str(exc)
    takes = baseline_takes(root) if strategy == "current" else read(root / "ladder-state.json")["takes"] if (root / "ladder-state.json").exists() else []
    result = {"case_id": case["case_id"], "asset_id": case['asset_id'], 'subject': context['subject'],
              "strategy": strategy, "historical_status": case["historical_status"],
              "accepted": bool(selected), "independent_pass": audit["pass"] if audit else None,
              "independent_audit": audit, "audit_error": audit_error, "selected": selected,
              "attempts": len(takes), "takes": takes, "error": error, **case_metrics(root)}
    result["submission_records"] = sum(bool(re.fullmatch(r"provider-\d+\.json", p.name)) for p in root.rglob('provider-*.json'))
    records = [json.loads(line) for line in (root / "metrics.jsonl").read_text().splitlines()]
    result["pipeline_wall_seconds"] = round(sum(r["seconds"] for r in records if r["kind"] == "wall"), 3)
    result["escalated"] = any(t.get("tier", 0) > 0 for t in takes)
    result.update(resolution_details(strategy, selected, takes, result['independent_pass']))
    result.update(model_usage(root))
    # Include backend failures in comparisons rather than dropping hard cases.
    # Retain their submission checkpoints and allow explicit reruns to resume.
    result['resumable'] = bool(error and "bounded provider recovery" not in error)
    write(root / "result.json", result)
    if result['resumable']:
        write(root / "last-error.json", result)
    store.db.close()
    return result


def experiment_plan(corpus, config, settings, selected):
    registry = validate_registry(config["image_provider_registry"])
    return {"protocol": PROTOCOL, "corpus_sha256": digest(corpus), "case_ids": [c["case_id"] for c in selected],
            "code_sha256": {name: digest((ROOT / "infofilm" / name).read_bytes()) for name in
                            ["image_experiment.py", "media.py", "image_providers.py", "image_studio.py", "composition.py", "llm.py"]},
            "settings": settings, "registry": registry,
            "runtime": {k: config[k] for k in ["image_url", "tts_url", "planner_url", "reviewer_url", "image_provider_policy", "max_repairs", "gpu_lock"] if k in config},
            "matching": "Same case context, normal steps, initial seed, review/repair models and maximum generated candidates; draft calls count toward the ladder budget.",
            "routing_difference": "The current arm uses production routing unchanged. The ladder arm can additionally fall back after its tier budget is exhausted with meaningful normal-step repairs; this experimental difference is explicit in settings and per-take escalation_trigger.",
            "ladder_budget": "Preview stages reserve one candidate for a final render; drafts count toward both budgets and do not establish provider weakness. Exhausted preview repair budget can trigger the configured fallback.",
            "measurement": "Isolated assets include model lifecycle and GPU-lock wait in wall time; generation measures adapter submission/wait or ImageStudio request. Independent audit is reported separately."}


def summarize(results):
    usage = {}
    for result in results:
        for role, counters in result.get('llm_usage', {}).items():
            totals = usage.setdefault(role, {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0})
            for key in totals:
                totals[key] += counters.get(key, 0)
    return {"cases": len(results), "accepted": sum(r["accepted"] for r in results),
            'llm_usage': usage,
            'stage_counts': {stage: sum(t.get('stage', 'final') == stage for r in results for t in r['takes']) for stage in ['draft', 'preview', 'final']},
            "independently_passed": sum(r["independent_pass"] is True for r in results),
            "accepted_but_audit_failed": sum(r["accepted"] and r["independent_pass"] is False for r in results),
            "audit_pending_or_error": sum(r["accepted"] and r["independent_pass"] is None for r in results),
            "backend_errors": sum(bool(r["error"]) and "bounded provider recovery" not in r["error"] for r in results),
            "reviewed_takes": sum(r["attempts"] for r in results),
            "submission_records": sum(r.get("submission_records", r["attempts"]) for r in results),
            'verified_resolution': {key: sum(r.get('verified_resolution') == key for r in results) for key in
                ['solved_through_ladder', 'solved_through_current', 'solved_through_flux', 'unresolved', 'audit_pending']},
            'preview_approval_stage': {key: sum(r.get('preview_approval_stage') == key and r['independent_pass'] is True for r in results) for key in ['draft', 'preview']},
            "escalated_cases": sum(r["escalated"] for r in results),
            **{key: round(sum(r[key] for r in results), 3) for key in ["generation_seconds", "review_seconds", "repair_seconds", "independent_audit_seconds", "pipeline_wall_seconds"]}}


def compare(output):
    output = Path(output)
    plans = [read(output / strategy / "plan.json") for strategy in ["current", "ladder"]]
    if plans[0] != plans[1]:
        raise ValueError("Experiments have different cases/settings/providers; cannot compare")
    by_strategy = {}
    for strategy in ["current", "ladder"]:
        by_strategy[strategy] = {cid: read(output / strategy / cid / "result.json") for cid in plans[0]["case_ids"] if (output / strategy / cid / "result.json").exists()}
    paired = sorted(set(by_strategy["current"]) & set(by_strategy["ladder"]))
    rows = [{"case_id": cid, "current": by_strategy["current"][cid], "ladder": by_strategy["ladder"][cid]} for cid in paired]
    report = {"protocol": PROTOCOL, "paired_cases": len(paired), "planned_cases": len(plans[0]["case_ids"]),
              "complete": len(paired) == len(plans[0]["case_ids"]),
              "summary": {s: summarize([by_strategy[s][cid] for cid in paired]) for s in by_strategy}, "pairs": rows,
              "human_review": "pending; automated reviews are efficacy signals, not ground truth"}
    report['resumable_backend_cases'] = {strategy: [cid for cid in paired if by_strategy[strategy][cid].get('resumable')] for strategy in by_strategy}
    report['model_identity_mismatches'] = []
    for row in rows:
        left, right = row['current'].get('model_identities', {}), row['ladder'].get('model_identities', {})
        if any(left[role] != right[role] for role in set(left) & set(right)):
            report['model_identity_mismatches'].append(row['case_id'])
    if paired:
        report["median_paired_pipeline_seconds_saved"] = round(statistics.median(r["current"]["pipeline_wall_seconds"] - r["ladder"]["pipeline_wall_seconds"] for r in rows), 3)
    write(output / "comparison.json", report)
    body = []
    for row in rows:
        cells = []
        for strategy in ["current", "ladder"]:
            r = row[strategy]
            image = Path(r["selected"]["file"]).relative_to(output).as_posix() if r["selected"] else None
            cells.append(f'<td>{strategy}: {html.escape(r["verified_resolution"])}; preview={html.escape(str(r["preview_approval_stage"]))}; audit={r["independent_pass"]}; takes={r["attempts"]}; wall={r["pipeline_wall_seconds"]}s' +
                         (f'<br><img src="{html.escape(image)}" width="480">' if image else '') + '</td>')
        body.append('<tr><th>' + html.escape(row['case_id']) + '</th>' + ''.join(cells) + '</tr>')
    (output / "comparison.html").write_text('<!doctype html><meta charset="utf-8"><title>Image ladder comparison</title><h1>Matched image experiments</h1><p>Human review pending. Partial pairs are not a completed experiment.</p><pre>' + html.escape(json.dumps(report['summary'], indent=2)) + '</pre><table>' + ''.join(body) + '</table>')
    return report


def main(strategy=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--search-root", type=Path, default=ROOT / "artifacts/runs")
    parser.add_argument("--corpus", type=Path, default=ROOT / "pilots/image-ladder/cases.json")
    parser.add_argument("--settings", type=Path, default=ROOT / "pilots/image-ladder/settings.json")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/image-ladder-experiment")
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--execute", action="store_true", help="Actually call models/providers; omit to prepare only")
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()
    if args.collect:
        corpus = collect_cases(args.search_root, args.count)
        write(args.corpus, corpus)
        rows = ['# Image experiment corpus', '', corpus['selection'], '', '| Case | Source project | Asset | Historical outcome | Reviewed takes |', '| --- | --- | --- | --- | --- |']
        rows += [f"| {c['case_id']} | {Path(c['source_project']).name} | {c['asset_id']} | {c['historical_status']} | {c['historical_attempts']} |" for c in corpus['cases']]
        args.corpus.with_suffix('.md').write_text('\n'.join(rows) + '\n')
        print(json.dumps({"corpus": str(args.corpus), "cases": corpus['case_count'], "historical_status": {s: sum(c['historical_status'] == s for c in corpus['cases']) for s in ['recovered', 'unresolved']}}))
        return
    if args.compare:
        print(json.dumps(compare(args.output)["summary"], indent=2))
        return
    if strategy not in {"current", "ladder"}:
        parser.error("Use the current or ladder experiment entry script")
    corpus, settings, config = read(args.corpus), read(args.settings), configuration(args.config)
    cases = validate_corpus(corpus)
    if args.cases:
        if set(args.cases) - {c['case_id'] for c in cases}:
            parser.error("Unknown case ID")
        cases = [c for c in cases if c['case_id'] in args.cases]
    for size in [settings['preview_size'], settings['full_size']]:
        if len(size) != 2 or any(type(n) is not int or n < 512 or n > 2816 or n % 16 for n in size):
            parser.error("Sizes must be 512–2816 pixels and divisible by 16")
    if settings['preview_size'][0] * settings['full_size'][1] != settings['preview_size'][1] * settings['full_size'][0]:
        parser.error("Preview and final aspect ratios must match")
    if settings['preview_size'][0] >= settings['full_size'][0]:
        parser.error("Preview must be smaller than the final frame")
    registry = config['image_provider_registry']
    if type(settings.get('exhaustion_fallback', False)) is not bool:
        parser.error("exhaustion_fallback must be boolean")
    if not isinstance(settings.get('low_steps'), dict):
        parser.error("low_steps must be a per-provider object")
    for name, provider in registry['providers'].items():
        low_steps = settings['low_steps'].get(name, 8)
        if type(low_steps) is not int or not 1 <= low_steps <= provider['settings']['steps']:
            parser.error("Low-step settings must be positive integers at most the provider's normal steps")
    config.update(image_width=settings['full_size'][0], image_height=settings['full_size'][1])
    config.pop('image_engine', None)
    plan = experiment_plan(corpus, config, settings, cases)
    output = args.output.resolve()
    for case in cases:
        source = Path(case['source_project']).resolve()
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            parser.error("Experiment output must be separate from production projects")
    plan_path = output / strategy / 'plan.json'
    if plan_path.exists() and read(plan_path) != plan:
        if any(p.name not in {'plan.json', 'corpus.json'} for p in plan_path.parent.iterdir()):
            parser.error("Output has different frozen experiment settings; choose a new output")
        print('Refreshing an unexecuted preparation plan; no case artifacts exist.', flush=True)
    write(plan_path, plan)
    write(output / strategy / 'corpus.json', corpus)
    print(json.dumps({'strategy': strategy, 'cases': len(cases), 'execute': args.execute, 'plan': str(plan_path)}), flush=True)
    if not args.execute:
        return
    results = []
    for index, case in enumerate(cases, 1):
        print(f"[{strategy}] {index}/{len(cases)} {case['case_id']} {case['source_context']['subject']} {case['asset_id']}", flush=True)
        results.append(run_case(case, config, output, strategy, settings))
        print('[case-result] ' + json.dumps({key: results[-1][key] for key in
            ['case_id', 'verified_resolution', 'preview_approval_stage', 'attempts', 'generation_seconds', 'review_seconds', 'repair_seconds', 'pipeline_wall_seconds', 'error']}), flush=True)
        write(output / strategy / 'summary.json', summarize(results))
    print(json.dumps(summarize(results), indent=2))


if __name__ == '__main__':
    main()
