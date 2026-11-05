"""A finished film in another language (user, 2026-11-05): the paediatric cardiologist's procedure films for parents, in
English and Hindi. The film's bundle (infofilm.rendition) carries its pictures, storyboard and script; this module
translates the script point by point, voices it, and renders the same pictures to the new narration.

Why a translation of the finished film, and not a second film planned in Hindi: the English film's facts were checked
against its sources, its pictures reviewed and approved; the Hindi film says the same things over the same pictures.

  translate   Opus writes each point in the language as a doctor would say it to parents (Hindi: everyday Hindi in
              Devanagari, the English medical words people use in Latin script: catheter, balloon, stent). Each scene's
              start word is marked in the English («S03») and the model puts the same mark before the word that starts
              that scene in its sentence, so the cuts land on the same thought. Headings and on-screen cards are
              translated too; a figure's own labels stay as drawn.
  voice       one take per point: VibeVoice cloning a reference (the Hindi model on this machine, :18092), or a HeyGen
              voice through OpenRouter. Whisper (large-v3) hears every take; a take that does not say its text
              (VibeVoice can drift or invent words) is made again.
  align       MMS on the media factory, with each word romanised (its dictionary is Latin); captions keep the script.
  render      the narration room, timeline and render of the rendition, with the bundle's own look.

  python -m infofilm.languages translate <bundle> --lang hi
  python -m infofilm.languages render <bundle> <out> --lang hi --voice gautam-hi|bram
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import unicodedata
import wave
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from .core import ROOT, Store, digest, read, resource_slot, write

LANGUAGES = {
    "hi": {"name": "Hindi", "whisper": "hi", "vibe_model": "hi_vibe_full", "sentence_end": "।",
           "style": ("everyday spoken Hindi in Devanagari script, as an Indian paediatric cardiologist explains a procedure "
                     "to worried parents: warm, plain, respectful (आप). Keep in Latin script the English medical and device "
                     "words Indian parents hear from their doctors (heart chambers may be चैंबर or chamber; keep catheter, "
                     "balloon, stent, device, wire, echo, X-ray, ICU, aorta, valve when the English uses them). Never use "
                     "Sanskritised textbook words where people say the English one. Write every number in Hindi words in "
                     "Devanagari (सत्रह, not 17).")},
}
VOICES = {
    "gautam-hi": {"engine": "vibevoice", "label": "Dr Gautam (Hindi)", "reference": str(ROOT / "references" / "gautam-hi.wav")},
    "bram": {"engine": "heygen", "label": "Bram (HeyGen, Hindi)", "voice": "bram"},
}
VIBE_URL = os.environ.get("VIBE_URL", "http://127.0.0.1:18092")
LUX_URL = os.environ.get("LUX_URL", "http://10.8.0.8:8078")     # the media factory: MMS alignment
OPENROUTER_URL = "https://openrouter.ai/api/v1"
HEYGEN_MODEL = "heygen/heygen-voice-1-turbo"
TRANSLATOR = {"model": "claude-opus-5-5", "reasoning_effort": "medium", "timeout": 900}
POINT_GAP = 0.7            # seconds of silence between points
TAKES = 4                  # takes per point before the film stops
MIN_HEARD = 0.8            # share of the point's letters Whisper must hear back (romanised); see heard_score
HEAR_WINDOW = 10           # seconds of audio Whisper decodes at a time; see hear
SR = 48000
MARK = re.compile(r"«(S\d+)»")


# ------------------------------------------------------------------ translation
PROMPT = """Translate this film's narration for parents into {name}: {style}

The film is already made; your words will be spoken over its pictures. Say exactly what each point says: add nothing,
drop nothing, keep every qualifier ("usually", "in many children", "may"), and keep it as easy to follow when heard
once. About as long as the English.

Some English points carry scene marks like «S03» in front of a word: the picture changes there. Put the same mark, once,
in front of the word of your sentence where the same idea starts, in the same order. A mark at the start of a point
stays at the start.

Also translate each scene's on-screen text (heading, summary, cards), short as the English.

Return JSON only:
{{"points": [{{"id": "P01", "narration": "..."}}], "scenes": [{{"id": "S01", "heading": "...", "summary": "...", "labels": ["..."], "details": ["..."]}}]}}

POINTS:
{points}

SCENES (on-screen text):
{scenes}"""


def marked_points(script, storyboard, timing):
    """Each point's narration with «Sxx» before the word each scene starts on. Anchors are word ids of the English
    film's timing, whose words are the narration's own words plus the narration room's phrases at a point's end."""
    where = {wid: (tp["point_id"], k) for tp in timing["points"] for k, wid in enumerate(tp["word_ids"])}
    starts = {}
    for s in storyboard["scenes"]:
        if s.get("start_word_id"):
            pid, k = where[s["start_word_id"]]
            starts[(pid, k)] = s["id"]
    out = []
    for p in script["points"]:
        words = p["narration"].split()
        marked = []
        for k, w in enumerate(words):
            sid = starts.pop((p["id"], k), None)
            marked.append(f"«{sid}» {w}" if sid else w)
        for (pid, k), sid in sorted(starts.items()):
            if pid == p["id"]:                       # a scene that starts on a room phrase starts on the point's last word
                marked[-1] = f"«{sid}» {marked[-1]}"
        out.append({"id": p["id"], "narration": " ".join(marked)})
    return out


def source_timing(bundle):
    from .rendition import film_dir
    return read(film_dir(ROOT / "artifacts" / "runs" / read(Path(bundle) / "bundle.json")["source_run"]) / "timing.json")


def translation_problems(result, english, storyboard, lang):
    out = []
    want = {p["id"]: MARK.findall(p["narration"]) for p in english}
    got = {p.get("id"): p.get("narration") or "" for p in result.get("points", [])}
    if set(got) != set(want):
        out.append(f"points must be exactly {sorted(want)}; got {sorted(got)}")
    for pid, marks in want.items():
        text = got.get(pid, "")
        if MARK.findall(text) != marks:
            out.append(f"{pid}: scene marks must be {marks} in that order, got {MARK.findall(text)}")
        if marks and text.strip() and english[[p["id"] for p in english].index(pid)]["narration"].startswith("«") and not text.strip().startswith("«"):
            out.append(f"{pid}: the English starts with a scene mark, so yours must too")
        if lang == "hi":
            if re.search(r"\d", MARK.sub("", text)):
                out.append(f"{pid}: write numbers in Hindi words, not digits")
            if len(re.findall(r"[ऀ-ॿ]", text)) < 0.5 * len(re.sub(r"\W", "", MARK.sub("", text)) or "x"):
                out.append(f"{pid}: mostly Devanagari, please (only the English medical words in Latin)")
        e = len(MARK.sub("", english[[p["id"] for p in english].index(pid)]["narration"]).split()) if pid in want else 0
        h = len(MARK.sub("", text).split())
        if e and not .6 * e <= h <= 2.0 * e:
            out.append(f"{pid}: {h} words for {e} English words; keep it about as long")
    scenes = {s.get("id") for s in result.get("scenes", [])}
    missing = [s["id"] for s in storyboard["scenes"] if s["id"] not in scenes]
    if missing:
        out.append(f"scenes missing: {missing}")
    return out


def _anthropic_key():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    secrets = read(ROOT / "artifacts" / "interface" / "llm-secrets.json")
    return next(v for k, v in secrets.items() if k.endswith("-anthropic") and isinstance(v, str))


def translate(bundle, lang="hi"):
    """bundle/<lang>/: script.json and storyboard.json in the language, word anchors moved to the new words."""
    from .anthropic_llm import json_call
    bundle = Path(bundle)
    script, storyboard = read(bundle / "script.json"), read(bundle / "storyboard.json")
    spec = LANGUAGES[lang]
    english = marked_points(script, storyboard, source_timing(bundle))
    scenes = [{k: s[k] for k in ("id", "heading", "summary", "labels", "details") if s.get(k)} for s in storyboard["scenes"]]
    prompt = PROMPT.format(name=spec["name"], style=spec["style"], points=json.dumps(english, ensure_ascii=False, indent=1),
                           scenes=json.dumps(scenes, ensure_ascii=False, indent=1))
    folder = bundle / lang
    folder.mkdir(exist_ok=True)
    problems = []
    for attempt in range(3):
        ask = prompt if not problems else prompt + "\n\nYour last answer had these problems; fix them:\n- " + "\n- ".join(problems)
        raw, text, _ = json_call(TRANSLATOR, _anthropic_key(), system="You translate medical explainer films for parents. JSON only.",
                                 prompt=ask, images=[], max_tokens=32000, label=f"translate-{lang}")
        write(folder / f"translation-{attempt + 1}.json", {"text": text, "usage": raw.get("usage")})
        try:
            result = json.loads(text[text.index("{"):text.rindex("}") + 1])
        except ValueError:
            problems = ["the answer was not JSON"]
            continue
        problems = translation_problems(result, english, storyboard, lang)
        if not problems:
            break
    else:
        raise RuntimeError(f"translation to {lang} failed its checks: {problems[:5]}")
    by_point = {p["id"]: p["narration"] for p in result["points"]}
    points, anchors, n = [], {}, 0
    for p in script["points"]:
        words = []
        for token in by_point[p["id"]].split():
            m = MARK.fullmatch(token)
            if m:
                anchors[m.group(1)] = f"w{n + 1:04d}"
                continue
            words.append(token)
            n += 1
        points.append({**p, "narration": " ".join(words)})
    texts = {s["id"]: s for s in result["scenes"]}
    scenes_out = []
    for s in storyboard["scenes"]:
        t = texts.get(s["id"], {})
        s = {**s, **{k: t[k] for k in ("heading", "summary", "labels", "details") if t.get(k) and s.get(k)}}
        if s.get("start_word_id"):
            s["start_word_id"] = anchors[s["id"]]
        scenes_out.append(s)
    write(folder / "script.json", {**script, "points": points, "language": lang})
    write(folder / "storyboard.json", {**storyboard, "scenes": scenes_out})
    return folder


# ------------------------------------------------------------------ words: display, tokens for the aligner, scoring
# Devanagari -> Latin for the forced aligner and for scoring; ported from ~/advert/advert/lux.py (romanize, tokenize).
_DEV_V = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
          "ऑ": "o", "ऍ": "e"}
_DEV_M = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e"}
_DEV_C = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny", "ट": "t",
          "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "प": "p", "फ": "ph", "ब": "b",
          "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "क़": "q", "ख़": "kh",
          "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y"}


def romanize(word):
    """Rough ITRANS-style transliteration of a Devanagari word; Latin text passes through."""
    if not re.search(r"[ऀ-ॿ]", word):
        return word
    w = unicodedata.normalize("NFC", word)
    out, i = [], 0
    while i < len(w):
        ch, two = w[i], w[i:i + 2]
        if two in _DEV_C:
            cons, i = _DEV_C[two], i + 2
        elif ch in _DEV_C:
            cons, i = _DEV_C[ch], i + 1
        else:
            out.append(_DEV_V.get(ch) or _DEV_M.get(ch) or ("n" if ch in "ंँ" else "h" if ch == "ः" else ch if ch.isascii() else ""))
            i += 1
            continue
        nxt = w[i] if i < len(w) else ""
        if nxt == "्":
            out.append(cons)
            i += 1
        elif nxt in _DEV_M:
            out.append(cons + _DEV_M[nxt])
            i += 1
        elif i >= len(w) and out:
            out.append(cons)
        else:
            out.append(cons + "a")
    return "".join(out)


def tokens(text):
    t = unicodedata.normalize("NFKC", text or "").lower().replace("’", "'")
    return [x.strip("'") for x in re.findall(r"[\w'ऀ-ॣ०-ॿ]+", t) if x.strip("'")]


def _sounds(text):
    """Letters as heard: romanised, vowel lengths and v/w, ph/f, z/j merged, so Whisper writing "चेंबर्स" for "chambers"
    or "डिवाइस" for "device" still counts as the word."""
    s = "".join(romanize(t) for t in tokens(text))
    for a, b in (("aa", "a"), ("ee", "i"), ("oo", "u"), ("w", "v"), ("ph", "f"), ("z", "j"), ("c", "k"), ("q", "k"), ("y", "i")):
        s = s.replace(a, b)
    s = re.sub(r"[aeiou]", "", s)           # vowels are where scripts disagree most; consonants carry the words
    return s


def heard_score(expected, heard):
    """Share of the expected text's consonant skeleton found, in order, in what Whisper heard."""
    a, b = _sounds(expected), _sounds(heard)
    if not a:
        return 1.0
    matched = sum(m.size for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    extra = max(0, len(b) - len(a)) / len(a)          # invented speech counts against the take
    return round(matched / len(a) - extra, 4)


# ------------------------------------------------------------------ voices
def _wav48(src, dst, rate=None):
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"] + (["-f", "s16le", "-ac", "1", "-ar", str(rate)] if rate else []) + \
          ["-i", "-" if rate else str(src), "-ac", "1", "-ar", str(SR),
           "-af", "silenceremove=start_periods=1:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse",
           str(dst)]
    subprocess.run(cmd, input=src if rate else None, check=True)


def _openrouter_key():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    return read(ROOT / "artifacts" / "interface" / "llm-secrets.json")["haiku"]


def _heygen(text, dst, voice):
    with httpx.Client(base_url=OPENROUTER_URL, headers={"Authorization": f"Bearer {_openrouter_key()}"}, timeout=180) as c:
        for attempt in range(8):
            r = c.post("/audio/speech", json={"model": HEYGEN_MODEL, "input": text, "voice": voice, "response_format": "pcm"})
            if r.status_code == 200 and len(r.content) > 1000:
                break
            time.sleep(min(60, 8 * (attempt + 1)) if r.status_code in (429, 402, 502, 503) else 3)
        else:
            raise RuntimeError(f"HeyGen failed: {r.status_code} {r.text[:120]}")
    m = re.search(r"rate=(\d+)", r.headers.get("content-type", ""))
    _wav48(r.content, dst, rate=int(m.group(1)) if m else 44100)


def _vibevoice(text, dst, reference, lang, seed):
    with httpx.Client(base_url=VIBE_URL, timeout=600) as c:
        health = c.get("/health").json()
        while health.get("active_job") or health.get("queued_or_running_jobs"):
            time.sleep(10)
            health = c.get("/health").json()
        model = LANGUAGES[lang]["vibe_model"]
        if health.get("model_loaded") != model:
            c.post("/load", json={"lang_key": model}).raise_for_status()
        with open(reference, "rb") as f:
            staged = c.post("/stage-speaker", data={"speaker_id": f"lang_{Path(reference).stem}"},
                            files={"file": (Path(reference).name, f, "audio/wav")}).raise_for_status().json()
        job_id = f"lang_{digest(text)[:10]}_{seed}_{int(time.time())}"
        body = {"job_id": job_id, "lang_key": model, "cfg_scale": 1.3, "seed": seed,
                "items": [{"script": "Speaker 1: " + text, "voice_samples": [staged["voice_ref"]], "output_name": "take.wav", "chunk_index": 0}]}
        for _ in range(90):                  # 409: the adapter is still on another job (one at a time); wait for it
            r = c.post("/render-batch", json=body)
            if r.status_code != 409:
                break
            time.sleep(10)
        r.raise_for_status()
        while (job := c.get(f"/jobs/{job_id}").json()).get("status") not in ("success", "failed"):
            time.sleep(2)
        if job["status"] != "success":
            raise RuntimeError(f"VibeVoice failed: {json.dumps(job)[:200]}")
    made = sorted(Path(job["output_path"]).rglob("*.wav"))
    if not made:
        raise RuntimeError(f"VibeVoice job {job_id} left no audio")
    _wav48(made[0], dst)


_whisper = None


def hear(path, lang):
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("large-v3", device="cpu", compute_type="int8", cpu_threads=12)
    # 10 s windows: Devanagari costs Whisper several tokens a letter, and a 30 s window ran out of tokens mid-word,
    # so every take of a long point was heard cut short (2026-11-06, P16 of the Hindi BAV film: 0.77, then 0.94)
    segments, _ = _whisper.transcribe(str(path), language=LANGUAGES[lang]["whisper"], beam_size=5, vad_filter=True,
                                      chunk_length=HEAR_WINDOW)
    return " ".join(s.text.strip() for s in segments)


def seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def voice_point(point, folder, lang, voice, log=print):
    """One audited take of a point, cached by its text and voice: (wav, record)."""
    spec = VOICES[voice]
    key = digest({"text": point["narration"], "voice": voice, "spec": spec, "lang": lang})[:16]
    record_path = folder / f"{point['id']}-{key}.json"
    if record_path.exists() and read(record_path).get("status") == "passed":
        record = read(record_path)
        return Path(record["wav"]), record
    takes = []
    for take in range(1, TAKES + 1):
        wav = folder / f"{point['id']}-{key}-t{take}.wav"
        if spec["engine"] == "heygen":
            _heygen(point["narration"], wav, spec["voice"])
        else:
            _vibevoice(point["narration"], wav, spec["reference"], lang, seed=take)
        heard = hear(wav, lang)
        score = heard_score(point["narration"], heard)
        takes.append({"take": take, "wav": str(wav), "heard": heard, "score": score, "seconds": round(seconds(wav), 2)})
        log(f"  {point['id']} take {take}: heard {score:.2f} ({takes[-1]['seconds']} s)")
        if score >= MIN_HEARD:
            break
    best = max(takes, key=lambda t: t["score"])
    record = {"status": "passed" if best["score"] >= MIN_HEARD else "failed", "wav": best["wav"], "takes": takes, "voice": voice}
    write(record_path, record)
    if record["status"] != "passed":
        raise RuntimeError(f"{point['id']}: no take said its text (best {best['score']}): {best['heard'][:120]}")
    return Path(best["wav"]), record


def _align(wav, words):
    """MMS word spans [(start, end)] for display words, or None (the aligner gets romanised tokens)."""
    toks = [(romanize(t), i) for i, w in enumerate(words) for t in tokens(w)]
    try:
        with httpx.Client(base_url=LUX_URL, timeout=300) as c, open(wav, "rb") as f:
            v = c.post("/api/align", data={"words": json.dumps([t for t, _ in toks])},
                       files={"audio": ("audio.wav", f, "audio/wav")}).raise_for_status().json()
        entries = [e for e in v["payload"]["tiers"]["words"]["entries"] if str(e[2]).strip() and e[2] != "<eps>"]
    except Exception:  # noqa: BLE001
        return None
    if len(entries) != len(toks):
        return None
    spans = {}
    for (_, i), (s, e, _) in zip(toks, entries):
        a, b = spans.get(i, (s, e))
        spans[i] = (min(a, s), max(b, e))
    return [spans.get(i) for i in range(len(words))]


def narrate(script, folder, lang, voice, log=print):
    """The joined narration and its timing (infofilm.timing.v1), from one audited take per point."""
    from .media import audio_info
    folder.mkdir(parents=True, exist_ok=True)
    clips = [(p, *voice_point(p, folder, lang, voice, log)) for p in script["points"]]
    gap = b"\0\0" * int(POINT_GAP * SR)
    pcm, words, points, offset, n = bytearray(), [], [], 0.0, 0
    for p, wav, _ in clips:
        with wave.open(str(wav)) as w:
            frames = w.readframes(w.getnframes())
        length = len(frames) / 2 / SR
        shown = p["narration"].split()
        spans = _align(wav, shown)
        if spans is None or any(s is None for s in spans):          # spread by letters: the scene cuts stay close
            total = sum(len(x) + 1 for x in shown)
            t, spans = 0.1, []
            for x in shown:
                d = (length - .2) * (len(x) + 1) / total
                spans.append((t, t + d * .9))
                t += d
            log(f"  {p['id']}: aligner missed, words spread by length")
        ids = []
        for x, (s, e) in zip(shown, spans):
            n += 1
            ids.append(f"w{n:04d}")
            words.append({"id": ids[-1], "word": x, "start": round(offset + s, 3), "end": round(offset + e, 3)})
        points.append({"point_id": p["id"], "start": words[-len(ids)]["start"], "end": words[-1]["end"], "word_ids": ids})
        pcm += frames + gap
        offset += length + POINT_GAP
    audio = folder / f"narration-{digest(bytes(pcm))[:12]}.wav"
    with wave.open(str(audio), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(pcm))
    info = audio_info(audio)
    text = "\n\n".join(p["narration"] for p in script["points"])
    return {"schema_version": "infofilm.timing.v1", "audio": str(audio), "audio_sha256": info["audio_sha256"],
            "text_sha256": digest(text), "words": words, "points": points, "audio_info": {**info, "text": text},
            "provenance": {"engine": "MMS (media factory)", "voice": voice, "language": lang}}


# ------------------------------------------------------------------ render
def render_language(bundle, out, *, lang="hi", voice="gautam-hi", log=print):
    from .music import prepare_music
    from .narration_room import apply as give_room
    from .rendition import _resolve, anchor_problems, film_config, film_gain, loudness, verify
    from .video import compile_timeline, render
    bundle, out = Path(bundle).resolve(), Path(out).resolve()
    manifest = verify(bundle)
    master = read(bundle / "master.json")
    script = read(bundle / lang / "script.json")
    storyboard = _resolve(read(bundle / lang / "storyboard.json"), bundle)
    images = _resolve(read(bundle / "images.json"), bundle)
    extras = _resolve(read(bundle / "extras.json"), bundle)
    for name, key in (("depth", "manifest.json"), ("clips", "manifest.json")):
        if extras.get(name):
            write(out / name / key, extras[name])
    if extras.get("backdrop"):
        write(out / "backdrop" / "current.json", extras["backdrop"])
    config = {**film_config(), "tts_engine": VOICES[voice]["engine"], "speech_conditioning": False, "width": master["width"],
              "height": master["height"], "fps": master["fps"]}
    store = Store(out)
    try:
        store.event("language", "voice", {"title": manifest["title"], "lang": lang, "voice": voice})
        timing = narrate(script, out / "narration", lang, voice, log)
        lost = anchor_problems(storyboard, timing)
        if lost:
            raise ValueError(f"scene anchors missing from the {lang} words: {lost}")
        duration = script.get("requested_seconds") or round(master["seconds"])
        film = {"script": script, "storyboard": storyboard, "timing": timing, "audio": timing["audio"], "audio_info": timing["audio_info"],
                "actual_seconds": timing["audio_info"]["duration_seconds"], "requested_seconds": duration}
        (out / f"film-{duration}").mkdir(parents=True, exist_ok=True)
        give_room(config, store, [film])
        config["delivery_gain_db"] = film_gain(loudness(film["timing"]["audio"])[0])
        film["music"] = {**prepare_music(config, store, film, read(bundle / "creative-brief.json"), "none"), "use_master": True}
        root, timeline = compile_timeline(config, store, script, film["timing"], film["storyboard"], images, read(bundle / "evidence-final.json"),
                                          read(bundle / "sources.json"), duration, creative_brief=read(bundle / "creative-brief.json"), music=film["music"])
        with resource_slot("remotion-render", config.get("render_slots", 2)):
            video = render(config, store, root, timeline)
        result = {"video": str(video), "seconds": round(film["actual_seconds"], 1), "qa": read(root / "qa.json")["passed"],
                  "lang": lang, "voice": voice, "bundle": manifest.get("concept") or manifest["title"]}
        write(out / "language.json", result)
        store.event("language", "succeeded", result)
        return result
    finally:
        store.db.close()


def make(run, lang="hi", voice="gautam-hi", log=print):
    """A finished film run in another language, end to end: its bundle (exported once), the translation (once), the
    render in this voice. Returns the render's result; everything is kept, so a second voice reuses the rest."""
    from .rendition import export
    run = Path(run).resolve()
    bundle = ROOT / "artifacts" / "bundles" / "films" / run.name
    if not (bundle / "bundle.json").exists():
        export(run, bundle)
    if not (bundle / lang / "storyboard.json").exists():
        translate(bundle, lang)
    return render_language(bundle, ROOT / "artifacts" / "languages" / f"{run.name}-{lang}-{voice}", lang=lang, voice=voice, log=log)


def main():
    parser = argparse.ArgumentParser(prog="infofilm.languages")
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("make")
    a.add_argument("run")
    a.add_argument("--lang", default="hi", choices=sorted(LANGUAGES))
    a.add_argument("--voice", default="gautam-hi", choices=sorted(VOICES))
    a = sub.add_parser("translate")
    a.add_argument("bundle")
    a.add_argument("--lang", default="hi", choices=sorted(LANGUAGES))
    a = sub.add_parser("render")
    a.add_argument("bundle")
    a.add_argument("out")
    a.add_argument("--lang", default="hi", choices=sorted(LANGUAGES))
    a.add_argument("--voice", default="gautam-hi", choices=sorted(VOICES))
    args = parser.parse_args()
    if args.command == "make":
        print(json.dumps(make(args.run, args.lang, args.voice), ensure_ascii=False))
    elif args.command == "translate":
        print(translate(args.bundle, args.lang))
    else:
        print(json.dumps(render_language(args.bundle, args.out, lang=args.lang, voice=args.voice), ensure_ascii=False))


if __name__ == "__main__":
    main()
