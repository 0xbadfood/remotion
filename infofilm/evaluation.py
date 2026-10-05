from __future__ import annotations

from .core import read, write


def evaluate(store):
    stages = []
    for file in sorted((store.root / "planning").glob("**/evaluation.json")):
        value = read(file)
        stages.append({"file": str(file), **value,
                       "model_accepted": value.get("model_accepted", value.get("accepted", "selected_hash" in value))})
    requests = []
    for file in sorted((store.root / "requests").glob("*.response.json")):
        response = read(file)
        request = read(file.with_name(file.name.replace(".response.json", ".request.json")))
        requests.append({"file": str(file), "role": request["role"], "model": request["model"],
                         "latency_seconds": response.get("measured_latency_seconds", 0), "usage": response.get("usage", {})})
    report = {"stages": stages, "provider_calls": requests,
              "measured_provider_latency_seconds": sum(r["latency_seconds"] for r in requests),
              "reported_model_tokens": sum(r["usage"].get("total_tokens", 0) for r in requests),
              "latency_scope": "Sum of measured individual provider response times; overlapping calls mean this is not elapsed project wall time.",
              "limitations": ["One subject and two requested durations do not establish general efficacy.",
                             "The smaller reviewer also performs blinded comparisons; this is model preference evidence, not an independent human experiment.",
                             "A passing score is not proof of factual or visual correctness.",
                             "Failed early schemes and source-audit corrections remain in the record."]}
    write(store.root / "evaluation.json", report)
    print(f"Evaluation saved: {len(stages)} stage trials, {len(requests)} measured calls")
    return report
