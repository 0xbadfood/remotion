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

AUDIT_POLICY = "english-completeness-equivalent-years-v6"
DEFAULT_FALLBACK_MODELS = ("Systran/faster-whisper-small", "openai/whisper-large-v3-turbo")


def script_differences(audit):
    """The normalized spans where the transcript departs from the script: the failure's fingerprint."""
    expected, observed = audit["expected_text"].split(), audit["transcript_text"].split()
    return [{"id": f"D{n+1:02d}", "expected": " ".join(expected[i:j]), "observed": " ".join(observed[k:l]), "operation": tag}
            for n, (tag, i, j, k, l) in enumerate(
                x for x in SequenceMatcher(None, expected, observed, autojunk=False).get_opcodes() if x[0] != "equal")]


def same_failure(audit, earlier):
    """Two independent takes that fail with the same departures from the script: Whisper is writing the same speech
    the same way (a likely false positive), not the voice misspeaking twice."""
    if not audit.get("transcript_text") or not earlier.get("transcript_text") or not audit.get("expected_text"):
        return False
    # Shared departures, not identical lists: names of foreign words vary take to take (kempelin/campellan) while
    # the real cause (Joseph II written for "the second") repeats. The arbiter still reviews every difference.
    strip = lambda diffs: {(d["expected"], d["observed"], d["operation"]) for d in diffs}
    return bool(strip(script_differences(audit)) & strip(script_differences(earlier)))


def audit_with_escalation(config, store, audio, text, points=None, earlier_audits=None):
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
    earlier = [a for a in earlier_audits or [] if a.get("status") == "failed"]
    if (selected["status"] != "passed" and earlier and selected.get("transcript_words") != 0
            and config.get("audio_audit_llm_review", True) and same_failure(selected, earlier[-1])):
        store.event("audio-audit", "repeated_failure_arbiter", {"audio": str(audio), "reasons": selected["reasons"],
                    "differences": script_differences(selected)})
        reviewed = adjudicate_transcript(config, store, audio, text, selected, repeated=earlier[-1])
        if reviewed is not None:
            selected = reviewed
    result = {**selected, "recognizer_attempts": history,
              "expected_prompt_supplied": False, "audit_device": "cpu"}
    if result["status"] == "passed" and len(history) > 1:
        store.event("audio-audit", "crosscheck_passed", {
            "audio": str(audio), "model": result["model"], "score": result["score"],
            "recognizers": len(history), "thresholds_relaxed": False,
            "acceptance_method": result.get("acceptance_method", "asr_thresholds")})
    write(Path(audio).parent / "audit.json", result)
    return result


ROMAN = re.compile(r"m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})", re.I)
ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def roman_value(text):
    """Integer value of a Roman numeral ('IV' -> 4), else None."""
    token = str(text).strip().lower()
    if not token or not ROMAN.fullmatch(token):
        return None
    total = 0
    for here, after in zip(token, token[1:] + " "):
        value = ROMAN_VALUES[here]
        total += -value if ROMAN_VALUES.get(after, 0) > value else value
    return total


ORDINAL_CARDINAL = {"first": "one", "second": "two", "third": "three", "fourth": "four", "fifth": "five",
                    "sixth": "six", "seventh": "seven", "eighth": "eight", "ninth": "nine", "tenth": "ten",
                    "eleventh": "eleven", "twelfth": "twelve", "thirteenth": "thirteen", "fourteenth": "fourteen",
                    "fifteenth": "fifteen", "sixteenth": "sixteen", "seventeenth": "seventeen",
                    "eighteenth": "eighteen", "nineteenth": "nineteen", "twentieth": "twenty"}


def same_number(difference):
    """Code check for a number written differently by ASR: both sides spell the same characters once numbers are words
    ('p nine' vs 'P9', 'u two hundred thirty five' vs 'U235', 'Phase IV' vs 'four'). A changed value never passes."""
    def compact(value):
        value = re.sub(r"^the\s+", "", str(value).strip(), flags=re.I)     # regnal reading: "the second" = "II"
        roman = roman_value(value)
        value = re.sub(r"(\d)(?=[a-z])(?!(?:st|nd|rd|th)\b)", r"\1 ", value, flags=re.I)   # "2y" = "two y"
        words = canonical_numeric_text(str(roman) if roman else value).lower().split()
        return re.sub(r"[^a-z]", "", "".join(ORDINAL_CARDINAL.get(w, w) for w in words))
    return bool(compact(difference["expected"])) and compact(difference["expected"]) == compact(difference["observed"])


def adjudicate_transcript(config, store, audio, text, audit, repeated=None):
    """Let the LLM distinguish ASR rendering errors from changed content. Normally only name-type disagreements; for a
    repeated identical failure (`repeated` is the earlier take's audit) any reason, with numbers checked by code."""
    if not audit.get("reasons"):
        return None
    if repeated is None and set(audit["reasons"]) - {"text_mismatch", "low_coverage", "point_coverage"}:
        return None
    if not config.get("reviewer_url"):
        return None
    from .core import digest, write
    from .llm import Models

    differences = script_differences(audit)
    if not differences:
        return None
    payload = {"script": text, "raw_transcript": audit.get("raw_transcript", audit["transcript_text"]),
               "normalized_differences": differences}
    if repeated is not None:
        payload["repeated_failure"] = {
            "note": "A second, independently generated take failed with exactly these differences; the earlier take's "
                    "transcript is below. Identical departures across takes usually mean the recognizer writes the same "
                    "correct speech differently from the script (number formatting, joined designations, names).",
            "earlier_raw_transcript": repeated.get("raw_transcript", repeated.get("transcript_text")),
            "earlier_reasons": repeated.get("reasons")}
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
        "'number_format'|'meaning_change'|'missing_content'|'uncertain',explanation:string}],reason:string}. "
        "number_format means the same number written differently (digits joined to letters, digits for words); "
        "code verifies it, so a changed value must be meaning_change. "
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
    allowed = {"non_english_name_variant", "asr_spelling", "word_boundary", "harmless_disfluency", "number_format"}
    valid = (isinstance(items, list) and len(items) == len(ids)
             and all(isinstance(i, dict) for i in items)
             and sorted(str(i.get("id")) for i in items) == sorted(ids))
    by_id = {d["id"]: d for d in differences}
    numbers_checked = valid and all(
        same_number(by_id[str(i.get("id"))]) for i in items
        if i.get("kind") == "number_format" or _NUMBER_WORDS.intersection(
            (by_id[str(i.get("id"))]["expected"] + " " + by_id[str(i.get("id"))]["observed"]).split()))
    accepted = (valid and numbers_checked and review.get("decision") == "accept" and review.get("meaning_preserved") is True
                and review.get("complete") is True and all(i.get("kind") in allowed for i in items))
    write(store.root / "audio-semantic-reviews" / (identity + ".json"),
          {"audio": str(audio), "input": payload, "asr_audit": audit, "review": review,
           "accepted": accepted, "review_contract_valid": valid, "audio_listened": False})
    store.event("audio-audit", "semantic_accepted" if accepted else "semantic_needs_review", {
        "audio": str(audio), "decision": review.get("decision"), "accepted": accepted,
        "differences": items, "reason": review.get("reason"), "original_score": audit["score"]})
    return {**audit, "status": "passed" if accepted else "failed", "reasons": [] if accepted else audit["reasons"],
            "acceptance_method": ("arbiter_repeated_failure" if repeated is not None else "llm_transcript_adjudication")
                                 if accepted else "asr_failed", "numbers_code_checked": numbers_checked,
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
    # ASR joins designations the narration speaks apart: 'P9', 'U235', 'B29' for 'P nine', 'U two hundred thirty five'.
    value = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", value)
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


# Words that join or sign a number but are not one: "Br minus" (Br-) and "plus or minus" are not quantities.
_CONNECTIVES = {"point", "percent", "minus", "plus"}


def quantity(words, start, end):
    """Whether words[start:end] holds a quantity: a numeral word, or a connective (minus, point...) next to one. The
    alkenes film failed its whole-narration check when "the bromide ion Br minus" was heard as "bcar"."""
    region = words[start:end]
    if _NUMBER_WORDS.difference(_CONNECTIVES).intersection(region):
        return True
    if not _CONNECTIVES.intersection(region):
        return False
    numerals = _NUMBER_WORDS - _CONNECTIVES
    return any(0 <= n < len(words) and words[n] in numerals for n in (start - 1, end))


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
        # A word split ('firsthand' / 'first hand') or the same value written another way ('the second' / 'ii')
        # is not a changed quantity.
        if (tag != "equal" and (quantity(expected, i, j) or quantity(actual, k, l))
                and "".join(expected[i:j]) != "".join(actual[k:l])
                and not same_number({"expected": " ".join(expected[i:j]), "observed": " ".join(actual[k:l])})):
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
    from .numeric_speech import canonical_year_pairs
    value = canonical_year_pairs(str(text or ""))
    value = re.sub(r"\ba\s+(hundred|thousand|million|billion|trillion)\b", r"one \1",
                   spoken_numeric_text(value), flags=re.I)
    return re.sub(r"\b(hundred|thousand|million|billion)\s+and\s+(?=(?:" +
                  "|".join(sorted(_NUMBER_WORDS)) + r")\b)", r"\1 ", value, flags=re.I)


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
        "normalization": "english-spoken-years-v2",
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
