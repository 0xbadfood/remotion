"""Evaluation suite: can a listening model (Gemma 4 12B on llama.cpp, port 8001) settle Whisper's audio failures?

Gemma gets only the audio and is asked for a verbatim transcript; it never sees the script, so it cannot simply agree.
Code compares each transcript (Whisper's and Gemma's) with the script using the production scorer. Two rules are
reported:
- gemma_alone: Gemma's transcript passes the scorer.
- two_ears: a take fails only where BOTH transcripts depart from the same script words (a difference only Whisper
  hears is a Whisper error).

Cases come from saved point takes: every failed take, every take the text arbiter accepted, and a fixed sample of
passed takes as controls. Fakes reuse control audio against a script changed by code (number, name, negation,
dropped clause): every rule must reject them.

Usage: .venv/bin/python scripts/audio_arbiter_eval.py [--controls 20] [--url http://localhost:8001/v1]
Writes artifacts/audio-arbiter-eval/{cases.json,transcripts.json,report.json}.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, json.loads((ROOT / "config.json").read_text())["quiz_root"])     # the shared ASR scorer
from infofilm.audio_audit import _NUMBER_WORDS, score_english_transcript, script_differences  # noqa: E402

OUT = ROOT / "artifacts" / "audio-arbiter-eval"
PROMPT = ("Transcribe this English narration verbatim, word for word, exactly as spoken. Write numbers, years and "
          "titles in words as they are spoken. Return only the transcript.")


SOURCES = {"spark": ROOT / "artifacts" / "runs", "aiserver": OUT / "aiserver" / "runs"}   # aiserver: copied takes


def collect(controls, sources=("spark",), every_fake=False):
    cases, passed = [], []
    paths = [p for source in sources for p in sorted(SOURCES[source].glob("**/points/*/take-*/**/audit.json"))]
    for audit_path in paths:
        audit = json.loads(audit_path.read_text())
        wav = audit_path.parent / "narration.wav"
        if not wav.is_file() or not audit.get("expected_text") or not audit.get("raw_transcript"):
            continue
        method = audit.get("acceptance_method", "asr_thresholds")
        case = {"id": hashlib.sha256(str(audit_path).encode()).hexdigest()[:12],
                "run": audit_path.parts[audit_path.parts.index("runs") + 1][:40],
                "point": audit_path.relative_to(ROOT).as_posix().split("/points/")[1].split("/")[0],
                "audio": str(wav), "expected_text": audit["expected_text"],
                "whisper_raw": audit["raw_transcript"], "whisper_status": audit["status"],
                "whisper_reasons": audit.get("reasons", []), "acceptance_method": method,
                "duration": float(audit.get("audio_duration_seconds") or 0),
                "span": float(audit.get("speech_span_seconds") or audit.get("audio_duration_seconds") or 0)}
        if audit["status"] == "failed" or method != "asr_thresholds":
            case["kind"] = "whisper_failed" if audit["status"] == "failed" else "text_arbiter_accepted"
            cases.append(case)
        else:
            passed.append({**case, "kind": "control"})
    random.Random(7).shuffle(passed)
    controls_cases = passed[:controls]
    names = sorted({w for c in controls_cases for w in re.findall(r"\b[A-Z][a-z]{3,}\b", c["whisper_raw"])})
    # every_fake: each control yields one fake of every kind that applies to it (no fallback between kinds).
    fakes = [fake for index, case in enumerate(controls_cases)
             for mode in (range(4) if every_fake else [index % 4])
             if (fake := mutate(case, mode, names, strict=every_fake)) is not None]
    return cases + controls_cases + fakes


def mutate(case, mode, names, strict=False):
    """The control's audio against a script changed by code. The change is the only true difference. strict: return
    None when this kind does not apply instead of falling back to the next kind."""
    words = case["expected_text"].split()
    rng = random.Random(f"{case['id']}-{mode}")
    first = mode
    if mode == 0:     # a changed number
        spots = [i for i, w in enumerate(words) if w in _NUMBER_WORDS and w not in {"hundred", "thousand", "point"}]
        swap = {"one": "two", "two": "three", "three": "four", "four": "five", "five": "six", "six": "seven",
                "seven": "eight", "eight": "nine", "nine": "one", "ten": "eleven", "first": "second",
                "second": "third", "third": "fourth", "eleven": "twelve", "twelve": "thirteen",
                "fifteen": "sixteen", "eighteen": "nineteen", "nineteen": "eighteen", "twenty": "thirty",
                "thirty": "forty", "forty": "fifty", "fifty": "sixty", "sixty": "seventy", "seventy": "eighty",
                "eighty": "ninety", "ninety": "seventy"}
        spots = [i for i in spots if words[i] in swap]
        if not spots:
            mode = 1
            if strict:
                return None
        else:
            i = rng.choice(spots); words[i] = swap[words[i]]; change = "number"
    if mode == 1:     # a swapped name
        present = [i for i, w in enumerate(words) if w.capitalize() in names]
        others = [n.lower() for n in names]
        if not present or len(others) < 2:
            mode = 2
            if strict:
                return None
        else:
            i = rng.choice(present); words[i] = rng.choice([n for n in others if n != words[i]]); change = "name"
    if mode == 2:     # negation
        spots = [i for i, w in enumerate(words) if w in {"was", "were", "is", "are", "did", "could", "would", "had"}]
        if not spots:
            mode = 3
            if strict:
                return None
        else:
            i = rng.choice(spots); words.insert(i + 1, "not"); change = "negation"
    if mode == 3:     # a dropped clause
        if len(words) < 16:
            return None
        i = rng.randrange(3, len(words) - 8); del words[i:i + 5]; change = "dropped_clause"
    return {**case, "id": f"{case['id']}-fake{first}", "kind": "fake", "change": change, "expected_text": " ".join(words)}


def transcribe(url, audio, cache):
    key = hashlib.sha256(Path(audio).read_bytes()).hexdigest()
    if key in cache:
        return cache[key]
    body = {"messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(Path(audio).read_bytes()).decode(),
                                                 "format": "wav"}},
        {"type": "text", "text": PROMPT}]}], "temperature": 0, "max_tokens": 3000}
    for _ in range(2):
        started = time.time()
        request = urllib.request.Request(url.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
        answer = json.load(urllib.request.urlopen(request, timeout=600))["choices"][0]["message"].get("content") or ""
        if answer.strip():
            break
    cache[key] = {"text": answer.strip(), "seconds": round(time.time() - started, 2)}
    return cache[key]


def score(case, transcript):
    return score_english_transcript(expected_text=case["expected_text"], transcript_text=transcript,
                                    audio_duration_seconds=case["duration"], speech_span_seconds=case["span"])


def departures(result):
    """Script word positions where the transcript departs (replace/delete of script words, or an insertion point)."""
    from difflib import SequenceMatcher
    expected, observed = result["expected_text"].split(), result["transcript_text"].split()
    spots = set()
    for tag, i, j, k, l in SequenceMatcher(None, expected, observed, autojunk=False).get_opcodes():
        if tag != "equal":
            spots.update(range(i, j) if j > i else [i])
    return spots


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--controls", type=int, default=20)
    parser.add_argument("--url", default="http://localhost:8001/v1")
    parser.add_argument("--sources", default="spark", help="comma list of: " + ", ".join(SOURCES))
    parser.add_argument("--every-fake", action="store_true", help="one fake of every applicable kind per control")
    parser.add_argument("--set", default="", help="subfolder for this case set (e.g. holdout)")
    args = parser.parse_args()
    out = OUT / args.set if args.set else OUT
    out.mkdir(parents=True, exist_ok=True)
    cases = collect(args.controls, args.sources.split(","), args.every_fake)
    (out / "cases.json").write_text(json.dumps(cases, indent=1))
    cache_path = OUT / "transcripts.json"     # shared: keyed by audio hash
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    rows = []
    for number, case in enumerate(cases, 1):
        heard = transcribe(args.url, case["audio"], cache)
        cache_path.write_text(json.dumps(cache, indent=1))
        whisper, gemma = score(case, case["whisper_raw"]), score(case, heard["text"])
        shared = sorted(departures(whisper) & departures(gemma))
        words = case["expected_text"].split()
        row = {"id": case["id"], "kind": case["kind"], "change": case.get("change"), "run": case["run"],
               "point": case["point"], "whisper_pass": whisper["status"] == "passed",
               "whisper_reasons": whisper["reasons"], "gemma_pass": gemma["status"] == "passed",
               "gemma_reasons": gemma["reasons"], "gemma_score": gemma["score"], "whisper_score": whisper["score"],
               "two_ears_pass": not shared, "shared_words": [words[i] for i in shared if i < len(words)],
               "gemma_differences": script_differences(gemma), "gemma_text": heard["text"],
               "gemma_seconds": heard["seconds"]}
        rows.append(row)
        print(f"{number:3}/{len(cases)} {case['kind']:22} {case.get('change') or '':14} whisper={row['whisper_pass']!s:5} "
              f"gemma={row['gemma_pass']!s:5} two_ears={row['two_ears_pass']!s:5} {heard['seconds']:5.1f}s "
              f"shared={row['shared_words'][:6]}", flush=True)
    summary = {}
    for kind in sorted({r["kind"] for r in rows}):
        group = [r for r in rows if r["kind"] == kind]
        summary[kind] = {"cases": len(group), **{rule: sum(r[rule] for r in group)
                         for rule in ("whisper_pass", "gemma_pass", "two_ears_pass")}}
    summary["gemma_seconds_mean"] = round(sum(r["gemma_seconds"] for r in rows) / max(len(rows), 1), 2)
    (out / "report.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
