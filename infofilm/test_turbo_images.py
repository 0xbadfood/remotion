"""Replay an existing film's initial image prompts through the production repair loop."""
from __future__ import annotations

import argparse
import fcntl
import html
import re
import signal
import shutil
import statistics
import time
from pathlib import Path

import httpx

from .core import ROOT, Store, configuration, digest, read, write
from .image_studio import ENGINES
from .llm import Models
from .media import generate_images, image_identity, image_source_context

INPUTS = ("scene-image-prompts.json", "image_manifest.json", "project.json",
          "evidence-final.json", "creative-brief.json")
POLICY = "initial-prompts-production-repair-v1"
TEXT_AUDIT_POLICY = "overlaid-typography-check-v1"


def audit_selected_text(root, config):
    """A separate evaluation pass; never alter the tested repair-loop decisions."""
    manifest_path = Path(root) / "image_manifest.json"
    if not manifest_path.exists():
        return
    manifest = read(manifest_path)
    audits = {}
    with_store = Store(Path(root) / "independent-audit")
    models = Models(config, with_store)
    try:
        for aid, selected in manifest.items():
            if digest(Path(selected["file"]).read_bytes()) != selected["sha256"]:
                raise ValueError("Selected benchmark image checksum changed")
            try:
                value = models.call("reviewer", "typography-" + aid,
                    "Inspect the actual image ONLY for visible overlaid typography. "
                    "This illustration will have its title and captions added later in Remotion. "
                    "Any already-drawn title, heading, explanatory label, caption or watermark is a failure, "
                    "even if the text is readable, correctly spelled, factually correct and relevant to the subject. "
                    "Do not forgive a generated title because its spelling is good. "
                    "Distinguish overlaid typography from natural surface textures, ornamentation or tiny incidental markings on objects. "
                    "Do not hallucinate words; quote only text clearly visible in this image, or describe an illegible text region. "
                    "Return {has_overlaid_typography:boolean, visible_text:[string], locations:[string], explanation:string}. "
                    "This is an independent final audit, not approval of historical accuracy or overall image quality.",
                    image_path=selected["file"], max_tokens=1500)
                if type(value.get("has_overlaid_typography")) is not bool:
                    raise ValueError("Text audit did not return a boolean")
                audits[aid] = {"policy": TEXT_AUDIT_POLICY, "image_sha256": selected["sha256"],
                               "pass": not value["has_overlaid_typography"], **value}
            except Exception as exc:
                audits[aid] = {"policy": TEXT_AUDIT_POLICY, "image_sha256": selected["sha256"],
                               "pass": None, "error": str(exc)}
            write(Path(root) / "independent-text-audit.json", audits)
    finally:
        with_store.db.close()


def prepare(project, config, engine_specs, assets, max_attempts, output=None):
    project = Path(project).resolve()
    library = read(project / "scene-image-prompts.json")
    if assets:
        requested = set(assets)
        unknown = requested - {a["id"] for a in library["assets"]}
        if unknown:
            raise ValueError(f"Unknown asset IDs: {sorted(unknown)}")
        library = {**library, "assets": [a for a in library["assets"] if a["id"] in requested]}
    if not library["assets"]:
        raise ValueError("No assets selected")
    if len({a["id"] for a in library["assets"]}) != len(library["assets"]):
        raise ValueError("Asset IDs must be unique")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", a["id"]) for a in library["assets"]):
        raise ValueError("Asset IDs must be safe filenames")
    protected = {name: digest((project / name).read_bytes()) for name in INPUTS if (project / name).exists()}
    original = read(project / "image_manifest.json")
    image_hashes = {key: digest(Path(original[key]["file"]).read_bytes()) for key in original}
    if any(image_hashes[key] != original[key]["sha256"] for key in original):
        raise ValueError("A production image no longer matches its manifest")
    plan = {"policy": POLICY, "project": str(project), "input_sha256": protected,
            "production_image_sha256": image_hashes, "library": library,
            "source_context": image_source_context(project, library), "engines": engine_specs,
            "max_attempts": max_attempts, "image_url": config["image_url"],
            "delivery_dimensions": [config["image_width"], config["image_height"]],
            "planner_url": config["planner_url"], "reviewer_url": config["reviewer_url"],
            "max_repairs": config.get("max_repairs", 2)}
    identity = digest(plan)
    root = Path(output).resolve() if output else ROOT / "artifacts/image-benchmarks" / project.name / identity[:16]
    if root == project or root.is_relative_to(project):
        raise ValueError("Benchmark output must be outside the production project")
    root.mkdir(parents=True, exist_ok=True)
    if (root / "plan.json").exists() and read(root / "plan.json") != plan:
        raise ValueError("Output already belongs to a different benchmark; choose a fresh directory")
    write(root / "plan.json", plan)
    return root, plan, original


def summary(cases):
    attempts = [take for case in cases for take in case["takes"]]
    accepted = [case for case in cases if case["accepted"]]
    times = [take["generation_seconds"] for take in attempts if take.get("generation_seconds") is not None]
    warm = [take["generation_seconds"] for take in attempts[1:] if take.get("generation_seconds") is not None]
    return {"planned_images": len(cases), "accepted_images": len(accepted),
            "attempted_images": sum(bool(c["takes"]) for c in cases),
            "pending_images": [c["asset"] for c in cases if not c["takes"] or not c["takes"][-1].get("review")],
            "exhausted_images": [c["asset"] for c in cases if c.get("status") == "exhausted"],
            "first_pass_images": sum(bool(c["takes"] and c["takes"][0].get("review", {}).get("pass") is True) for c in cases),
            "accepted_within_two_takes": sum(c["accepted"] and c["accepted_take"] <= 2 for c in cases),
            "unresolved_images": [c["asset"] for c in cases if not c["accepted"]],
            "generation_takes": len(times), "generation_seconds": round(sum(times), 3),
            "first_request_seconds_including_load": times[0] if times else None,
            "warm_median_request_seconds": round(statistics.median(warm), 3) if warm else None,
            "takes_per_accepted_image_including_failures": round(len(times)/len(accepted), 3) if accepted else None,
            "generation_seconds_per_accepted_image_including_failures": round(sum(times)/len(accepted), 3) if accepted else None}


def report(root, plan, original):
    root = Path(root).resolve()
    engines = {}
    for engine, spec in plan["engines"].items():
        directory = root / engine
        config_path = directory / "benchmark-config.json"
        if not config_path.exists():
            continue
        config = read(config_path)
        text_audits = read(directory / "independent-text-audit.json") if (directory / "independent-text-audit.json").exists() else {}
        cases = []
        for asset in plan["library"]["assets"]:
            image_root = directory / "images" / image_identity(config, plan["library"], asset)[:20]
            takes = []
            for index in range(plan["max_attempts"]):
                provider = image_root / f"provider-{index}.json"
                if not provider.exists():
                    continue
                result = read(image_root / f"provider-result-{index}.json") if (image_root / f"provider-result-{index}.json").exists() else {}
                take = {"attempt": index + 1, "prompt": read(provider)["params"]["prompt"],
                        "settings": read(provider), "generation_seconds": result.get("generation_seconds"),
                        "error": result.get("error"), "file": str(image_root / f"candidate-{index}.png"),
                        "sha256": result.get("sha256"),
                        "review": read(image_root / f"review-{index}.json") if (image_root / f"review-{index}.json").exists() else {}}
                takes.append(take)
            selected = read(image_root / "selected.json") if (image_root / "selected.json").exists() else None
            accepted_take = next((t["attempt"] for t in takes if selected and t["file"] == selected["file"]), None)
            text_audit = text_audits.get(asset["id"])
            if text_audit and selected and text_audit.get("image_sha256") != selected.get("sha256"):
                text_audit = None
            status = ("accepted" if selected else "exhausted" if len(takes) == plan["max_attempts"] and takes[-1]["review"].get("pass") is False
                      else "pending" if not takes else "repairing")
            cases.append({"asset": asset["id"], "purpose": asset["purpose"], "initial_prompt": asset["prompt"], "status": status,
                          "accepted": selected is not None, "accepted_take": accepted_take, "takes": takes,
                          "independent_text_audit": text_audit})
        latency = {"planner": [], "reviewer": []}
        usage = {"planner": {"prompt_tokens": 0, "completion_tokens": 0},
                 "reviewer": {"prompt_tokens": 0, "completion_tokens": 0}}
        for response_path in (directory / "requests").glob("*.response.json"):
            request_path = response_path.with_name(response_path.name.replace(".response.json", ".request.json"))
            role = read(request_path)["role"]
            response = read(response_path)
            latency[role].append(response.get("measured_latency_seconds", 0))
            for key in usage[role]:
                usage[role][key] += response.get("usage", {}).get(key, 0)
        engines[engine] = {"settings": spec, "summary": summary(cases), "cases": cases,
                           "llm_calls": {k: len(v) for k, v in latency.items()},
                           "llm_seconds": {k: round(sum(v), 3) for k, v in latency.items()}, "llm_tokens": usage,
                           "run": read(directory / "run.json") if (directory / "run.json").exists() else {}}
        engines[engine]["summary"].update(
            accepted_images_passing_independent_text_audit=sum(c["accepted"] and c["independent_text_audit"] is not None and c["independent_text_audit"].get("pass") is True for c in cases),
            reviewer_false_positives_for_typography=[c["asset"] for c in cases if c["accepted"] and c["independent_text_audit"] is not None and c["independent_text_audit"].get("pass") is False],
            accepted_images_awaiting_text_audit=[c["asset"] for c in cases if c["accepted"] and (c["independent_text_audit"] is None or c["independent_text_audit"].get("pass") is None)])
    project = Path(plan["project"])
    preserved = all(digest((project / name).read_bytes()) == sha for name, sha in plan["input_sha256"].items())
    preserved = preserved and all(digest(Path(original[key]["file"]).read_bytes()) == sha
                                  for key, sha in plan["production_image_sha256"].items())
    historical = []
    for asset in plan["library"]["assets"]:
        baseline = original.get(asset["id"])
        if not baseline:
            continue
        accepted_file = Path(baseline["file"])
        result_file = accepted_file.with_name(accepted_file.stem.replace("candidate-", "provider-result-") + ".json")
        result = read(result_file) if result_file.exists() else {}
        seconds = result.get("finished_at", 0) - result.get("started_at", 0)
        if seconds > 0:
            historical.append(seconds)
    value = {"policy": POLICY, "subject": plan["source_context"]["subject"], "project": plan["project"],
             "engines": engines, "production_preserved": preserved, "human_review": "pending",
             "historical_qwen_accepted_take_median_seconds": round(statistics.median(historical), 3) if historical else None,
             "limitations": ["One historical subject; automated reviews need human confirmation.",
                             "Model-native steps/CFG; final dimensions match, native turbo canvas is padded then cropped.",
                             "Request timings include service work and first-request model loading, not isolated GPU compute.",
                             "Qwen timing is historical, using selected/repaired prompts; this is not a fresh controlled Qwen run.",
                             "Initial planner prompts are replayed; accepted production prompts are comparison references only."]}
    write(root / "report.json", value)
    if not preserved:
        raise RuntimeError("A protected production input or image changed")
    return value


def gallery(root, plan, original, result):
    root = Path(root).resolve()
    destination = ROOT / "output" / Path(plan["project"]).name / "turbo-benchmark" / root.name
    destination.mkdir(parents=True, exist_ok=True)
    def picture(path, name):
        path = Path(path)
        if not path.exists():
            return "<p>Image not available yet</p>"
        exported = destination / name
        source_stat = path.stat()
        if (not exported.exists() or exported.stat().st_size != source_stat.st_size
                or exported.stat().st_mtime_ns != source_stat.st_mtime_ns):
            shutil.copy2(path, exported)
        return f'<a href="{name}"><img loading="lazy" src="{name}"></a>'
    body = ['<h1>Turbo image engine test</h1>', f'<p>{html.escape(result["subject"])} · Original prompts + production repair loop · up to {plan["max_attempts"]} takes</p>',
            '<p>Click images for full resolution. Production Qwen images are historical references. Review labels are automated; human review is pending.</p>',
            '<p><a href="report.json">Detailed report and prompts</a></p><table><tr><th>Engine</th><th>First pass</th><th>Within two</th><th>Loop passes</th><th>Text-check passes</th><th>Total takes</th><th>Warm median/request</th></tr>']
    for engine, record in result["engines"].items():
        s = record["summary"]
        body.append(f'<tr><td>{engine}</td><td>{s["first_pass_images"]}/{s["planned_images"]}</td><td>{s["accepted_within_two_takes"]}/{s["planned_images"]}</td><td>{s["accepted_images"]}/{s["planned_images"]}</td><td>{s["accepted_images_passing_independent_text_audit"]}/{s["accepted_images"]} (pending: {len(s["accepted_images_awaiting_text_audit"])})</td><td>{s["generation_takes"]}</td><td>{s["warm_median_request_seconds"]} s</td></tr>')
    body.append('</table>')
    for asset in plan["library"]["assets"]:
        aid = asset["id"]
        body.append(f'<section><h2>{aid} · {html.escape(asset["purpose"])}</h2><details><summary>Initial prompt</summary><pre>{html.escape(asset["prompt"])}</pre></details><div class="grid">')
        if aid in original:
            body.append('<article><h3>Qwen production reference</h3>' + picture(original[aid]["file"], f'{aid}-qwen.png') + '</article>')
        for engine, record in result["engines"].items():
            case = next(c for c in record["cases"] if c["asset"] == aid)
            take = next((t for t in case["takes"] if t["attempt"] == case["accepted_take"]), case["takes"][-1] if case["takes"] else None)
            body.append(f'<article><h3>{engine} · {"REVIEWER PASS" if case["accepted"] else "UNRESOLVED"}</h3>')
            if take:
                body.append(picture(take["file"], f'{aid}-{engine}-take-{take["attempt"]}.png'))
            if case["independent_text_audit"]:
                audit = case["independent_text_audit"]
                body.append('<p><strong>Independent text audit: ' + ("PASS" if audit.get("pass") is True else "FAIL" if audit.get("pass") is False else "UNRESOLVED") + '</strong></p><p>' + html.escape(audit.get("explanation", audit.get("error", ""))) + '</p>')
            for item in case["takes"]:
                review = item["review"]
                body.append(f'<details><summary>Take {item["attempt"]} · {"PASS" if review.get("pass") is True else "FAIL / PENDING"} · {item["generation_seconds"]} s</summary>')
                body.append(picture(item["file"], f'{aid}-{engine}-take-{item["attempt"]}.png'))
                body.append('<pre>' + html.escape(item["prompt"]) + '</pre><p>' + html.escape(str(review)) + '</p></details>')
            body.append('</article>')
        body.append('</div></section>')
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Turbo image benchmark</title><style>body{font:16px system-ui;background:#121821;color:#e6eaf0;margin:2rem}a{color:#8fcfff}table{border-collapse:collapse}td,th{padding:.6rem;border:1px solid #536070}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1rem}img{width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}article{background:#202b38;padding:1rem}section{margin:2rem 0}details{margin:.8rem 0}@media(max-width:850px){.grid{grid-template-columns:1fr}}</style>' + ''.join(body) + '</html>'
    (destination / "index.html").write_text(page)
    write(destination / "report.json", result)
    (root / "gallery-path.txt").write_text(str(destination / "index.html") + "\n")
    return destination / "index.html"


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt("Benchmark interrupted; restoring parked providers")
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--engines", nargs="+", choices=sorted(ENGINES), default=["zimage", "ernie-turbo"])
    parser.add_argument("--assets", nargs="+", help="Optional asset IDs, e.g. A006 A007 A015")
    parser.add_argument("--max-attempts", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--studio-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--skip-text-audit", action="store_true", help="Skip the separate final typography audit")
    parser.add_argument("--skip-engines", nargs="+", choices=sorted(ENGINES), default=[],
                        help="Keep these engines' saved results in the comparison but skip further generation")
    parser.add_argument("--park-idle-providers", action="store_true",
                        help="Temporarily stop idle local VibeVoice/Qwen services to release their CUDA contexts")
    args = parser.parse_args()
    config = configuration(args.config)
    config["qwen_url"] = config["image_url"]
    config["image_url"] = args.studio_url.rstrip("/")
    with httpx.Client(timeout=30) as client:
        response = client.get(config["image_url"] + "/api/models")
        response.raise_for_status()
        available = {entry["id"]: entry for entry in response.json()["engines"]}
    specs = {engine: {key: available[engine][key] for key in
             ("defaultSteps", "defaultCfgScale", "dimensionMultiple", "maxWidth", "maxHeight")}
             for engine in dict.fromkeys(args.engines)}
    for engine, spec in specs.items():
        for dimension, limit in ((config["image_width"], spec["maxWidth"]), (config["image_height"], spec["maxHeight"])):
            if ((dimension + spec["dimensionMultiple"] - 1)//spec["dimensionMultiple"])*spec["dimensionMultiple"] > limit:
                parser.error(f"{engine} cannot render the requested padded dimensions")
    root, plan, original = prepare(args.project, config, specs, args.assets, args.max_attempts, args.output)
    print(f"Benchmark: {root}", flush=True)
    with (root / "benchmark.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for engine, spec in specs.items():
            if engine in args.skip_engines:
                write(root / engine / "run.json", {"status": "skipped", "reason": "Skipped by user; saved results retained"})
                print(f"Skipping {engine}; retaining saved benchmark images and reviews", flush=True)
                continue
            current = {**config, "image_engine": engine, "image_steps": spec["defaultSteps"],
                       "image_cfg_scale": spec["defaultCfgScale"], "image_dimension_multiple": spec["dimensionMultiple"],
                       "image_max_attempts": args.max_attempts, "image_continue_on_exhaustion": True,
                       "image_park_idle_services": args.park_idle_providers}
            directory = root / engine
            write(directory / "benchmark-config.json", current)
            # These copies provide the same subject, evidence and creative context.
            for name in INPUTS:
                if name != "image_manifest.json" and (args.project / name).exists():
                    shutil.copy2(args.project / name, directory / name)
            if args.report_only:
                continue
            store = Store(directory)
            started = time.monotonic()
            try:
                store.event("benchmark", "running", {"engine": engine, "planned_images": len(plan["library"]["assets"]), "max_attempts": args.max_attempts})
                generate_images(current, store, plan["library"], Models(current, store),
                                on_progress=lambda: gallery(root, plan, original, report(root, plan, original)))
                write(directory / "run.json", {"status": "finished", "last_execution_seconds": round(time.monotonic()-started, 3)})
            except Exception as exc:
                store.event("benchmark", "error", {"engine": engine, "error": str(exc)})
                write(directory / "run.json", {"status": "error", "error": str(exc)})
            finally:
                store.db.close()
                value = report(root, plan, original)
                print(f"Gallery: {gallery(root, plan, original, value)}", flush=True)
        if not args.report_only and not args.skip_text_audit:
            for engine in specs:
                audit_selected_text(root / engine, read(root / engine / "benchmark-config.json"))
        value = report(root, plan, original)
        print(f"Gallery: {gallery(root, plan, original, value)}", flush=True)
        for engine, record in value["engines"].items():
            print(engine, record["summary"], flush=True)
        if any(record["summary"]["unresolved_images"] or record["summary"]["reviewer_false_positives_for_typography"]
               or (not args.skip_text_audit and not args.report_only and record["summary"]["accepted_images_awaiting_text_audit"])
               for record in value["engines"].values()):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
