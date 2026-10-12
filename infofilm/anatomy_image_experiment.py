"""Matched first-pass anatomy variations on Qwen and premium Flux.

The source diagram is an input and review reference, not a claim that generated
cross-sections are source-verified. No repair, model escalation or film creation.
"""
from __future__ import annotations

import argparse
import html
import json
import time
from pathlib import Path

import httpx
from PIL import Image

from .core import ROOT, Store, configuration, digest, read, write
from .image_providers import provider_config
from .image_recovery import provider_session, render_take
from .llm import Models, ModelOutputError
from .person_references import validate_reference

PROTOCOL = "infofilm.anatomy-image-experiment.v1"
SOURCE_URL = "https://www.niddk.nih.gov/news/media-library/18162"
IMAGE_URL = "https://www.niddk.nih.gov/media-assets/18162/N01516-H.jpg"
RIGHTS_URL = "https://www.niddk.nih.gov/copyright"
DEFAULT_ROOT = ROOT / "artifacts/experiments/anatomy-digestive-v1"
CASE_FILE = ROOT / "experiments/anatomy-images/cases.json"
CRITERIA = ["organ_placement", "connections", "message_clarity", "visual_quality", "reference_use"]


def prepare_reference(root):
    directory = root / "reference"
    manifest = directory / "source.json"
    if manifest.exists():
        result = read(manifest)
        validate_reference(result)
        if digest((directory / "source.jpg").read_bytes()) != result["source_sha256"]:
            raise ValueError("Archived source image checksum changed")
        return result
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        page = client.get(SOURCE_URL); page.raise_for_status()
        if "/media-assets/18162/N01516-H.jpg" not in page.text:
            raise ValueError("The source page no longer binds the expected image")
        response = client.get(IMAGE_URL); response.raise_for_status()
        if len(response.content) > 12_000_000:
            raise ValueError("Reference download exceeded size budget")
        rights = client.get(RIGHTS_URL); rights.raise_for_status()
    (directory / "source-page.html").write_text(page.text)
    (directory / "copyright.html").write_text(rights.text)
    raw = directory / "source.jpg"
    raw.write_bytes(response.content)
    destination = directory / "reference.png"
    with Image.open(raw) as image:
        if image.width * image.height > 20_000_000:
            raise ValueError("Reference exceeded pixel budget")
        image.convert("RGB").save(destination)
    result = {"title": "Illustration of the digestive system labeled", "source_url": SOURCE_URL,
              "image_url": IMAGE_URL, "rights_url": RIGHTS_URL,
              "credit": "National Institute of Diabetes and Digestive and Kidney Diseases, National Institutes of Health",
              "downloaded_at": time.time(), "source_sha256": digest(response.content),
              "file": str(destination.resolve()), "sha256": digest(destination.read_bytes()),
              "scope": "Labeled whole-organ overview; internal wall layers and movement are not depicted."}
    write(manifest, result)
    return result


def scene_prompt(case, conditioning):
    prompt = ("Setting: Human digestive anatomy, anterior view where applicable.\n"
              "Visual treatment: Accurate educational medical illustration.\n"
              "Composition: One coherent landscape teaching image with clear organ relationships.\n"
              "Scene: " + case["scene"])
    if conditioning == "reference":
        if case.get('reference_instruction'):
            return prompt + '\nAnatomy reference: ' + case['reference_instruction']
        prompt += ("\nAnatomy reference: the attached NIH diagram supplies organ arrangement and identification. "
                   "Use the relevant visible structures as anatomical guidance and create the requested new view. "
                   "Its text and leader lines remain in the reference; the finished scene contains anatomy and plain background. "
                   "For a cutaway, depict the requested internal structure rather than copying the full torso diagram.")
    return prompt


def review_errors(value, case):
    if not isinstance(value, dict):
        return ["Expected a review object"]
    errors = []
    if value.get("case_id") != case["id"]:
        errors.append("Echo the case_id")
    if value.get("verdict") not in {"pass", "fail", "uncertain"}:
        errors.append("verdict must be pass, fail or uncertain")
    scores = value.get("scores", {})
    if not isinstance(scores, dict) or set(scores) != set(CRITERIA):
        errors.append("Return exactly the specified score keys")
    elif any(type(v) not in {int, float} or not 0 <= v <= 100 for v in scores.values()):
        errors.append("Scores must be finite numbers in 0..100")
    for key in ["issues", "limitations", "reference_supported_observations", "inferred_details"]:
        if not isinstance(value.get(key), list) or any(not isinstance(v, str) for v in value[key]):
            errors.append(key + " must contain strings")
    if value.get("verdict") == "pass" and value.get("issues"):
        errors.append("Blocking issues cannot accompany a pass")
    if value.get("verdict") == "fail" and not value.get("issues"):
        errors.append("A failure requires concrete visible issues")
    if case["id"] in {"D04", "D05", "D06"} and not value.get("inferred_details"):
        errors.append("Record the cutaway/internal details absent from this source diagram")
    checks = value.get("checks", [])
    if (not isinstance(checks, list) or len(checks) != len(case["checks"]) or
        any(not isinstance(c, dict) or c.get("criterion") != expected or
            c.get("status") not in {"met", "not_met", "uncertain"} or
            not isinstance(c.get("evidence"), str) or not c["evidence"].strip()
            for c, expected in zip(checks, case["checks"]))):
        errors.append("Assess each supplied check in order with status and visible evidence")
    elif value.get("verdict") == "pass" and any(c["status"] == "not_met" for c in checks):
        errors.append("A failed essential check cannot accompany a pass")
    return errors


def review_candidate(models, root, case, candidate, prompt, reference):
    destination = root / "review.json"
    packet = {"case": case, "current_prompt": prompt,
              "source": {k: reference[k] for k in ["title", "source_url", "scope"]}}
    signature = digest({"packet": packet, "candidate_sha256": digest(candidate.read_bytes()),
                        "reference_sha256": reference["sha256"],
                        "reviewer_url": models.config["reviewer_url"]})
    if destination.exists():
        record = read(destination)
        if record.get("input_sha256") != signature:
            raise ValueError("Saved anatomy review inputs changed")
        errors = review_errors(record["review"], case)
        if errors:
            raise ValueError("Saved review fails validation: " + "; ".join(errors))
        return record
    task = ("Screen an experimental anatomical teaching image. Image 1 is the candidate; image 2 is the NIH source diagram. "
            "You are not an expert certification service. Judge visible anatomy, tract connections, stated message and usability. "
            "An anterior view reverses patient and viewer left/right. Allow artistic style, transparency and deliberate cutaways. "
            "A close-up need not include unrelated organs. Distinguish structures visible in the source from inferred internal detail. "
            "Do not invent evidence or claim a whole-organ diagram verifies microscopic layers, ducts or motion. "
            "Wrong connections, swapped organs, grossly malformed structures or misleading biology are blocking defects. "
            "Generated labels/leader lines conflict with the prompt's plain surrounding space; identify them when present. "
            "Use uncertain when essential anatomy cannot be assessed. A pass means plausible enough for experimental screening, "
            "not source-verified or medically certified. Score each criterion 0..100; scores never override a visible blocking defect. "
            "Do not recommend a provider or receive previous reviews. "
            "Return {case_id,verdict:'pass'|'fail'|'uncertain',scores:{organ_placement,connections,message_clarity,visual_quality,reference_use},"
            "checks:[{criterion:exact supplied check,status:'met'|'not_met'|'uncertain',evidence:string}],"
            "issues:[string],limitations:[string],reference_supported_observations:[string],inferred_details:[string]}. "
            "Review all supplied checks in order; keep evidence concise.\nINPUT:\n" + json.dumps(packet))
    start = time.monotonic()
    feedback = ""
    for index in range(2):
        try:
            value = models.call("reviewer", "anatomy-review-" + root.parent.name + "-" + case["id"] + f"-{index}",
                                task + feedback, image_path=candidate,
                                reference_image_paths=[validate_reference(reference)], max_tokens=3200)
            errors = review_errors(value, case)
        except ModelOutputError as exc:
            errors = [str(exc)]
        if not errors:
            record = {"input_sha256": signature, "seconds": round(time.monotonic() - start, 3), "review": value}
            write(destination, record)
            return record
        feedback = "\nCorrect the JSON/schema for the same images: " + json.dumps(errors)
    raise ValueError("Anatomy screening schema failed: " + "; ".join(errors))


def write_report(store, definition):
    root = store.root
    rows = []
    for case in definition["cases"]:
        for provider in definition["providers"]:
            path = root / "results" / provider / case["id"] / "result.json"
            row = read(path) if path.exists() else {"case_id": case["id"], "provider": provider, "status": "pending"}
            # A failed or unfinished review must not hide the generated image.
            candidate = path.parent / "candidate-0.png"
            if candidate.is_file() and not row.get("file"):
                row = {**row, "file": str(candidate), "sha256": digest(candidate.read_bytes())}
            rows.append(row)
    summary = {"schema_version": PROTOCOL, "conditioning": definition["conditioning"],
               "planned": len(rows), "completed": sum(r["status"] == "complete" for r in rows),
               "errors": sum(r["status"] == "error" for r in rows),
               "screening_only": True, "results": rows}
    write(root / "summary.json", summary)
    escape = html.escape
    def relative(path):
        return escape(Path(path).resolve().relative_to(root).as_posix(), quote=True)
    tiles = []
    for case in definition["cases"]:
        items = []
        for row in [r for r in rows if r["case_id"] == case["id"]]:
            review = row.get("review", {})
            image = '<img src="' + relative(row["file"]) + '" loading="lazy">' if row.get("file") else ""
            score = review.get("scores", {})
            details = {"screening": review, "error": row.get("error"), "prompt": row.get("prompt")}
            if "baseline_review" in row:
                details["previous_review"] = row["baseline_review"]
            items.append('<article><h3>' + escape(row["provider"]) + ' — ' + escape(review.get("verdict", row["status"])) +
                         '</h3>' + image + '<p>Generation: ' + str(row.get("generation_seconds", "—")) +
                         's · review: ' + str(row.get("review_seconds", "—")) + 's</p><p>' +
                         escape(json.dumps(score)) + '</p><details><summary>Prompt, checks and observations</summary><pre>' +
                         escape(json.dumps(details, indent=2)) + '</pre></details></article>')
        reference_panel = ''
        ref = definition.get('case_references', {}).get(case['id'])
        if ref:
            reference_panel = ('<details><summary>Scene reference and attribution</summary><p><a href="' +
                               escape(ref['source_url'], quote=True) + '">Source</a> · ' + escape(ref['credit']) +
                               '</p><p>' + escape(ref.get('usage', '')) + '</p><img class="reference" src="' +
                               relative(ref['file']) + '"></details>')
        tiles.append('<section><h2>' + escape(case["id"] + ' ' + case["title"]) + '</h2><p>' +
                     escape(case["visual_message"]) + '</p><p>Reference support: ' + escape(case["reference_support"]) +
                     '</p>' + reference_panel + '<div class="grid">' + ''.join(items) + '</div></section>')
    document = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>Digestive anatomy image experiment</title><style>body{font:16px system-ui;background:#f4f6f8;color:#17212b;'
                'margin:24px;max-width:1600px}h1,h2{line-height:1.2}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));'
                'gap:20px}article,section{background:white;padding:18px;margin:18px 0;border-radius:10px}img{max-width:100%;height:auto}'
                'pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}.reference{max-height:500px}</style>'
                '<h1>Digestive anatomy: matched first-pass experiment</h1><p>' +
                f'{summary["completed"]}/{summary["planned"]} completed; {summary["errors"]} errors. Conditioning: ' +
                escape(definition["conditioning"]) + '. Identical scene prompts and seeds; provider-native steps. No image repair loop.</p>'
                '<p>Reviewer: ' + escape(definition.get('reviewer_url','unspecified')) +
                (' · previous reviewer: ' + escape(definition['baseline_reviewer_url']) if definition.get('baseline_reviewer_url') else '') + '</p>'
                '<p>Automated screening only. Internal details absent from the reference are inferred. '
                'These scores do not certify scientific accuracy or establish that reference conditioning caused an improvement.</p>'
                '<p><a href="' + escape(SOURCE_URL) + '">NIH source diagram</a> · '
                '<a href="' + escape(RIGHTS_URL) + '">Source attribution policy</a> · <a href="summary.json">Raw results</a></p>'
                '<p>' + escape(definition["reference"]["credit"]) + '</p><img class="reference" src="' +
                relative(definition["reference"]["file"]) + '">' + ''.join(tiles) + '</html>')
    temporary = root / "report.html.tmp"
    temporary.write_text(document)
    temporary.replace(root / "report.html")
    return summary


def saved_result(root, case_id, provider):
    path = root / "results" / provider / case_id / "result.json"
    if not path.exists():
        return None
    result = read(path)
    if result.get("case_id") != case_id or result.get("provider") != provider:
        raise ValueError("Saved result identity changed")
    if result.get("status") == "complete":
        file = Path(result["file"])
        if digest(file.read_bytes()) != result["sha256"]:
            raise ValueError("Saved anatomy candidate checksum changed")
    return result


def run(args):
    config = configuration(args.config)
    store = Store(args.output)
    try:
        reference = prepare_reference(store.root)
        corpus = read(args.case_file)
        if corpus.get("schema_version") != "infofilm.anatomy-image-cases.v1":
            raise ValueError("Unknown anatomy case schema")
        cases = corpus["cases"]
        if len({c["id"] for c in cases}) != len(cases):
            raise ValueError("Case IDs must be unique")
        registry = config["image_provider_registry"]["providers"]
        providers = {k: registry[k] for k in ["qwen_local", "flux_dev_local"]}
        definition = {"schema_version": PROTOCOL, "cases": cases, "providers": providers,
                      "reference": reference, "conditioning": args.conditioning,
                      "dimensions": [args.width, args.height], "seed": args.seed,
                      "reviewer_url": config["reviewer_url"],
                      "prompts": {c["id"]: scene_prompt(c, args.conditioning) for c in cases}}
        if args.review_policy == 'strict':
            from .anatomy_strict_review import POLICY
            definition['review_policy'] = POLICY
        if any(c.get('reference_manifest') for c in cases):
            references = {}
            for case in cases:
                if case.get('reference_manifest'):
                    path = (store.root / case['reference_manifest']).resolve()
                    if not path.is_relative_to(store.root):
                        raise ValueError('Scene reference manifest must be inside experiment root')
                    item = read(path)
                    validate_reference(item)
                    if not Path(item['file']).resolve().is_relative_to(store.root):
                        raise ValueError('Scene reference image must be inside experiment root')
                    references[case['id']] = item
            definition['case_references'] = references
        experiment = store.root / "experiment.json"
        if experiment.exists() and digest(read(experiment)) != digest(definition):
            raise ValueError("Experiment inputs changed; use a different --output directory")
        write(experiment, definition)
        if args.publish:
            files = ['report.html', 'summary.json', 'reference/reference.png']
            files += [f'results/{p}/{c["id"]}/candidate-0.png' for p in providers for c in cases]
            files += [Path(r['file']).resolve().relative_to(store.root).as_posix()
                      for r in definition.get('case_references', {}).values()]
            write(store.root/'public-report.json', {'schema_version':'infofilm.public-experiment-report.v1','files':files})
        summary = write_report(store, definition)
        if args.prepare_only:
            store.event("anatomy-experiment", "prepared", {"report": str(store.root / "report.html"), "planned": summary["planned"]})
            return
        selected = [c for c in cases if not args.cases or c["id"] in args.cases]
        if args.cases and set(args.cases) - {c["id"] for c in cases}:
            raise ValueError("Unknown requested case IDs")
        models = Models(config, store)
        for provider_id in args.providers:
            local = provider_config(config, provider_id, providers[provider_id])
            pending = []
            for case in selected:
                saved = saved_result(store.root, case["id"], provider_id)
                # Errors remain visible, and a deliberate rerun uses the adapter's
                # recorded submission to reconcile them instead of blindly POSTing.
                if not saved or saved.get("status") != "complete":
                    pending.append(case)
            if not pending:
                continue
            needs_gpu = any(not (store.root / "results" / provider_id / c["id"] / "candidate-0.png").exists() for c in pending)
            # Avoid a GPU/service switch when only a saved image review is pending.
            from contextlib import nullcontext
            with provider_session(local, store) if needs_gpu else nullcontext():
                for case in pending:
                    scene_reference = definition.get('case_references', {}).get(case['id'], reference)
                    directory = store.root / "results" / provider_id / case["id"]
                    directory.mkdir(parents=True, exist_ok=True)
                    prompt = scene_prompt(case, args.conditioning)
                    params = {"prompt": prompt, "width": str(args.width), "height": str(args.height),
                              "steps": str(local["image_steps"]), "guidance": "0", "seed": str(args.seed + cases.index(case)),
                              "output_format": "png"}
                    if args.conditioning == "reference":
                        params["reference_images"] = [{"file": scene_reference["file"], "sha256": scene_reference["sha256"]}]
                    write(directory / "request.json", {"case": case, "provider": provider_id, "params": params})
                    store.event("anatomy-experiment", "rendering", {"case": case["id"], "provider": provider_id,
                                "completed": summary["completed"], "planned": summary["planned"], "steps": local["image_steps"]})
                    start = time.monotonic()
                    try:
                        candidate = render_take(local, store, directory, params, provider_id + "-" + case["id"])
                        measured = round(time.monotonic() - start, 3)
                        timing = directory / "generation-timing.json"
                        if not timing.exists():
                            write(timing, {"seconds": measured, "includes_resume_wait": True})
                        if args.review_policy == 'strict':
                            from .anatomy_strict_review import review_candidate as strict_review
                            review = strict_review(models, directory, case, candidate, prompt, scene_reference)
                        else:
                            review = review_candidate(models, directory, case, candidate, prompt, scene_reference)
                        result = {"case_id": case["id"], "provider": provider_id, "status": "complete",
                                  "file": str(candidate), "sha256": digest(candidate.read_bytes()), "prompt": prompt,
                                  "generation_seconds": read(timing)["seconds"], "review_seconds": review["seconds"],
                                  "review": review["review"]}
                    except Exception as exc:
                        result = {"case_id": case["id"], "provider": provider_id, "status": "error",
                                  "prompt": prompt, "error": type(exc).__name__ + ": " + str(exc)}
                        candidate = directory / "candidate-0.png"
                        if candidate.is_file():
                            result.update(file=str(candidate), sha256=digest(candidate.read_bytes()))
                    write(directory / "result.json", result)
                    summary = write_report(store, definition)
                    store.event("anatomy-experiment", "progress", {"case": case["id"], "provider": provider_id,
                                "status": result["status"], "verdict": result.get("review", {}).get("verdict"),
                                "completed": summary["completed"], "errors": summary["errors"], "planned": summary["planned"]})
        store.event("anatomy-experiment", "finished", {"completed": summary["completed"], "errors": summary["errors"],
                    "planned": summary["planned"], "report": str(store.root / "report.html")})
    finally:
        store.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--case-file", type=Path, default=CASE_FILE)
    parser.add_argument("--providers", nargs="+", choices=["qwen_local", "flux_dev_local"], default=["qwen_local", "flux_dev_local"])
    parser.add_argument("--cases", nargs="+", help="Case IDs, e.g. D01 D04; other cases remain pending")
    parser.add_argument("--conditioning", choices=["reference", "none"], default="reference")
    parser.add_argument("--width", type=int, default=1536)
    parser.add_argument("--height", type=int, default=864)
    parser.add_argument("--seed", type=int, default=1947180)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument('--review-policy', choices=['legacy', 'strict'], default='legacy')
    parser.add_argument('--publish', action='store_true', help='Serve report and images through the authenticated experiment route')
    args = parser.parse_args()
    if not 256 <= args.width <= 2048 or not 256 <= args.height <= 2048 or args.width % 16 or args.height % 16:
        parser.error("Dimensions must be multiples of 16 between 256 and 2048")
    run(args)


if __name__ == "__main__":
    main()
