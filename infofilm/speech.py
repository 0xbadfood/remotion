"""Pre-voice conditioning: the spoken form of a chapter's narration (user design, 2026-10-13).

Before VibeVoice renders a chapter, code and one LLM call decide how each hard token should be said: "Phase IV" ->
"Phase Four", "NTCA" -> "N T C A", "St." -> "Saint". The result is a lexicon {written token: spoken words}:
- the voice and the aligner receive the spoken form;
- the ASR audit compares against it where the recognizer would write it that way (symbols, abbreviations), and
  against the written token for capitalised words (acronyms, names, Roman numerals), which Whisper writes as written;
- captions and banners keep the written text.

Code rules come first and win (Roman numerals after a counter or a regnal name); the LLM covers what rules cannot
decide (acronym read as a word or as letters, unusual abbreviations and symbols). Names are not respelled.
"""
from __future__ import annotations

import json
import re

from .audio_audit import roman_value
from .core import digest, read, write

COUNTERS = {"phase", "war", "part", "act", "chapter", "book", "class", "category", "type", "grade", "stage", "level",
            "volume", "article", "title", "schedule", "section", "tier", "round", "mark", "model", "apollo", "vatican",
            "council", "amendment", "plan"}
WORD_ACRONYMS = {"NASA", "UNESCO", "NATO", "UNICEF", "OPEC", "ASEAN", "SEATO", "LASER", "RADAR", "SONAR", "AIDS",
                 "COVID", "SARS", "WHO", "NAFTA", "INTERPOL", "UNHCR"}
EDGE = "\"'“”‘’()[]{},;:!?"
TOKEN = re.compile(r"\S+")
SPOKEN = re.compile(r"[A-Za-z][A-Za-z '\-]{0,80}")
ORDINALS = ["", "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth",
            "Eleventh", "Twelfth", "Thirteenth", "Fourteenth", "Fifteenth", "Sixteenth", "Seventeenth", "Eighteenth",
            "Nineteenth", "Twentieth"]

TASK = (
    "You prepare narration for a text-to-speech voice. List only the tokens in the narration below that a voice is "
    "likely to read differently from what the writer intends, or that a listener would hear differently from the "
    "written text: acronyms (decide whether each is said as a word, like UNESCO or NASA, or letter by letter, like "
    "N T C A), abbreviations (St., Dr., No., km, approx.), symbols (%, &, /), and unusual numerals or codes. "
    "Do not list ordinary words, plain numbers, years or dates (code handles those), and do not respell names. "
    "spoken is the plain words a careful news reader would say, letters separated by spaces when spelled out, with no "
    "digits. Return {entries:[{written: the token exactly as it appears, spoken: string}]}; an empty list is fine.")


def core(token):
    """The token without surrounding punctuation, keeping an abbreviation's dot ("St." stays "St.")."""
    return token.strip(EDGE)


def candidates(token):
    word = core(token)
    return [word, word.rstrip(".")] if word.endswith(".") else [word]


def code_lexicon(points):
    """Deterministic entries: Roman numerals after a counter (Phase IV -> Four) or a regnal name (Henry VIII ->
    the Eighth), and letter acronyms (spelled out unless known as a word)."""
    entries = {}
    for point in points:
        words = [core(t) for t in TOKEN.findall(point.get("narration", ""))]
        for index, word in enumerate(words):
            bare = word.rstrip(".")
            before = words[index - 1].rstrip(".,") if index else ""
            # I, V and X only: "Type C" or "Section D" are letters, not 100 and 500.
            value = roman_value(bare) if re.fullmatch(r"[IVX]+", bare) else None
            if value and before.lower() in COUNTERS:
                from num2words import num2words
                entries[bare] = num2words(value).replace("-", " ").title()
            elif value and len(bare) >= 2 and before[:1].isupper() and value <= 20:
                # Regnal numbers (Henry VIII, Louis XIV); a lone letter is left to the LLM ("Malcolm X").
                entries.setdefault(bare, "the " + ORDINALS[value])
            elif re.fullmatch(r"[A-Z]{2,6}", bare) and bare not in WORD_ACRONYMS and not value:
                entries.setdefault(bare, " ".join(bare))
    return entries


def valid(entries, text):
    """Keep entries whose written token occurs in the text and whose spoken form is plain words."""
    present = {c for token in TOKEN.findall(text) for c in candidates(token)}
    kept = {}
    for written, spoken in entries.items():
        if (isinstance(written, str) and isinstance(spoken, str) and written in present and spoken.strip()
                and SPOKEN.fullmatch(spoken.strip()) and len(spoken.split()) <= 10 and spoken.strip() != written):
            kept[written] = spoken.strip()
    return kept


def lexicon(config, store, points):
    """The chapter's lexicon, cached by its narration. Code entries for Roman numerals win; the LLM may override an
    acronym decision (word or letters) and add abbreviations and symbols."""
    text = "\n".join(p.get("narration", "") for p in points)
    path = store.root / "speech" / f"{digest({'text': text, 'policy': 'speech-v2'})[:16]}.json"
    if path.exists():
        return read(path)["lexicon"]
    rules = valid(code_lexicon(points), text)
    learned, error = {}, None
    if config.get("speech_conditioning_llm", True) and config.get("planner_url"):
        from .llm import Models
        try:
            numerals = {k: v for k, v in rules.items() if roman_value(k)}
            guesses = {k: v for k, v in rules.items() if k not in numerals}
            answer = Models(config, store).call("planner", "speech-conditioning",
                TASK + "\nNUMERALS (decided by code; do not list):\n" + json.dumps(numerals) +
                "\nACRONYM GUESSES (code spells every capitalised token letter by letter; correct each one said as a "
                "word, e.g. DISHA said Disha, by returning it with spoken set to the word):\n" + json.dumps(guesses) +
                "\nNARRATION:\n" + text, max_tokens=2500)
            learned = valid({e.get("written"): e.get("spoken") for e in answer.get("entries") or []
                             if isinstance(e, dict)}, text)
        except Exception as exc:          # advisory: the voice still gets the code entries
            error = str(exc)[:300]
    merged = {**rules, **{k: v for k, v in learned.items() if not roman_value(k.rstrip("."))}}
    write(path, {"lexicon": merged, "code": rules, "llm": learned, **({"llm_error": error} if error else {})})
    store.event("speech-conditioning", "succeeded", {"entries": len(merged), "code": len(rules), "llm": len(learned),
                                                     **({"llm_error": error} if error else {})})
    return merged


def apply(text, entries, *, audit=False):
    """Text with each lexicon token replaced by its spoken form. audit=True keeps capitalised tokens (acronyms,
    names, Roman numerals: Whisper writes "Joseph II") as written, because the recognizer writes them that way, and the
    number check accepts "Phase 4" for "Phase IV"; lowercase abbreviations (km) use the spoken form."""
    if not entries:
        return text

    def swap(match):
        token = match.group()
        for written in candidates(token):
            spoken = entries.get(written)
            if spoken is None:
                continue
            if audit and re.fullmatch(r"[A-Z][A-Za-z]*", written):
                return token
            start = token.find(written)
            return token[:start] + spoken + token[start + len(written):]
        return token
    return TOKEN.sub(swap, text)


def with_speech(config, store, points):
    """Points carrying the chapter lexicon (point["speech"]) for the voice, the audit and the aligner."""
    entries = lexicon(config, store, points)
    return [{**p, "speech": entries} if entries else p for p in points]
