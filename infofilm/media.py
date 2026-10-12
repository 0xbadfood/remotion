from __future__ import annotations

import importlib.util
import contextlib
import json
import os
import math
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import wave
from pathlib import Path

import httpx

from .core import ROOT, digest, gpu_lease, read, write, resource_slot, shares_gpu
from .composition import asset_composition
from .planning import word_count


class AlignmentError(RuntimeError):
    """The bounded MFA attempts could not produce validated word timing."""


def mfa_numeric_text(text, *, context=None, offset=0, natural_years=True):
    """Shared explicit numeric wording for English synthesis and MFA.

    Only provider/alignment input is expanded; source scripts and captions retain
    their exact original spelling. Preserved provider submissions stay immutable.
    """
    from num2words import num2words
    from .audio_audit import spoken_numeric_text
    from .numeric_speech import year_text

    value = unicodedata.normalize("NFKC", str(text or ""))
    # A hyphen in mid-1970s joins a compound; it is not a negative number.
    value = re.sub(r"(?<=[A-Za-z])[-–—](?=[12]\d{2}0s\b)", " ", value, flags=re.I)

    def decade(match):
        year = num2words(int(match.group(1)), lang="en", to="year")
        return year[:-1] + "ies" if year.endswith("y") else year + "s"

    value = re.sub(r"(?<![\w.])([12]\d{2}0)s(?!\w)", decade, value, flags=re.I)
    if natural_years:
        value = year_text(value, context=context, offset=offset)
    return spoken_numeric_text(value)


def request(config, service, method, path, **kwargs):
    """A provider queue that is full (429), busy with another job (409 from a single-job server) or briefly
    unavailable (503) answers "wait your turn": retry after its Retry-After (or with backoff) for up to
    `provider_wait_seconds`, so parallel chapters queue instead of failing."""
    deadline, delay = time.monotonic() + config.get("provider_wait_seconds", 7200), 5
    timeout = kwargs.pop("timeout", 90)
    while True:
        for value in kwargs.get("files") or []:      # rewind uploads before a retry
            handle = value[1][1] if isinstance(value, tuple) and isinstance(value[1], tuple) else None
            if hasattr(handle, "seek"):
                handle.seek(0)
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, config[service + "_url"] + path, **kwargs)
        # A single-job server (Spark VibeVoice) answers 409 "A render job is (already) active" while it serves another
        # chapter: that is a queue, not an error. ("Job X is already active" — our own job — still stands.)
        busy = response.status_code == 409 and "A render job is" in response.text
        if (response.status_code in {429, 503} or busy) and time.monotonic() < deadline:
            try:
                wait = min(300, max(1, float(response.headers.get("retry-after"))))
            except (TypeError, ValueError):
                wait = delay
            time.sleep(wait)
            delay = min(delay * 2, 120)
            continue
        response.raise_for_status()
        return response.json()


def wait_job(config, service, path, success, failure, store, name, timeout=1800):
    start = time.monotonic()
    last_notice = -30
    while time.monotonic() - start < timeout:
        job = request(config, service, "GET", path)
        state = job.get("status")
        if state in success:
            return job
        if state in failure:
            raise RuntimeError(f"{service} job failed: {json.dumps(job)}")
        if time.monotonic() - start - last_notice > 25:
            last_notice = time.monotonic() - start
            store.event(name, "waiting", {"state": state, "elapsed_seconds": round(last_notice)})
        time.sleep(2)
    raise TimeoutError(f"{name} is still unresolved; resume its recorded provider job")


def audio_info(path):
    with wave.open(str(path)) as audio:
        pcm = audio.readframes(audio.getnframes())
        return {"sample_count": audio.getnframes(), "sample_rate": audio.getframerate(), "channels": audio.getnchannels(),
                "duration_seconds": audio.getnframes() / audio.getframerate(), "pcm_sha256": digest(pcm),
                "audio_sha256": digest(Path(path).read_bytes())}


def _gate_vibe_audio(config, store, audio, info, name):
    from .silence import cleanup
    cleaned, report = cleanup(audio, config, store, name)
    return cleaned, {**info, **audio_info(cleaned), 'silence_cleanup': report,
                     'observed_words_per_minute': round(word_count(info['text']) / audio_info(cleaned)['duration_seconds'] * 60, 3)}


def render_audio(config, store, text, name):
    reference = Path(config["reference_audio"]).expanduser().resolve()
    identity = digest({"text": text, "reference_hash": digest(reference.read_bytes()),
                       "url": config["tts_url"], "language": config["language"], "cfg_scale": 1.3,
                       "variant": config.get("audio_variant", 0)})
    directory = store.root / "audio" / identity[:20]
    final = directory / "narration.wav"
    if final.exists() and (directory / "result.json").exists():
        return _gate_vibe_audio(config, store, final, read(directory / "result.json"), name)
    directory.mkdir(parents=True, exist_ok=True)
    record_path = directory / "provider.json"
    job_id = "infofilm_" + identity[:28]
    started = time.monotonic()
    with gpu_lease(config, "voice"):
        health = request(config, "tts", "GET", "/health")
        if health.get("queued_or_running_jobs") and not record_path.exists():
            raise RuntimeError("VibeVoice has other queued/running work; leave it undisturbed")
        if shares_gpu(config, "voice", "images"):      # free the shared GPU only when voice and images share one
            image = request(config, "image", "GET", "/api/status")
            if image.get("busy"):
                raise RuntimeError("Image service is busy; cannot switch shared GPU")
            if image.get("state") == "loading":
                raise RuntimeError("Image service is loading; cannot switch shared GPU")
            if image.get("state") == "loaded":
                request(config, "image", "POST", "/api/unload")
        try:
            if not record_path.exists():
                with reference.open("rb") as handle:
                    staged = request(config, "tts", "POST", "/stage-speaker", data={"speaker_id": "infofilm_" + identity[:16]},
                                     files={"file": (reference.name, handle, "audio/wav")})
                body = {"job_id": job_id, "lang_key": config["language"], "script": "Speaker 1: " + mfa_numeric_text(text),
                        "voice_samples": [staged["voice_ref"]], "cfg_scale": 1.3, "output_name": "narration.wav"}
                write(record_path, {"state": "submission_started", "job_id": job_id, "body": body})
                response = request(config, "tts", "POST", "/render-chunk", json=body)
                write(record_path, {"state": "submitted", "job_id": job_id, "body": body, "response": response})
            job = wait_job(config, "tts", "/jobs/" + job_id, {"success"}, {"failed"}, store, name)
            write(directory / "provider-result.json", job)
            with httpx.Client(timeout=180) as client:
                response = client.get(config["tts_url"] + "/jobs/" + job_id + "/audio")
                response.raise_for_status()
                (directory / "provider.wav").write_bytes(response.content)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(directory / "provider.wav"), "-ac", "1", "-c:a", "pcm_s16le", str(final)], check=True)
        finally:
            from .gpu_release import release_vibevoice
            release_vibevoice(config, store, request_fn=request, allow_restart=final.exists())
    info = {**audio_info(final), "text": text, "text_sha256": digest(text), "reference_audio": str(reference),
            "reference_audio_sha256": digest(reference.read_bytes()), "language": config["language"], "provider": "VibeVoice",
            "reference_transcript": config.get("reference_transcript"),
            "reference_transcript_used_for_conditioning": False,
            "job_id": job_id, "measured_latency_seconds": round(time.monotonic() - started, 3),
            "observed_words_per_minute": round(word_count(text) / audio_info(final)["duration_seconds"] * 60, 3)}
    write(directory / "result.json", info)
    store.event(name, "succeeded", {"duration_seconds": info["duration_seconds"], "observed_words_per_minute": info["observed_words_per_minute"]})
    return _gate_vibe_audio(config, store, final, info, name)


def audit_audio(config, store, audio, text, points=None):
    sys.path.insert(0, config["quiz_root"])
    from .audio_audit import audit_with_escalation
    return audit_with_escalation(config, store, audio, text, points=points)


def map_source_words(points, aligned, tokenize, source_tokenize=None):
    """Rejoin MFA clitics/hyphens, retaining source spelling and precise spans."""
    words, boundaries, cursor = [], [], 0
    for point in points:
        first = len(words)
        for match in re.finditer(r"\S+", point["narration"]):
            display = match.group()
            expected = "".join(source_tokenize(display, point, match.start())
                               if source_tokenize else tokenize(display))
            if not expected:
                continue
            start, actual = cursor, ""
            # MFA's English dictionary emits <unk> for digit-written years and
            # ordinals. Keep the source spelling and that token's measured span;
            # every other unknown still fails the exact transcript check.
            if (cursor < len(aligned) and aligned[cursor]["word"] == "<unk>"
                    and re.fullmatch(r"\d+(?:st|nd|rd|th)?", expected, re.I)):
                actual = expected
                cursor += 1
            while cursor < len(aligned) and len(actual) < len(expected):
                actual += "".join(tokenize(aligned[cursor]["word"]))
                cursor += 1
                if not expected.startswith(actual):
                    raise ValueError(f"MFA transcript mismatch at {point['id']}: {display!r} vs {actual!r}")
            if actual != expected:
                raise ValueError(f"MFA transcript incomplete at {point['id']}: {display!r}")
            words.append({"id": f"w{len(words)+1:04d}", "word": display,
                          "start": aligned[start]["start"], "end": aligned[cursor-1]["end"],
                          "mfa_word_ids": [w["id"] for w in aligned[start:cursor]]})
        if first == len(words):
            raise ValueError(f"Empty narrated point: {point['id']}")
        boundaries.append({"point_id": point["id"], "start": words[first]["start"], "end": words[-1]["end"],
                           "word_ids": [w["id"] for w in words[first:]]})
    if cursor != len(aligned):
        raise ValueError("MFA transcript has unmatched trailing words")
    return words, boundaries


def _mfa_adapter(config):
    module_path = Path(config["story_backend"]) / "experiments/compare_story_mfa_alignment.py"
    spec = importlib.util.spec_from_file_location("infofilm_mfa", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module_path


def align_audio(config, store, audio, script, name, *, spoken_points=None):
    from .audio_audit import spoken_numeric_text

    text = "\n\n".join(p["narration"] for p in script["points"])
    # English G2P cannot reliably pronounce accented Latin names. Change only
    # its orthographic input, while keeping the recording and display intact.
    lexicon_text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    dependencies = {"audio": digest(Path(audio).read_bytes()), "text": text}
    legacy_identity = digest({**dependencies,
        **({"mfa_orthography": lexicon_text} if lexicon_text != text else {})})
    legacy = store.root / "alignment" / legacy_identity[:20]
    # Saved provider wording belongs to the saved recording. In particular,
    # do not force old cardinal-year speech through a new split-year transcript.
    year_styles = {}
    for point in script["points"]:
        source = point["narration"]
        recorded = (spoken_points or {}).get(point["id"])
        current = mfa_numeric_text(source)
        prior = mfa_numeric_text(source, natural_years=False)
        if recorded is not None and recorded not in {source, current, prior}:
            raise ValueError("Saved synthesis wording does not match its source point")
        year_styles[point["id"]] = recorded is None or recorded == current
    expanded = "\n\n".join(mfa_numeric_text(p["narration"], natural_years=year_styles[p["id"]])
                           for p in script["points"])
    expanded = "".join(c for c in unicodedata.normalize("NFKD", expanded) if not unicodedata.combining(c))
    spoken_text = re.sub(r"[ \t]+", " ", expanded).strip()
    alignment_policy = ("natural-years-v3" if any(year_styles[p["id"]] and
                        mfa_numeric_text(p["narration"]) != mfa_numeric_text(p["narration"], natural_years=False)
                        for p in script["points"]) else
                        "spoken-decades-v2" if spoken_text != spoken_numeric_text(lexicon_text)
                        else "spoken-numbers-v1")

    def profile(strategy, transcript, beam=160, retry_beam=320):
        identity = digest({**dependencies, "mfa_orthography": transcript,
                           "alignment_policy": alignment_policy, "beam": beam, "retry_beam": retry_beam})
        return {"strategy": strategy, "text": transcript, "beam": beam, "retry_beam": retry_beam,
                "directory": store.root / "alignment" / identity[:20]}

    original = {"strategy": "original_orthography", "text": lexicon_text,
                "beam": 160, "retry_beam": 320, "directory": legacy}
    primary = profile("spoken_numbers", spoken_text) if spoken_text != lexicon_text else original
    # Diagnose preserved pre-fix results without repeating their provider call.
    # Fresh runs send spoken numbers to MFA on their first attempt.
    if spoken_text != lexicon_text and (legacy / "mfa-raw.json").is_file():
        profiles = [original, primary]
    else:
        profiles = [primary, profile("wider_search", primary["text"], 320, 640)]
    cached_paths = [candidate["directory"] / "timing.json" for candidate in reversed(profiles)]
    prior_text = re.sub(r"[ \t]+", " ", mfa_numeric_text(lexicon_text, natural_years=False)).strip()
    if prior_text != spoken_text:
        prior_policy = "spoken-decades-v2" if prior_text != spoken_numeric_text(lexicon_text) else "spoken-numbers-v1"
        for beam, retry_beam in [(160, 320), (320, 640)]:
            prior_identity = digest({**dependencies, "mfa_orthography": prior_text,
                                     "alignment_policy": prior_policy, "beam": beam, "retry_beam": retry_beam})
            cached_paths.append(store.root / "alignment" / prior_identity[:20] / "timing.json")
    for path in cached_paths:
        if path.exists():
            cached = read(path)
            if (cached.get("audio_sha256") == dependencies["audio"] and cached.get("text_sha256") == digest(text)
                    and [p["point_id"] for p in cached["points"]] == [p["id"] for p in script["points"]]):
                return cached

    module, module_path = _mfa_adapter(config)
    info = audio_info(audio)
    history = []
    store.event(name, "running", {"engine": "MMS" if config.get("aligner", "mfa") == "mms" else "MFA",
                                  "audio": str(audio), "max_attempts": len(profiles)})
    last_error = None
    for index, candidate in enumerate(profiles, 1):
        directory = candidate["directory"]
        directory.mkdir(parents=True, exist_ok=True)
        detail = {"attempt": index, "max_attempts": len(profiles), "strategy": candidate["strategy"],
                  "beam": candidate["beam"], "retry_beam": candidate["retry_beam"]}
        if index > 1:
            store.event(name, "retrying", {**detail, "reason": str(last_error), "audio_regenerated": False})
        else:
            store.event(name, "attempt_running", detail)
        try:
            raw = directory / "mfa-raw.json"
            if raw.exists():
                payload = read(raw)
                metadata = read(directory / "mfa-metadata.json") if (directory / "mfa-metadata.json").exists() else {"resumed_preserved_raw_alignment": True}
            else:
                # The adapter also retries decoder failures with wider beams. MFA is CPU-heavy: parallel chapters
                # share a fixed number of alignment slots (a queue), instead of oversubscribing the cores.
                with resource_slot("mfa", config.get("mfa_slots", max(2, (os.cpu_count() or 8) // 8))):
                    if config.get("aligner", "mfa") == "mms":
                        # MMS forced alignment (GPU, any architecture); same output shape and tokenizer as MFA.
                        from .mms_align import align as mms_align
                        payload, metadata = mms_align(Path(audio), candidate["text"], tokenize=module.tokenize,
                                                      device=config.get("aligner_device"))
                    else:
                        payload, _, metadata = module.run_mfa_alignment(
                            module.DEFAULT_MFA_BIN, module.DEFAULT_MAMBA_BIN, module.DEFAULT_MFA_ROOT, Path(audio), candidate["text"],
                            module.DEFAULT_ACOUSTIC_MODEL, module.DEFAULT_DICTIONARY_MODEL, module.DEFAULT_G2P_MODEL,
                            directory / "work", beam=candidate["beam"], retry_beam=candidate["retry_beam"])
                write(raw, payload)
                write(directory / "mfa-metadata.json", metadata)
            aligned = [{"id": f"mfa{i+1:04d}", "word": str(entry[2]), "start": float(entry[0]), "end": float(entry[1])}
                       for i, entry in enumerate(payload["tiers"]["words"]["entries"])
                       if str(entry[2]).strip() and entry[2] != "<eps>"]

            def normalized_tokens(value):
                value = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
                if candidate["text"] != lexicon_text:
                    value = mfa_numeric_text(value)
                return module.tokenize(value)

            def source_tokens(value, point, offset):
                if candidate["text"] != lexicon_text:
                    value = mfa_numeric_text(value, context=point["narration"], offset=offset,
                                             natural_years=year_styles[point["id"]])
                value = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
                return module.tokenize(value)

            words, points = map_source_words(script["points"], aligned, normalized_tokens, source_tokens)
            prior = 0
            for word in words:
                if word["start"] < prior - 1e-6 or not 0 <= word["start"] <= word["end"] <= info["duration_seconds"] + .001:
                    raise ValueError("MFA output has invalid word bounds")
                prior = word["end"]
        except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
            last_error = error
            history.append({**detail, "status": "failed", "error": str(error), "directory": str(directory)})
            write(legacy / "alignment-attempts.json", history)
            store.event(name, "attempt_failed", {**detail, "error": str(error)})
            continue
        history.append({**detail, "status": "passed", "directory": str(directory)})
        write(legacy / "alignment-attempts.json", history)
        value = {"schema_version": "infofilm.timing.v1", "audio": str(audio), "audio_sha256": info["audio_sha256"],
                 "text_sha256": digest(text), "words": words, "points": points, "audio_info": info,
                 "provenance": {"engine": "MMS" if config.get("aligner", "mfa") == "mms" else "MFA",
                                "adapter": str(module_path), "metadata": metadata,
                                "orthographic_input": candidate["text"], "original_transcript_preserved": True,
                                "alignment_attempts": history, "had_retry": index > 1,
                                "validation": "exact source lexemes with spoken numeric expansion, MFA clitic spans and sequential bounds",
                                "human_alignment_review": "pending"}}
        write(directory / "timing.json", value)
        store.event(name, "succeeded", {"words": len(words), "points": len(points), "attempt": index, "strategy": candidate["strategy"]})
        return value
    store.event(name, "failed", {"attempts": len(history), "error": str(last_error)})
    raise AlignmentError(f"MFA alignment still needs review after {len(history)} attempts: {last_error}") from last_error


def image_identity(config, library, asset):
    # Keep the original identity stable so prompt recovery reuses accepted assets
    # and preserves every rejected take from earlier versions of the adapter.
    settings = {"asset": asset, "style": library["style"], "provider": config["image_url"],
                "width": config["image_width"], "height": config["image_height"], "steps": config["image_steps"]}
    if config.get("image_engine") in {"zimage", "ernie-turbo", "flux2", "flux2-klein"}:
        settings.update(engine=config["image_engine"], cfg_scale=config.get("image_cfg_scale", 1.0),
                        dimension_multiple=config.get("image_dimension_multiple", 64))
    prior = config.get("image_prior_attempts", {}).get(asset["id"], [])
    if prior:
        settings["prior_attempts_sha256"] = digest(prior)
    return digest(settings)


def replacement_prompt_errors(value, original_prompt):
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or not 40 <= len(prompt.strip()) <= 3200:
        return ["Need a complete scene prompt of 40–3200 characters"]
    errors = []
    if prompt.strip() == original_prompt.strip():
        errors.append("Replace the failed description to address the recorded visual defects")
    if re.search(r"\b(?:no|not|without|excluding|avoiding|do not)\b|(?:^|[.!?;,\n])\s*(?:avoid|exclude|remove)\b", prompt, re.I):
        errors.append("Use positive descriptions only: remove exclusion clauses and name only desired visible elements")
    if re.search(r"\b(?:Shared visual style|Scene illustration|Targeted correction)\s*:", prompt, re.I):
        errors.append("Return just the scene description; the adapter adds style and scene prefixes")
    return errors


def image_source_context(project, library):
    project = Path(project)
    record = read(project / "project.json") if (project / "project.json").exists() else {}
    evidence = read(project / "evidence-final.json") if (project / "evidence-final.json").exists() else {}
    brief = read(project / "creative-brief.json") if (project / "creative-brief.json").exists() else {}
    return {"subject": record.get("subject") or evidence.get("title") or library.get("subject", ""),
            "evidence_title": evidence.get("title", ""),
            "facts": [{"id": fact["id"], "claim": fact["claim"]} for fact in evidence.get("facts", [])],
            "creative_brief": {key: brief[key] for key in ["resolved_mood", "visual_style", "visual_balance", "theme"] if key in brief}}


def review_scene_image(models, library, asset, candidate, params, name, source_context, *, attempt_history=None, provider_id=None, review_stage="final"):
    from .educational_review import review_guidance, component_checks_enabled, normalize_inventory
    from .person_references import reference_review_kwargs
    reference_kwargs = reference_review_kwargs(asset)
    from .image_providers import IMAGE_REVIEW_REASONS, image_review_errors
    prompt = review_guidance(asset) + (
        "Inspect this actual educational illustration against the asset specification, narrated passage and full subject/source context. "
        "Judge the intended subject, historical era, geography, clear educational meaning, composition, obvious artifacts and text-safe area. "
        "The overall subject and source facts apply even when the local narrated passage does not repeat its date or location. "
        "Reject an explicit mismatch between the generated prompt/visible scene and the sourced era or setting. "
        "An ambiguous noun must represent its intended contextual meaning, not a visually unrelated interpretation. "
        "Do not claim the illustration proves scientific facts or a precise real location. Typography is composited later; "
        "judge available banner space without requiring generated text or exact maps/labels. "
        "The composition_contract describes the actual renderer and overrides optional positional wording in the asset prompt/acceptance. "
        "Minimal-copy scenes place text below the image: a centred subject or visible detail on the left is acceptable. "
        "For standard landscape scenes, judge whether essential content will survive the left text overlay; subdued background detail is acceptable. "
        "For standard portrait scenes, judge whether essential content survives the right-biased crop. "
        "Right and right-of-centre satisfy the same framing intent; do not reject one for failing the other or demand exact coordinates. "
        "A placement rejection must identify the essential visible content and the actual overlay/crop that would obscure it. "
        "The composition contract relaxes layout preferences only. Never excuse a demonstrable era, geography or scientific mismatch "
        "as acceptable because the image is stylized or illustrative; put that mismatch in issues and set pass=false, not merely in limitations. "
        "For maps, distinguish stable physical geography from political boundaries: modern national borders cannot stand in for a historical empire's extent, "
        "and conquests outside the sourced territory cannot be invented. Abstract illustration does not waive these factual checks. "
        "For period props, reject demonstrably modern objects; do not invent an anachronism from generic tick marks or uncertain construction details alone. "
        "Reject concrete semantic errors or unusable visuals, not optional artistic refinements. "
        "For a rejection, correction must describe the desired replacement scene with precise, unambiguous objects; "
        "explicitly replace words that confused the generator with unambiguous synonyms "
        "(for example, 'document stamp' or 'wax imprint' for a legal 'seal'). "
        "Keep corrections within the sourced era and place; never invent a different century or region. "
        "Preserve elements already correct and simplify only failing optional details. "
        "Do not repeat the confusing word in the corrected description or merely append a prohibition. "
        "Reject overlaid titles, captions or invented labels even if correctly spelled; native overlays provide typography. "
        "Review the attempt history before diagnosing a failure. Distinguish ambiguous or overloaded prompts from repeated "
        "rendering limitations after meaningful prompt repairs. Repetition alone does not establish provider weakness. "
        "A blank image or technical corruption needs backend diagnosis, not artistic prompt repair. "
        "Generation uses positive descriptions only. Do not blame the absence of a negative prompt or prescribe exclusion clauses; "
        "describe desired replacements, such as blank parchment, plain surfaces or a clearly positioned prop. "
        "Do not recommend a provider based on its brand; cite concrete observed defects and prior repairs. "
        "Keep cause and suggested_action consistent: a confident provider_weakness after meaningful repairs calls for escalate, "
        "prompt_weakness calls for repair_prompt, backend_failure calls for diagnose_backend, and uncertainty calls for recheck. "
        "Unwanted generated captions or figures that persist despite clear, materially repaired descriptions can be provider weakness; "
        "their presence in a single take alone is not sufficient. "
        "Use only the reason_keys defined below, with concrete visible evidence in issues. "
        "Accepted images have reason_keys:[]; rejected images need one or more distinct allowed keys. "
        "Use unwanted_text for generated overlay captions, labels and gibberish typography. "
        "Code owns recovery: unwanted_text escalates after the minimum meaningful repair attempts regardless of diagnosis or suggested_action. "
        "Record unwanted elements in issues. Return {pass:boolean, reason_keys:[allowed key], issues:[string], correction:string, limitations:[string], "
        "diagnosis:{cause:'prompt_weakness'|'provider_weakness'|'backend_failure'|'uncertain', confidence:number from 0 to 1, "
        "evidence:[string], suggested_action:'repair_prompt'|'escalate'|'diagnose_backend'|'recheck'}}.\nREASON KEYS:\n" +
        json.dumps(IMAGE_REVIEW_REASONS) + "\nSPEC:\n" +
        json.dumps({"asset": {**asset, "prompt": params["prompt"]},
                    "scene_contexts": library.get("scene_contexts_by_asset", {}).get(asset["id"], []),
                    "composition_contract": asset_composition(library, asset),
                    "source_context": source_context, "previous_provider_attempts": attempt_history or [],
                    "current_provider": provider_id}))
    if review_stage == "preview":
        prompt = ("This is a low-resolution semantic preview, not final-quality acceptance. "
                  "Reject major wrong subjects, factual setting/meaning errors, generated overlays and unusable composition. "
                  "Do not reject small hand artifacts, fine ornamentation, texture or detail that cannot be resolved at this size. "
                  "Record those only in limitations; the full-resolution image will be reviewed separately.\n" + prompt)
    elif review_stage != "final":
        raise ValueError("Unknown image review stage")
    if reference_kwargs:
        prompt = ('First image: generated scene. Second image: sourced appearance contact card. '
                  'Judge observable appearance fidelity in the requested treatment, pose and scene lighting. '
                  'Do not infer identity from pixels. Reference labels are not intended for the scene.\n' + prompt)
    feedback = ""
    for format_attempt in range(2):
        call_name = name if not format_attempt else name + "-format-repair"
        review = models.call("reviewer", call_name, prompt + feedback, image_path=candidate, max_tokens=2500, **reference_kwargs)
        review = normalize_inventory(review)
        errors = image_review_errors(review, require_inventory=component_checks_enabled(asset), reference_available=bool(reference_kwargs))
        if not errors:
            return review
        feedback = "\nPrevious response failed the JSON contract. Reinspect this same image and return corrected JSON. Errors:\n" + json.dumps(errors)
    raise ValueError("Image review failed its strict reason-key contract: " + "; ".join(errors))


def repair_image_prompt(models, library, asset, root, attempt, source_context=None):
    history = list((source_context or {}).get("previous_provider_attempts", [])) + [{"attempt": index + 1, "settings": read(root / f"provider-{index}.json").get("params", {})
                if (root / f"provider-{index}.json").exists() else {},
                "review": read(root / f"review-{index}.json")} for index in range(attempt)]

    def validate(value):
        return replacement_prompt_errors(value, asset["prompt"])

    result = models.loop(f"image-prompt-repair-{asset['id']}",
        "Rewrite the entire image description after an actual generated image failed visual review. "
        "Return {prompt:string, simplifications:[string]}: one complete, standalone replacement scene description, "
        "not an appended correction, and a brief list of optional details removed to make the image reliable. "
        "Return only the scene wording; the adapter supplies Shared visual style and Scene illustration prefixes. "
        "Use the narrated context, purpose and acceptance criteria to preserve the intended meaning. "
        "Resolve ambiguous nouns with unambiguous synonyms and describe the intended visible object precisely: "
        "for example, use 'document stamp' or 'red wax imprint' for a legal 'seal', rather than repeating the confusing word. "
        "Describe desired objects positively; do not mention an unwanted object or repeat the mistaken interpretation in the generation prompt. "
        "Do not include exclusion clauses such as 'No animals' or 'without lettering'. Instead describe desired surfaces as blank or plain. "
        "The prompt validator rejects 'no', 'not' and 'without'; describe banner areas as clear, empty space rather than listing excluded objects. "
        "Use all prior review feedback, giving the latest observed failure priority. Simplify the composition without changing the subject. "
        "The narrated explanation and asset PURPOSE determine essential content; details from the failed prompt are optional presentation choices. "
        "The full subject and source facts govern dates, historical era and geography, even when the local passage does not repeat them. "
        "Do not introduce unsupported dates, costumes or locations. Preserve already-correct elements while resolving new defects. "
        "Do not blindly copy a reviewer's correction when it retains a detail responsible for repeated failures. "
        "Remove optional decorations that keep causing failures instead of repeating them. If a decorative map produces invented labels, "
        "omit the map entirely and describe a plain period setting; do not keep asking for an accurate map with label prohibitions. "
        "Retain a map only when geographic relationships themselves are essential to the exact narrated passage. "
        "For documentary imagery with failed generated writing, consider folded blank parchment and closed unmarked ledgers. "
        "Preserve the shared mood, visual style and space for composited banners. Native overlays supply exact typography, labels and factual values; "
        "Follow the supplied composition_contract: group essential content in the right half for standard left-text scenes, "
        "using an appropriate camera angle or distance for wide objects. Describe the low-detail background concretely. "
        "Minimal-copy scenes use full-frame imagery with text below, so centring the subject and natural detail on the left are acceptable. "
        "Prior layout objections that contradict this contract are optional preferences; preserve the scene meaning and repair real visibility problems. "
        "the image should provide clear contextual imagery without invented lettering or factual claims. "
        "The generator API has no negative_prompt field, so do not rely on negative conditioning." +
        (" The people_in_frame cast and sourced appearance references are binding: retain the named people "
         "and simplify surrounding staging rather than substituting an invented face or deleting them. "
         "The adapter preserves their reference card." if asset.get('people_in_frame') else ''),
        {"asset": asset, "style": library["style"],
         "composition_contract": asset_composition(library, asset),
         "scene_contexts": library.get("scene_contexts_by_asset", {}).get(asset["id"], []), "previous_attempts": history,
         "source_context": {key: value for key, value in source_context.items() if key != "previous_provider_attempts"}
                           if source_context is not None else image_source_context(root.parent.parent, library)},
        validate, experimental=False)
    write(root / f"prompt-repair-{attempt}.json", {"policy": "replace-scene-description-v1", **result})
    return result["prompt"].strip()


def generate_images(config, store, library, models, *, on_progress=None):
    if type(config.get('image_review', True)) is not bool:
        raise ValueError('image_review must be boolean')
    if not config.get('image_review', True):
        from .image_direct import generate_unreviewed_images
        return generate_unreviewed_images(config, store, library, on_progress=on_progress)
    if config.get("image_recovery_mode") == "message_v1":
        from .image_recovery import generate_message_images
        return generate_message_images(config, store, library, models, on_progress=on_progress)
    if config.get("image_recovery_mode"):
        raise ValueError("Unknown image recovery mode")
    if config.get("image_provider_registry") and not config.get("image_engine"):
        from .image_providers import generate_with_providers
        return generate_with_providers(config, store, library, models, on_progress=on_progress)
    return generate_images_single(config, store, library, models, on_progress=on_progress)


def generate_images_single(config, store, library, models, *, on_progress=None):
    from .image_studio import ENGINES, park_idle_gpu_services, release_qwen, render_studio
    studio = config.get("image_engine") in ENGINES
    directory = store.root / "images"
    directory.mkdir(parents=True, exist_ok=True)
    output = dict(config.get("image_manifest_seed", {}))
    max_attempts = config.get("image_max_attempts", 5)
    if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
        raise ValueError("image_max_attempts must be an integer between 1 and 5")
    pending = []
    source_context = image_source_context(store.root, library)
    for asset in library["assets"]:
        identity = image_identity(config, library, asset)
        root = directory / identity[:20]
        selected = root / "selected.json"
        if selected.exists():
            value = read(selected)
            path = Path(value["file"])
            if (value.get("asset") != asset["id"] or value.get("review", {}).get("pass") is not True
                    or not path.is_file() or digest(path.read_bytes()) != value.get("sha256")):
                raise ValueError(f"Accepted image checkpoint is invalid: {selected}")
            output[asset["id"]] = value
        else:
            pending.append((asset, identity, root))
    write(store.root / "image_manifest.json", output)
    planned_total = config.get("image_planned_total", len(library["assets"]))
    store.event("images", "planned", {"planned_images": planned_total, "reused_images": len(output),
                                      "remaining_images": len(pending), "max_attempts": max_attempts})
    if not pending:
        return output
    with gpu_lease(config, "images"), contextlib.ExitStack() as services:
        if studio and config.get("image_park_idle_services"):
            services.enter_context(park_idle_gpu_services(config, store))
        else:
            from .gpu_release import release_vibevoice
            release_vibevoice(config, store, request_fn=request)
            if studio:
                release_qwen(config)
        if not studio:
            request(config, "image", "POST", "/api/load")
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                state = request(config, "image", "GET", "/api/status")
                if state["state"] == "loaded":
                    break
                if state["state"] == "error":
                    raise RuntimeError(f"Image model load failed: {state}")
                time.sleep(2)
            else:
                raise TimeoutError("Image model is still loading")
        try:
            for asset, identity, root in pending:
                selected = root / "selected.json"
                for attempt in range(max_attempts):
                    candidate = root / f"candidate-{attempt}.png"
                    provider = root / f"provider-{attempt}.json"
                    detail = {"attempt": attempt + 1, "max_attempts": max_attempts}
                    if config.get("image_provider_id"):
                        detail.update(provider=config["image_provider_id"], tier=config["image_tier"],
                                      total_attempts=len(config.get("image_prior_attempts", {}).get(asset["id"], [])) + attempt + 1)
                    if provider.exists():
                        # A submitted job must keep its actual settings, even if
                        # it used the old append-only correction strategy.
                        record = read(provider)
                        if not studio and not record.get("job_id") and not candidate.exists():
                            raise RuntimeError(f"Unknown image submission outcome: {provider}; reconcile before resubmitting")
                        params = record["params"]
                    else:
                        scene_prompt = asset["prompt"]
                        prior = config.get("image_prior_attempts", {}).get(asset["id"], [])
                        if not attempt and prior:
                            scene_prompt = repair_image_prompt(models, library, asset, root, 0,
                                {**source_context, "previous_provider_attempts": prior})
                        if attempt and not candidate.exists():
                            previous = read(root / f"review-{attempt - 1}.json")
                            store.event(asset["id"], "retrying", {**detail, "strategy": "replace_scene_description",
                                "issues": previous.get("issues", []), "correction": previous.get("correction", "")})
                            scene_prompt = repair_image_prompt(models, library, asset, root, attempt,
                                {**source_context, "previous_provider_attempts": prior})
                            store.event(asset["id"], "prompt_repaired", {**detail, "prompt": scene_prompt,
                                "strategy": "replace_scene_description"})
                        elif attempt:
                            # Preserve attribution for a legacy downloaded take
                            # whose submission checkpoint is no longer present.
                            scene_prompt += "\nTargeted correction: " + read(root / "review-0.json").get("correction", "Address the recorded visual defects.")
                        subject = source_context.get("subject", "")
                        params = {"prompt": "Composition: " + asset_composition(library, asset)['generation_direction'] +
                                  "\nShared visual style: " + library["style"] +
                                  ("\nSubject context, governing era and setting: " + subject if subject else "") +
                                  "\nScene illustration: " + scene_prompt,
                                  "width": str(config["image_width"]), "height": str(config["image_height"]),
                                  "steps": str(config["image_steps"]), "guidance": "0",
                                  "seed": str(int(identity[:7], 16) + attempt), "output_format": "png"}
                        from .person_references import bind_reference_params, reference_addendum
                        params['prompt'] += reference_addendum(asset)
                        params = bind_reference_params(params, asset)
                        if studio:
                            # Match initial seeds across engines, irrespective of provider identity.
                            params["seed"] = str(int(digest({"asset": asset, "style": library["style"]})[:7], 16) + attempt)
                    if not candidate.exists():
                        if studio:
                            render_studio(config, store, root, attempt, params)
                        else:
                            if not provider.exists():
                                write(provider, {"state": "submission_started", "params": params, "asset": asset["id"]})
                                from .person_references import submit_qwen_job
                                response = submit_qwen_job(config, params, request)
                                write(provider, {"state": "submitted", "params": params, "asset": asset["id"], "job_id": response["id"]})
                            record = read(provider)
                            if not record.get("job_id"):
                                raise RuntimeError(f"Unknown image submission outcome: {provider}; reconcile before resubmitting")
                            from .image_jobs import wait_qwen_job
                            job = wait_qwen_job(config, store, provider, asset["id"], request_fn=request, wait_fn=wait_job)
                            with httpx.Client(timeout=180) as client:
                                response = client.get(config["image_url"] + job["url"])
                                response.raise_for_status()
                                candidate.write_bytes(response.content)
                            write(root / f"provider-result-{attempt}.json", job)
                    review_path = root / f"review-{attempt}.json"
                    cached_review = review_path.exists()
                    if cached_review:
                        review = read(review_path)
                    else:
                        previous_attempts = config.get("image_prior_attempts", {}).get(asset["id"], []) + [
                            {"provider": config.get("image_provider_id", config.get("image_engine", "qwen")),
                             "attempt": index + 1,
                             "settings": read(root / f"provider-{index}.json").get("params", {}),
                             "review": read(root / f"review-{index}.json")} for index in range(attempt)]
                        if config.get("image_provider_id"):
                            from .image_providers import check_candidate
                            check_candidate(candidate, store, asset["id"])
                        review = review_scene_image(models, library, asset, candidate, params,
                            f"image-{asset['id']}-{identity[:8]}-{attempt}",
                            source_context, attempt_history=previous_attempts,
                            provider_id=config.get("image_provider_id", config.get("image_engine", "qwen")))
                        write(review_path, review)
                    if review.get("pass") is True:
                        value = {"asset": asset["id"], "file": str(candidate), "sha256": digest(candidate.read_bytes()),
                                 "provider": config["image_engine"] if studio else "Qwen Image 2.1", "review": review, "prompt": params["prompt"], "settings": params,
                                 "source_context_sha256": digest(source_context)}
                        if config.get("image_provider_id"):
                            value.update(provider_id=config["image_provider_id"], tier=config["image_tier"],
                                         tier_attempt=attempt + 1,
                                         total_attempts=len(config.get("image_prior_attempts", {}).get(asset["id"], [])) + attempt + 1)
                        write(selected, value)
                        output[asset["id"]] = value
                        write(store.root / "image_manifest.json", output)
                        store.event(asset["id"], "accepted", {**detail, "completed_images": len(output),
                            "planned_images": planned_total, "cached_review": cached_review})
                        if on_progress:
                            on_progress()
                        break
                    store.event(asset["id"], "rejected", {**detail, "issues": review.get("issues", []),
                        "reason_keys": review.get("reason_keys", []),
                        "correction": review.get("correction", ""), "cached_review": cached_review})
                    if on_progress:
                        on_progress()
                    if config.get("image_provider_id"):
                        from .image_providers import escalation_decision
                        history = previous_attempts if not cached_review else config.get("image_prior_attempts", {}).get(asset["id"], []) + [
                            {"settings": read(root / f"provider-{index}.json").get("params", {}),
                             "review": read(root / f"review-{index}.json")} for index in range(attempt)]
                        gate_config = {**config, "image_prior_count": len(config.get("image_prior_attempts", {}).get(asset["id"], []))}
                        action = escalation_decision(review, history + [{"settings": params, "review": review}], gate_config)
                        if action == "diagnose_backend" or (action == "escalate" and config.get("image_has_next_tier")):
                            store.event(asset["id"], action + "_recommended", {**detail, "diagnosis": review.get("diagnosis"),
                                "reason_keys": review.get("reason_keys", []),
                                "routing_basis": "unwanted_text_rule" if action == "escalate" and "unwanted_text" in review.get("reason_keys", []) else "review_diagnosis"})
                            if action == "diagnose_backend":
                                raise RuntimeError(f"Asset {asset['id']} needs backend diagnosis; see {review_path}")
                            break
                else:
                    store.event(asset["id"], "exhausted", {"attempts": max_attempts, "issues": review.get("issues", [])})
                    if not config.get("image_continue_on_exhaustion", False):
                        raise RuntimeError(f"Asset {asset['id']} did not pass bounded visual review after {max_attempts} attempts")
        finally:
            request(config, "image", "POST", "/api/unload")
    write(store.root / "image_manifest.json", output)
    return output
