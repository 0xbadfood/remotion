"""Local, format-aware scoring of the existing Whisper transcript cache."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from num2words import num2words

from .core import read

AUDIT_POLICY = "english-completeness-equivalent-quantities-v5"
DEFAULT_FALLBACK_MODELS = ("Systran/faster-whisper-small", "openai/whisper-large-v3-turbo")


def audit_with_escalation(config, store, audio, text, points=None):
    """Adjudicate ASR disagreement before requesting another speech take."""
    from quiz_harness.audio_audit import WhisperAuditClient, DEFAULT_WHISPER_MODEL
    from .core import write

    fallback = config.get("audio_audit_fallback_models", list(DEFAULT_FALLBACK_MODELS))
    if not isinstance(config.get("audio_audit_llm_review", True), bool):
        raise ValueError("audio_audit_llm_review must be boolean")
    if not isinstance(fallback, list) or len(fallback) > 2 or any(
            model not in DEFAULT_FALLBACK_MODELS for model in fallback):
        raise ValueError("audio_audit_fallback_models must contain up to two supported CPU models")
    history = []
    selected = None
    models = list(dict.fromkeys([DEFAULT_WHISPER_MODEL, *fallback]))
    for model in models:
        try:
            client = WhisperAuditClient
            if model == "openai/whisper-large-v3-turbo":
                client = _large_audit_client(WhisperAuditClient)
            with client(model=model, cache_root=store.root / "audio_audit_cache",
                        device="cpu", compute_type="int8") as auditor:
                result = audit_english_audio(auditor, audio, text, points=points)
        except Exception as exc:
            if selected is None:
                raise
            history.append({"model": model, "device": "cpu", "error": str(exc)})
            store.event("audio-audit", "fallback_failed", {"audio": str(audio), "model": model, "error": str(exc)})
            continue
        history.append(result)
        selected = result
        if result["status"] == "passed":
            break
        if result.get("transcript_words") == 0:
            # Empty speech cannot be adjudicated from names; use the normal
            # bounded speech retry rather than loading more recognizers.
            break
        if config.get("audio_audit_llm_review", True):
            reviewed = adjudicate_transcript(config, store, audio, text, result)
            if reviewed is not None:
                selected = reviewed
                if reviewed["status"] == "passed":
                    break
        store.event("audio-audit", "needs_crosscheck" if model != models[-1] else "failed", {
            "audio": str(audio), "model": model, "score": result["score"], "reasons": result["reasons"]})
    assert selected is not None
    result = {**selected, "recognizer_attempts": history,
              "expected_prompt_supplied": False, "audit_device": "cpu"}
    if result["status"] == "passed" and len(history) > 1:
        store.event("audio-audit", "crosscheck_passed", {
            "audio": str(audio), "model": result["model"], "score": result["score"],
            "recognizers": len(history), "thresholds_relaxed": False,
            "acceptance_method": result.get("acceptance_method", "asr_thresholds")})
    write(Path(audio).parent / "audit.json", result)
    return result


def adjudicate_transcript(config, store, audio, text, audit):
    """Let the LLM distinguish foreign-name ASR errors from changed content."""
    if not audit.get("reasons") or set(audit["reasons"]) - {"text_mismatch", "low_coverage", "point_coverage"}:
        return None
    if not config.get("reviewer_url"):
        return None
    from .core import digest, write
    from .llm import Models

    expected = audit["expected_text"].split()
    observed = audit["transcript_text"].split()
    differences = [{"id": f"D{n+1:02d}", "expected": " ".join(expected[i:j]),
                    "observed": " ".join(observed[k:l]), "operation": tag}
                   for n, (tag, i, j, k, l) in enumerate(
                       x for x in SequenceMatcher(None, expected, observed, autojunk=False).get_opcodes()
                       if x[0] != "equal")]
    if not differences:
        return None
    payload = {"script": text, "raw_transcript": audit.get("raw_transcript", audit["transcript_text"]),
               "normalized_differences": differences}
    prompt = (
        "Adjudicate an English narration ASR disagreement. You receive the approved script and an unprompted "
        "Whisper transcript, NOT the audio. Short clips exaggerate word error percentages: a non-English proper noun "
        "can occupy several mistaken tokens. Decide whether all intended content is represented, whether this is "
        "a likely ASR rendering of the same names, or whether another speech take is needed. "
        "Accept plausible phonetic/transliteration variants of the SAME non-English person, dynasty or place "
        "(e.g. Thanjavur/Tanjavur), word-boundary spelling, contraction normalization and harmless disfluency. "
        "Use asr_spelling only for a spelling of the same spoken word, not for replacing a claim or changing an entity. "
        "Do not demand English dictionary spellings for non-English names. A different person/place, an omitted "
        "claim, changed negation, changed relationship or unrelated speech must fail. If a name is transcribed "
        "as an unrelated English phrase and phonetic equivalence is unclear, choose uncertain for stronger ASR. "
        "A malformed transcript of a foreign name is not by itself evidence that the speech omitted that name. "
        "The normalized diff may expand possessive 's as 'is'; use the raw script and raw transcript to understand it. "
        "Review EVERY supplied difference by its exact id. Do not claim to have heard or verified pronunciation. "
        "Return {decision:'accept'|'retry'|'uncertain',meaning_preserved:boolean,complete:boolean,"
        "differences:[{id,kind:'non_english_name_variant'|'asr_spelling'|'word_boundary'|'harmless_disfluency'|"
        "'meaning_change'|'missing_content'|'uncertain',explanation:string}],reason:string}. "
        "Accept only when all differences are benign and both meaning_preserved and complete are true.\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    identity = digest({"policy": AUDIT_POLICY, "payload": payload, "audio": digest(Path(audio).read_bytes())})[:16]
    try:
        review = Models(config, store).call_json("reviewer", "audio-semantic-" + identity, prompt, max_tokens=2200)
    except Exception as exc:
        store.event("audio-audit", "semantic_review_failed", {"audio": str(audio), "error": str(exc)})
        return None
    items = review.get("differences")
    ids = [d["id"] for d in differences]
    allowed = {"non_english_name_variant", "asr_spelling", "word_boundary", "harmless_disfluency"}
    valid = (isinstance(items, list) and len(items) == len(ids)
             and all(isinstance(i, dict) for i in items)
             and sorted(str(i.get("id")) for i in items) == sorted(ids))
    accepted = (valid and review.get("decision") == "accept" and review.get("meaning_preserved") is True
                and review.get("complete") is True and all(i.get("kind") in allowed for i in items))
    write(store.root / "audio-semantic-reviews" / (identity + ".json"),
          {"audio": str(audio), "input": payload, "asr_audit": audit, "review": review,
           "accepted": accepted, "review_contract_valid": valid, "audio_listened": False})
    store.event("audio-audit", "semantic_accepted" if accepted else "semantic_needs_review", {
        "audio": str(audio), "decision": review.get("decision"), "accepted": accepted,
        "differences": items, "reason": review.get("reason"), "original_score": audit["score"]})
    return {**audit, "status": "passed" if accepted else "failed", "reasons": [] if accepted else audit["reasons"],
            "acceptance_method": "llm_transcript_adjudication" if accepted else "asr_failed",
            "asr_audit": audit, "semantic_review": review, "review_contract_valid": valid}


def _large_audit_client(base):
    """Keep the shared cache/scorer protocol; change only the local ASR worker."""
    import subprocess

    class LargeAuditClient(base):
        def _start(self):
            if self._process is not None and self._process.poll() is None:
                return self._process
            self._process = subprocess.Popen([
                str(self.python_path), "-m", "infofilm.large_audio_audit_worker", "--model", self.model,
                "--cache-dir", str(self.model_cache),
            ], cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
            return self._process

    return LargeAuditClient


_NUMBER = re.compile(
    r"(?<![\w.])([+-]?)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?"
    r"(st|nd|rd|th)?(?!\w)", re.I
)
_NUMBER_WORDS = set(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty "
    "thirty forty fifty sixty seventy eighty ninety hundred thousand million "
    "billion trillion first second third fourth fifth sixth seventh eighth ninth "
    "tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth "
    "eighteenth nineteenth twentieth thirtieth fortieth fiftieth sixtieth "
    "seventieth eightieth ninetieth hundredth thousandth millionth billionth "
    "point percent minus plus".split()
)


def spoken_numeric_text(text):
    """Expand notation before punctuation stripping; retain values and units."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    # ASR writes spoken ranges as 7-10, and ratios as 1.4:1.
    value = re.sub(r"(?<=\d)\s*[-–—]\s*(?=\d)", " to ", value)
    value = re.sub(r"(?<=\d)\s*:\s*(?=\d)", " to ", value)

    def expand(match):
        sign, integer, fraction, ordinal = match.groups()
        number = int(integer.replace(",", ""))
        words = num2words(number, lang="en", to="ordinal" if ordinal else "cardinal")
        if fraction is not None:
            words += " point " + " ".join(num2words(int(d), lang="en") for d in fraction)
        if sign:
            words = ("minus " if sign == "-" else "plus ") + words
        return words

    return _NUMBER.sub(expand, value).replace("%", " percent ")


def score_english_transcript(*, expected_text, transcript_text,
                             audio_duration_seconds, speech_span_seconds, config=None):
    from quiz_harness.audio_audit import score_transcript

    kwargs = {"config": config} if config is not None else {}
    result = score_transcript(
        expected_text=canonical_numeric_text(expected_text),
        transcript_text=canonical_numeric_text(transcript_text),
        audio_duration_seconds=audio_duration_seconds,
        speech_span_seconds=speech_span_seconds, **kwargs,
    )
    # Ordinary WER tolerance must not forgive a changed quantity or lost unit.
    expected = result["expected_text"].split()
    actual = result["transcript_text"].split()
    for tag, i, j, k, l in SequenceMatcher(None, expected, actual, autojunk=False).get_opcodes():
        if tag != "equal" and _NUMBER_WORDS.intersection(expected[i:j] + actual[k:l]):
            if "numeric_mismatch" not in result["reasons"]:
                result["reasons"].append("numeric_mismatch")
        # A whole omitted passage must not disappear inside the overall WER
        # allowance. Small substitutions remain ordinary ASR uncertainty.
        if tag in {"delete", "replace"} and j-i >= 4 and l-k < (j-i)/2:
            reason = "incomplete_ending" if j == len(expected) else (
                "incomplete_opening" if i == 0 else "missing_passage")
            if reason not in result["reasons"]:
                result["reasons"].append(reason)
    if result["reasons"]:
        result["status"] = "failed"
    result["audit_policy"] = AUDIT_POLICY
    return result


def canonical_numeric_text(text):
    """Scoring-only equivalence: 'a thousand' and '1,000' name the same value.

    Leave MFA orthography and the frozen script unchanged. Explicit coefficients,
    units, signs and qualifiers such as 'over' still have to match.
    """
    return re.sub(r"\ba\s+(hundred|thousand|million|billion|trillion)\b", r"one \1",
                  spoken_numeric_text(text), flags=re.I)


def point_coverage(points, transcript_text):
    """Measure each point against the unforced ASR transcript, in script order."""
    from quiz_harness.audio_audit import normalize_text
    expected, spans = [], []
    for point in points:
        tokens = normalize_text(canonical_numeric_text(point["narration"])).split()
        start = len(expected)
        expected.extend(tokens)
        spans.append((point["id"], start, len(expected)))
    actual = normalize_text(canonical_numeric_text(transcript_text)).split()
    matched = set()
    for block in SequenceMatcher(None, expected, actual, autojunk=False).get_matching_blocks():
        matched.update(range(block.a, block.a+block.size))
    return [{"point_id": point_id, "expected_words": end-start,
             "matched_words": sum(i in matched for i in range(start, end)),
             "coverage": round(sum(i in matched for i in range(start, end))/max(1, end-start), 4)}
            for point_id, start, end in spans]


def audit_english_audio(auditor, audio, text, points=None):
    """Reuse upstream ASR, then rescore its raw transcript without losing decimals."""
    original = auditor.audit(Path(audio), text)
    if auditor.language != "en":
        return original
    # WhisperAuditClient's normalized result discards decimal/range/unit syntax.
    # Its cache key is model + NUL + language + WAV bytes, independent of scoring.
    key = hashlib.sha256()
    key.update(auditor.model.encode())
    key.update(b"\0")
    key.update(auditor.language.encode())
    key.update(Path(audio).read_bytes())
    transcript = read(auditor.cache_root / f"{key.hexdigest()}.json")
    raw_text = str(transcript.get("text") or "")
    result = {
        **score_english_transcript(
            expected_text=text, transcript_text=raw_text,
            audio_duration_seconds=float(transcript.get("audio_duration_seconds") or 0),
            speech_span_seconds=float(transcript.get("speech_span_seconds") or 0),
            config=auditor.config,
        ),
        "model": auditor.model,
        "normalization": "english-spoken-numbers-v1",
        "raw_transcript": raw_text,
        "verbatim_audit": original,
    }
    if points:
        result["points"] = point_coverage(points, raw_text)
        failed = [p["point_id"] for p in result["points"] if p["coverage"] < auditor.config.coverage_threshold]
        if failed:
            result["status"] = "failed"
            result["reasons"].append("point_coverage")
            result["failed_points"] = failed
    return result
