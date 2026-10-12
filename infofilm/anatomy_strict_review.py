"""Versioned anatomy screening with code-owned acceptance gates."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .core import digest, read, write
from .llm import ModelOutputError
from .person_references import validate_reference
from .educational_review import GUIDANCE, inventory_errors, inventory_failures, normalize_inventory

POLICY = "infofilm.anatomy-screening.components.v3"
CHECKS = {
    "D01": ["Liver and stomach have the stated anterior-view positions.", "A single esophagus visibly reaches the stomach.", "Small intestine is central and colon frames it, with a terminating rectum."],
    "D02": ["The torso shows the stated left-right organ positions.", "Pancreas is distinguishable in the promised transparent/cutaway view.", "Digestive organs are distinct rather than duplicated or fused."],
    "D03": ["The requested focused upper-abdominal view dominates; a whole-system diagram is not a substitute.", "Liver, gallbladder, stomach, pancreas and duodenum are each distinguishable.", "Visible bile and pancreatic ducts reach the duodenum, rather than ending at stomach or other organs."],
    "D04": ["An isolated stomach cutaway with a short duodenum is shown; a whole bowel diagram is not substituted.", "There is one esophageal inlet and one pyloric outlet; trace their actual paths and count any extra outlet-like branches.", "A continuous lumen connects inlet to outlet, with internal lining folds and contents inside it."],
    "D05": ["A short bowel segment is shown as a hollow tube with a continuous lumen.", "Inner folds and villi project into the lumen from the mucosa, rather than outside the bowel.", "Villi remain smaller than the bowel lumen and the surrounding wall is continuous."],
    "D06": ["An isolated esophageal segment is shown, rather than a whole-system diagram.", "There is a continuous open passage below the bolus; trace it, do not assume it continues behind an opaque endpoint.", "A localized contraction is visible above the downward-moving bolus, with a relaxed, open passage below it."],
}


def gates(case):
    return {"G01": "Candidate contains plain surrounding space: generated labels, leader lines and source-card panels are absent.",
            **{f"G{i+2:02d}": text for i, text in enumerate(CHECKS[case["id"]])}}


def errors(value, case):
    if not isinstance(value, dict):
        return ["Return one JSON object"]
    checks = value.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(gates(case)):
        return ["Return exactly the gate IDs as keys of checks"]
    result = inventory_errors(value, required=True, nonempty=True)
    for key, item in checks.items():
        if (not isinstance(item, dict) or item.get("status") not in {"met", "not_met", "uncertain"} or
            any(not isinstance(item.get(k), str) or not item[k].strip() for k in ["observation", "location"])):
            result.append(key + " needs status, concrete observation and image location")
    for field in ["limitations", "inferred_details"]:
        if not isinstance(value.get(field), list) or any(not isinstance(v, str) for v in value[field]):
            result.append(field + " must be a list of strings")
    return result


def decide(value):
    statuses = [v["status"] for v in value["checks"].values()]
    # Attractive rendering and an LLM's overall opinion cannot override a gate.
    return "fail" if "not_met" in statuses or inventory_failures(value) else "uncertain" if "uncertain" in statuses else "pass"


def review_candidate(models, root, case, candidate, prompt, reference):
    root = Path(root)
    destination = root / "review-components-v3.json"
    packet = {"case_id": case["id"], "visual_message": case["visual_message"],
              "current_prompt": prompt, "mandatory_gates": gates(case),
              "reference_scope": reference["scope"]}
    example = {"checks": {k: {"status": "uncertain", "observation": "Specific visible observation",
                              "location": "Describe position in the candidate image"} for k in gates(case)},
               "component_inventory": [{"component":"A component explicitly requested in the prompt",
                                        "expected_count":1,"visible_count":None,"count_status":"uncertain",
                                        "placement_status":"uncertain","reference_checked":True,
                                        "evidence":"Concrete candidate locations and actual source comparison"}],
               "limitations": [], "inferred_details": []}
    task = GUIDANCE + ("Inspect image 1, the generated candidate. Image 2 is its sourced anatomy reference. "
            "Evaluate every mandatory gate using pixels in IMAGE 1. Never describe the reference as if it appeared in the candidate. "
            "First inventory visible organs and text; then trace each requested connection from one visible endpoint to the other. "
            "Count branches and openings explicitly. Missing visibility means uncertain; a visible contradiction means not_met. "
            "Do not excuse a substituted whole-body composition when an isolated segment/close-up was requested. "
            "Do not excuse generated labels simply because they are correctly spelled. "
            "For ducts and cutaways, do not infer hidden continuations solely from expected anatomy. "
            "Ignore cosmetic preferences, illustration style and benign shading. Do not demand photorealism. "
            "The source can support only details actually depicted. Record inferred internal detail in limitations. "
            "A lack of microscopic validation is not itself a visible contradiction. "
            "There is no prior review, provider identity or desired outcome. Do not provide an overall pass or numeric score. "
            "Return valid JSON exactly in the example shape: checks is an OBJECT keyed by gate IDs, never an array. "
            "Each observation must describe concrete visible evidence; avoid generic assertions such as anatomically correct. "
            "Be concise, maximum two sentences per gate.\nEXAMPLE SHAPE:\n" + json.dumps(example) +
            "\nCURRENT INPUT:\n" + json.dumps(packet))
    signature = digest({"policy": POLICY, "task": task, "candidate": digest(candidate.read_bytes()),
                        "reference": reference["sha256"], "reviewer": models.config["reviewer_url"]})
    if destination.exists():
        record = read(destination)
        if record["input_sha256"] != signature or errors(record["raw_review"], case):
            raise ValueError("Strict-review checkpoint inputs or schema changed")
        if record['review']['verdict'] != decide(record['raw_review']):
            raise ValueError('Saved verdict conflicts with its mandatory gates')
        return record
    feedback = ""
    start = time.monotonic()
    for index in range(3):
        try:
            value = models.call("reviewer", f"anatomy-strict-{root.parent.name}-{case['id']}-{index}",
                                task + feedback, image_path=candidate,
                                reference_image_paths=[validate_reference(reference)], max_tokens=2400)
            value = normalize_inventory(value)
            problems = errors(value, case)
        except ModelOutputError as exc:
            problems = [str(exc)]
        if not problems:
            verdict = decide(value)
            issues = [k + ": " + item["observation"] for k, item in value["checks"].items() if item["status"] == "not_met"]
            issues += inventory_failures(value)
            review = {"case_id": case["id"], "policy": POLICY, "verdict": verdict, "checks": value["checks"],
                      "component_inventory": value['component_inventory'],
                      "issues": issues, "limitations": value["limitations"], "inferred_details": value["inferred_details"]}
            record = {"input_sha256": signature, "seconds": round(time.monotonic()-start, 3),
                      "raw_review": value, "review": review}
            write(destination, record)
            return record
        feedback = "\nReturn the complete OBJECT-based example schema. Fix: " + json.dumps(problems)
    raise ValueError("Strict reviewer schema failed: " + "; ".join(problems))
