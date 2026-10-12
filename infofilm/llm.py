from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from pathlib import Path

import httpx

from .core import digest, read, write
from .llm_settings import api_key, endpoint


ITEM_LISTS = ("assets", "scenes")
SIMPLIFY_AFTER = 2          # failed review rounds on one item before it is simplified


def _items(candidate):
    """{id: (list key, index)} for the independently repairable items of a candidate."""
    found = {}
    for key in ITEM_LISTS:
        for index, item in enumerate(candidate.get(key) or []):
            if isinstance(item, dict) and item.get("id"):
                found[str(item["id"])] = (key, index)
    return found


def flagged_items(candidate, errors, review):
    """Map item id -> the blocking findings and validation errors that name it."""
    items, flagged = _items(candidate), {}
    def owner(text):
        match = re.match(r"\s*(\w+)\[(\d+)\]", text)
        if match and match.group(1) in ITEM_LISTS:
            entries = candidate.get(match.group(1)) or []
            index = int(match.group(2))
            if index < len(entries) and isinstance(entries[index], dict) and entries[index].get("id"):
                return str(entries[index]["id"])
        match = re.match(r"\s*([A-Za-z0-9_-]+)\s*[:.]", text)
        return match.group(1) if match and match.group(1) in items else None
    for issue in (review or {}).get("issues", []):
        if issue.get("severity") in {"blocker", "major"}:
            item = owner(str(issue.get("field", "")))
            if item:
                flagged.setdefault(item, []).append({k: issue.get(k) for k in ("field", "problem", "correction")})
    for error in errors:
        item = owner(str(error))
        if item:
            flagged.setdefault(item, []).append({"validation_error": error})
    return flagged


ANCHORS = ("id", "point_id", "start_word_id")   # a repaired scene keeps its place in the timeline


def keep_unflagged(prior, draft, flagged):
    """Targeted repair is enforced by code: the candidate stays the prior one, with only the flagged items taken from
    the draft (matched by id). Items the draft added, dropped or renumbered are ignored, so a repair cannot rebuild
    the rest (Manhattan C03: visual beats inserted scenes, renumbered 16 later ones and broke their diagrams).
    A replaced scene keeps its timeline anchors so its neighbours stay valid."""
    if not flagged:
        return draft
    result = dict(draft)
    for key in ITEM_LISTS:
        before = [a for a in prior.get(key) or [] if isinstance(a, dict)]
        after = {str(a.get("id")): a for a in draft.get(key) or [] if isinstance(a, dict) and a.get("id")}
        if not before or not after:
            continue
        merged = []
        for item in before:
            ident = str(item.get("id"))
            if ident in flagged and ident in after:
                replacement = dict(after[ident])
                if key == "scenes":
                    replacement.update({k: item[k] for k in ANCHORS if k in item})
                merged.append(replacement)
            else:
                merged.append(item)
        result[key] = merged
    return result


def repair_feedback(prior, errors, review, flagged, simplify):
    feedback = "\nREPAIR the prior candidate. Keep correct content; address actual defects.\nPRIOR:\n" + json.dumps(prior)
    if not flagged:
        return feedback + "\nFEEDBACK:\n" + json.dumps({"errors": errors, "review": review})
    targeted = {k: v for k, v in flagged.items() if k not in simplify}
    unowned = [e for e in errors if not any({"validation_error": e} in v for v in flagged.values())]
    if targeted:
        feedback += ("\nFLAGGED ITEMS (fix exactly these; return every other item unchanged, as in PRIOR):\n" +
                     json.dumps(targeted, ensure_ascii=False))
    hard = {k: v for k, v in flagged.items() if k in simplify}
    if hard:
        feedback += ("\nSIMPLIFY THESE ITEMS (they failed review " + str(SIMPLIFY_AFTER) + " times; stop re-arguing the same details):\n" +
                     json.dumps(hard, ensure_ascii=False) +
                     "\nRewrite each as a simpler, safer version. Keep its id, timing anchors and the narration point it serves. "
                     "Use only facts stated in the evidence and drop every contested or unsupported specific (injuries, times, numbers, "
                     "exact actions, who is present) instead of guessing a replacement. Prefer a generic, symbolic or establishing "
                     "composition (place, object, document, silhouette, hands) over a detailed reconstruction. List in people_in_frame "
                     "only people the scene's narration names, or none; link only fact_ids whose claim the image itself shows.")
    if unowned:
        feedback += "\nOTHER VALIDATION ERRORS:\n" + json.dumps(unowned, ensure_ascii=False)
    return feedback


REASONING_ROOM = 8000
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
TRANSIENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
                    httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)


def retry_after(response):
    try:
        return min(300, max(1, float(response.headers.get("retry-after"))))
    except (TypeError, ValueError, AttributeError):
        return None


@contextlib.contextmanager
def llm_slot(connection):
    """Cross-process limit on concurrent requests per connection (lock files, one per slot). Legacy local
    endpoints without settings keep their old unlimited behaviour."""
    if connection.get("legacy"):
        yield
        return
    folder = Path(tempfile.gettempdir()) / "infofilm-llm-slots" / digest({"url": connection["url"], "model": connection.get("model")})[:16]
    folder.mkdir(parents=True, exist_ok=True)
    slots = max(1, int(connection.get("max_concurrency") or 1))
    while True:
        for index in range(slots):
            handle = (folder / f"slot-{index}").open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
            return
        time.sleep(0.5)


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
        connection = endpoint(self.config, role)
        url = connection["url"]
        key = (os.environ.get("VLLM_API_KEY") if connection.get("legacy") else
               api_key(self.config.get("llm_secrets_root"), connection.get("id"), connection))
        if not key and connection["type"] != "openai_chat":
            raise RuntimeError(f"The {role} connection {connection.get('label', url)} needs an API key (LLM settings)")
        headers = {"Authorization": "Bearer " + key} if key else {}
        images = ([image_path] if image_path else []) + references
        system = "Return only the requested valid JSON object. Be precise, evidence-grounded and concise. Do not invent sources or claim human review."
        with httpx.Client(timeout=connection.get("timeout", 900), headers=headers) as client:
            if role not in self.identities:
                if connection.get("model"):
                    self.identities[role] = connection["model"]
                else:
                    response = client.get(url + "/models")
                    response.raise_for_status()
                    self.identities[role] = response.json()["data"][0]["id"]
            model = self.identities[role]
            if connection["type"] == "openai_responses":
                # OpenAI rejects json_object output unless the input messages themselves mention JSON.
                content = [{"type": "input_text", "text": prompt + "\n\nRespond with one JSON object."}] + [
                    {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(Path(p).read_bytes()).decode()}
                    for p in images]
                path, body = "/responses", {"model": model, "instructions": system, "input": [{"role": "user", "content": content}],
                    "reasoning": {"effort": connection.get("reasoning_effort", "low")},
                    "max_output_tokens": max(max_tokens * 3, 8000), "text": {"format": {"type": "json_object"}}, "store": False}
            else:
                content = prompt
                if images:
                    content = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(Path(p).read_bytes()).decode()}} for p in images]
                path, body = "/chat/completions", {"model": model, "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": content}], "temperature": 0.15,
                    "max_tokens": max_tokens, "response_format": {"type": "json_object"}}
                if connection["type"] == "openai_chat":     # llama.cpp / vLLM reasoning switch; hosted APIs reject it
                    body["chat_template_kwargs"] = {"enable_thinking": False}
                else:
                    # Hosted models (OpenRouter) count hidden reasoning inside max_tokens: keep reasoning light and
                    # give it room on top of the answer budget (only tokens used are billed).
                    body["reasoning"] = {"effort": "low"}
                    body["max_tokens"] = max_tokens + REASONING_ROOM
            # Do not persist inline image bytes or credentials.
            write(prefix.with_suffix(".request.json"), {"url": url, "role": role, "model": model,
                "connection": connection.get("id"), "prompt": prompt, "image": str(image_path) if image_path else None,
                "input_hash": signature,
                **({'reference_images': [str(p) for p in references]} if references else {}),
                "settings": {"temperature": 0.15, "max_tokens": max_tokens}})
            self.store.event(name, "running", {"role": role, "model": model})
            start = time.monotonic()
            response = self._post(client, connection, url + path, body, name)
            if connection["type"] == "openai_responses" and response.status_code < 500:
                raw = response.json()
            else:
                response.raise_for_status()
                raw = response.json()
            raw["measured_latency_seconds"] = round(time.monotonic() - start, 3)
            if connection["type"] == "openai_responses":     # same usage fields as chat completions, for token accounting
                usage = raw.get("usage") or {}
                raw["usage"] = {**usage, "prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}
            write(prefix.with_suffix(".response.json"), raw)
        if connection["type"] == "openai_responses":
            truncated = raw.get("status") == "incomplete" and "max_output" in str(raw.get("incomplete_details"))
            if not truncated and (response.status_code >= 400 or raw.get("status") != "completed"):
                detail = str(raw.get("error") or raw.get("incomplete_details") or raw.get("status"))[:400]
                raise RuntimeError(f"{connection.get('label', url)} request did not complete: {detail}")
            text = "".join(part.get("text", "") for item in raw.get("output", []) if item.get("type") == "message"
                           for part in item.get("content", []) if part.get("type") == "output_text")
        else:
            choice = raw["choices"][0]
            truncated = choice.get("finish_reason") == "length"
            text = choice["message"].get("content") or ""
        if truncated:
            self.store.event(name, "failed", {"reason": "output_token_limit", "max_tokens": max_tokens,
                                            "raw_response": str(prefix.with_suffix(".response.json"))})
            raise ModelOutputError(f"LLM response reached its {max_tokens}-token output limit; raw response preserved")
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

    def _post(self, client, connection, url, body, name):
        """Send one request inside the connection's concurrency limit. Outages, 429 and 5xx wait and retry with
        backoff (honouring Retry-After) and do not count as attempts; after `llm_outage_seconds` the error stands.
        Legacy endpoints (old frozen jobs, experiment harnesses with their own transport retries) post once, as before."""
        if connection.get("legacy"):
            return client.post(url, json=body)
        deadline = time.monotonic() + self.config.get("llm_outage_seconds", 1800)
        delay = 5
        while True:
            try:
                with llm_slot(connection):
                    response = client.post(url, json=body)
                status = getattr(response, "status_code", 200)
                if status not in TRANSIENT_STATUS or time.monotonic() >= deadline:
                    return response
                wait, reason = retry_after(response) or delay, f"HTTP {status}"
            except TRANSIENT_ERRORS as exc:
                if time.monotonic() >= deadline:
                    raise
                wait, reason = delay, f"{type(exc).__name__}: {exc}"
            self.store.event(name, "transient_retry", {"reason": reason[:300], "wait_seconds": wait})
            time.sleep(min(wait, max(1, deadline - time.monotonic())))
            delay = min(delay * 2, 300)

    def call_json(self, role, name, prompt, *, max_tokens):
        """Retry a malformed semantic review without drafting the plan again."""
        feedback = ""
        for index in range(2):
            try:
                return self.call(role, name if not index else name + "-format-repair", prompt + feedback, max_tokens=max_tokens)
            except ModelOutputError as exc:
                if index or getattr(exc, 'output_budget_exhausted', False):
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
        feedback, prior, flagged, prior_findings = "", None, {}, []
        strikes, simplify = {}, set()
        rounds = self.config.get("max_repairs", 2) + 1
        previous = directory / "evaluation.json"
        if previous.exists() and read(previous).get("accepted") is False:
            # A resume after an exhausted loop would replay the same cached rounds and fail the same way:
            # give it a few more rounds instead.
            rounds += self.config.get("resume_extra_rounds", 2)
            self.store.event(stage, "resume_extra_rounds", {"max_attempts": rounds})
        attempt = -1
        while attempt + 1 < rounds:
            attempt += 1
            try:
                current = self.call("planner", f"{stage}-draft-{identity[:8]}-{attempt}", prompt + feedback)
            except ModelOutputError as exc:
                errors = [str(exc)]
                history.append({"attempt": attempt, "candidate_hash": None, "errors": errors, "review": None})
                self.store.event(stage, "output_retry", {"attempt": attempt + 1,
                    "max_attempts": rounds, "error": str(exc),
                    "retrying": attempt + 1 < rounds})
                feedback += ("\nThe previous output failed its JSON/output-length contract. Return one complete compact JSON object. "
                            "Keep all required fields and entries, use concise strings and avoid repeated decorative detail. "
                            "No unfinished strings or trailing commentary. Error: " + str(exc))
                continue
            if prior is not None:
                current = keep_unflagged(prior, current, flagged)
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
                    "\nCANDIDATE:\n" + json.dumps(review_candidate) + "\nDETERMINISTIC METRICS:\n" + json.dumps(deterministic_metrics) + "\nDETERMINISTIC ERRORS:\n" + json.dumps(errors) +
                    ("\nPRIOR FINDINGS (previous round; the planner was asked to fix them):\n" + json.dumps(prior_findings) +
                     "\nCheck each one against the CURRENT candidate before repeating it, and quote the current value at that field in its evidence. "
                     "Drop a finding the current candidate already resolves; never report a value the candidate no longer contains."
                     if prior_findings else ""), max_tokens=4500)
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
            flagged = flagged_items(current, errors, review)
            for item in flagged:
                strikes[item] = strikes.get(item, 0) + 1
            hard = {item for item in flagged if strikes[item] >= SIMPLIFY_AFTER} - simplify
            if hard:
                if not simplify:        # simplification gets its own rounds, once per stage
                    rounds += self.config.get("simplify_rounds", 2)
                simplify |= hard
                self.store.event(stage, "simplify", {"attempt": attempt + 1, "items": sorted(hard), "max_attempts": rounds})
            owned = [e for v in flagged.values() for e in (x.get("validation_error") for x in v) if e]
            prior = current if all(e in owned for e in errors) else None   # a batch-level error may need any item
            prior_findings = [{k: x.get(k) for k in ("field", "severity", "problem")}
                              for x in (review or {}).get("issues", []) if x.get("severity") in {"blocker", "major"}]
            feedback = repair_feedback(current, errors, review, flagged, simplify)
        write(directory / "evaluation.json", {"stage": stage, "attempts": history, "accepted": False})
        raise RuntimeError(f"{stage} exhausted its bounded planner/reviewer trial; candidates remain in {directory}")
