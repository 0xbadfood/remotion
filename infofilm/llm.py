from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

import httpx

from .core import digest, read, write


class ModelOutputError(ValueError):
    """A saved response failed the output contract; safe to retry within a budget."""


class Models:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.identities = {}

    def call(self, role, name, prompt, *, image_path=None, reference_image_paths=None, max_tokens=7000):
        root = self.store.root / "requests"
        references = list(reference_image_paths or [])
        signature = digest({"url": self.config[role + "_url"], "prompt": prompt,
                            "image": digest(Path(image_path).read_bytes()) if image_path else None,
                            "max_tokens": max_tokens,
                            **({'reference_images': [digest(Path(p).read_bytes()) for p in references]} if references else {})})
        prefix = root / f"{name}-{signature[:12]}"
        if prefix.with_suffix(".result.json").exists():
            return read(prefix.with_suffix(".result.json"))
        headers = {}
        if os.environ.get("VLLM_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["VLLM_API_KEY"]
        url = self.config[role + "_url"]
        with httpx.Client(timeout=900, headers=headers) as client:
            if role not in self.identities:
                response = client.get(url + "/models")
                response.raise_for_status()
                self.identities[role] = response.json()["data"][0]["id"]
            content = prompt
            if image_path or references:
                content = [{"type": "text", "text": prompt}]
                for path in ([image_path] if image_path else []) + references:
                    content.append({"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()}})
            body = {"model": self.identities[role], "messages": [
                {"role": "system", "content": "Return only the requested valid JSON object. Be precise, evidence-grounded and concise. Do not invent sources or claim human review."},
                {"role": "user", "content": content}], "temperature": 0.15,
                "max_tokens": max_tokens, "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False}}
            # Do not persist inline image bytes or credentials.
            write(prefix.with_suffix(".request.json"), {"url": url, "role": role, "model": self.identities[role],
                "prompt": prompt, "image": str(image_path) if image_path else None, "input_hash": signature,
                **({'reference_images': [str(p) for p in references]} if references else {}),
                "settings": {"temperature": 0.15, "max_tokens": max_tokens}})
            self.store.event(name, "running", {"role": role, "model": self.identities[role]})
            start = time.monotonic()
            response = client.post(url + "/chat/completions", json=body)
            response.raise_for_status()
            raw = response.json()
            raw["measured_latency_seconds"] = round(time.monotonic() - start, 3)
            write(prefix.with_suffix(".response.json"), raw)
        choice = raw["choices"][0]
        if choice.get("finish_reason") == "length":
            self.store.event(name, "failed", {"reason": "output_token_limit", "max_tokens": max_tokens,
                                            "raw_response": str(prefix.with_suffix(".response.json"))})
            raise ModelOutputError(f"LLM response reached its {max_tokens}-token output limit; raw response preserved")
        text = choice["message"].get("content") or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            self.store.event(name, "failed", {"reason": "invalid JSON; raw response preserved"})
            raise ModelOutputError(f"LLM returned invalid JSON: {exc}; raw response preserved") from exc
        if not isinstance(value, dict):
            raise ModelOutputError("Expected one JSON object; raw response preserved")
        write(prefix.with_suffix(".result.json"), value)
        self.store.event(name, "succeeded", {"latency_seconds": raw["measured_latency_seconds"], "usage": raw.get("usage", {})})
        return value

    def call_json(self, role, name, prompt, *, max_tokens):
        """Retry a malformed semantic review without drafting the plan again."""
        feedback = ""
        for index in range(2):
            try:
                return self.call(role, name if not index else name + "-format-repair", prompt + feedback, max_tokens=max_tokens)
            except ModelOutputError as exc:
                if index:
                    raise
                feedback = ("\nThe response exceeded its output limit or failed JSON parsing. Return a complete compact JSON object "
                            "with the requested fields, concise issues and evidence. Error: " + str(exc))
                self.store.event(name, "format_retry", {"attempt": index + 1, "max_attempts": 2, "error": str(exc)})

    def loop(self, stage, task, context, validator, *, experimental=True, metrics=None):
        root = self.store.root / "planning" / stage
        identity = digest({"task": task, "context": context, "experimental": experimental,
                           "planner": self.config["planner_url"], "reviewer": self.config["reviewer_url"],
                           **({"review_contract":"narrative-semantic-v2"} if stage.startswith("script-") else {})})
        directory = root / identity[:16]
        selected = directory / "selected.json"
        if selected.exists():
            result = read(selected)
            errors = validator(result)
            if errors:
                raise ValueError(f"Cached {stage} fails current validation: {errors}")
            return result
        prompt = task + "\nINPUT CONTEXT:\n" + json.dumps(context, ensure_ascii=False)
        initial, current, history = None, None, []
        feedback = ""
        for attempt in range(self.config.get("max_repairs", 2) + 1):
            try:
                current = self.call("planner", f"{stage}-draft-{identity[:8]}-{attempt}", prompt + feedback)
            except ModelOutputError as exc:
                errors = [str(exc)]
                history.append({"attempt": attempt, "candidate_hash": None, "errors": errors, "review": None})
                self.store.event(stage, "output_retry", {"attempt": attempt + 1,
                    "max_attempts": self.config.get("max_repairs", 2) + 1, "error": str(exc),
                    "retrying": attempt < self.config.get("max_repairs", 2)})
                feedback += ("\nThe previous output failed its JSON/output-length contract. Return one complete compact JSON object. "
                            "Keep all required fields and entries, use concise strings and avoid repeated decorative detail. "
                            "No unfinished strings or trailing commentary. Error: " + str(exc))
                continue
            write(directory / f"candidate-{attempt}.json", current)
            if initial is None:
                initial = current
            errors = validator(current)
            review = None
            if experimental and not errors:
                deterministic_metrics = metrics(current) if metrics else {}
                review_task, review_context, review_candidate = task, context, current
                if isinstance(current.get("points"), list):
                    # Give the semantic reviewer no numeric runtime target. Earlier
                    # trials showed that instructions alone did not stop it inventing
                    # hard word limits and damaging otherwise useful narration.
                    review_task = ("Review this informational narration for factual accuracy, causal meaning, natural delivery and a coherent explanation of its subject. "
                        "Assess the selected points and their flow. The evidence brief contains optional material for different depths; do not require every fact or editorial priority. "
                        "Runtime is advisory. Do not ask for compression, padding, faster delivery or removal of sound content to meet a time or word budget. "
                        "Only source-grounded meaning and narrative quality are acceptance criteria.")
                    if context.get("evidence", {}).get("chapter_context"):
                        review_task += (" This narration is a chapter in a longer film. Respect its planned opening/ending roles, "
                                        "entry context, exclusions and handoff. Do not demand a standalone introduction or conclusion for each chapter. "
                                        "The chapter outline is direction, not evidence for unverified facts.")
                    evidence = context.get("evidence", {})
                    review_context = {"evidence": {k:v for k,v in evidence.items() if k != "narrative_priorities"}}
                    if context.get("creative_brief"):
                        review_context["creative_brief"] = context["creative_brief"]
                    review_candidate = {k:v for k,v in current.items() if k not in {"requested_seconds","duration_seconds","target_seconds"}}
                    deterministic_metrics = {"duration_policy":"advisory; final recording determines duration"}
                review = self.call_json("reviewer", f"{stage}-review-{identity[:8]}-{attempt}",
                    "Independently review the candidate against the task and source context. Identify concrete factual, causal, narration, pacing or executable-visual problems. "
                    "Do not request cosmetic edits without benefit. Every finding needs a field pointer, evidence and actionable correction. "
                    "Distinguish explicit requirements from artistic preferences. Only demonstrable factual errors, explicit requirement violations or clearly unusable content are major. Speculative generation risks and optional refinements are minor. Do not invent an exclusive palette, require photographs to encode diagrams, or ban ordinary weather features. "
                    "A spoken number does not by itself require an infographic. Native banners/captions can carry exact values over contextual imagery. Script visual_intent suggestions are not binding shot directions. "
                    "Do not alternate between demanding a graphic and demanding an image for the same correct explanation. Graphic versus image preferences are minor unless the candidate violates an explicit requirement or misrepresents the facts. Corrections must not introduce facts absent from the evidence. "
                    "Do not count words, estimate exact delivery seconds or reject validated structural constraints: deterministic code owns these. Duration is advisory and actual audio determines final runtime. Use supplied metrics as authoritative. Focus on semantic accuracy, useful clarity, fluent progression and cognitive pacing. "
                    "Return {pass:boolean, score:0..100, issues:[{field,severity:'blocker'|'major'|'minor',problem,evidence,correction}], strengths:[string]}. "
                    "Pass requires score >=82 and no blocker or major issue.\nTASK:\n" + review_task + "\nCONTEXT:\n" + json.dumps(review_context) +
                    "\nCANDIDATE:\n" + json.dumps(review_candidate) + "\nDETERMINISTIC METRICS:\n" + json.dumps(deterministic_metrics) + "\nDETERMINISTIC ERRORS:\n" + json.dumps(errors), max_tokens=4500)
                write(directory / f"review-{attempt}.json", review)
            history.append({"attempt": attempt, "candidate_hash": digest(current), "errors": errors, "review": review})
            passing = not errors and (not experimental or (review is not None and review.get("pass") is True and
                isinstance(review.get("score"), (int, float)) and review["score"] >= 82 and
                not any(x.get("severity") in {"blocker", "major"} for x in review.get("issues", []))))
            if passing:
                write(selected, current)
                summary = {"stage": stage, "initial_hash": digest(initial), "selected_hash": digest(current),
                           "changed": initial != current, "accepted":True, "attempts": history, "experimental": experimental,
                           "human_acceptance": "pending"}
                if experimental and initial != current:
                    # Randomize the label assignment without exposing which draft is newer.
                    swap = int(identity[0], 16) % 2 == 0
                    a, b = (current, initial) if swap else (initial, current)
                    validation = {label: validator(candidate) for label, candidate in [("A", a), ("B", b)]}
                    comparison_a, comparison_b = a, b
                    if isinstance(current.get("points"), list):
                        comparison_a, comparison_b = [
                            {k:v for k,v in candidate.items() if k not in {"requested_seconds", "duration_seconds", "target_seconds"}}
                            for candidate in [a, b]]
                    comparison = self.call_json("reviewer", f"{stage}-blind-comparison-{identity[:8]}",
                        "Compare anonymized candidates A and B against the source and task. Runtime is advisory; do not impose word counts or duration limits. "
                        "Code validation is authoritative about allowed fields and enum values. Do not prefer an invalid candidate by inventing a different schema. "
                        "Return {preferred:'A'|'B'|'tie', reasons:[{criterion,evidence_A,evidence_B}], regressions:[string], limitations:[string]}. "
                        "A preference must be justified by specific observable content, not familiarity.\nTASK:\n" + review_task + "\nCONTEXT:\n" + json.dumps(review_context) +
                        "\nCODE VALIDATION:\n" + json.dumps(validation) +
                        "\nA:\n" + json.dumps(comparison_a) + "\nB:\n" + json.dumps(comparison_b), max_tokens=3500)
                    summary["blind_comparison"] = {"label_mapping": {"A": "selected" if swap else "initial", "B": "initial" if swap else "selected"},
                                                 "code_validation": validation, **comparison}
                write(directory / "evaluation.json", summary)
                write(root / "current.json", {"input_hash": identity, "selected": str(selected), "evaluation": str(directory / "evaluation.json")})
                return current
            feedback = "\nREPAIR the prior candidate. Keep correct content; address actual defects.\nPRIOR:\n" + json.dumps(current) + "\nFEEDBACK:\n" + json.dumps({"errors": errors, "review": review})
        write(directory / "evaluation.json", {"stage": stage, "attempts": history, "accepted": False})
        raise RuntimeError(f"{stage} exhausted its bounded planner/reviewer trial; candidates remain in {directory}")
