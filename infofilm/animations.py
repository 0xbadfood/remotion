"""Animated beats in curriculum films: a Manim scene coded for one beat by Lesson Studio's coder (scivid.coded), timed
to the recorded narration, rendered in its sandbox and reviewed from code and frames, then played full frame.

A film gets the animation kind only when its figure options carry "animation" (Chemistry Studio sets it); every other
film plans and renders exactly as before. The options:
  {"code_root": "/home/nitin/manim", ...figures..., "animation": {"manim_python": ".../bin/python",
   "python_base": "/home/nitin/.pyenv/versions/3.11.15", "scivid_data": "/home/nitin/manim/scivid-data",
   "kits": ["chem"]}}
The coder and reviewers use Lesson Studio's model settings (scivid-data/model-settings.json), the models that code
its lessons.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ANIMATION_TASK = (
    " Some ideas only make sense in motion: electrons moving between orbits and giving out light, particles "
    "deflected or scattered, atoms combining, bonds breaking and forming, a quantity changing as another changes. "
    "For such a passage use kind:'animation': a coder will animate it exactly, in time with the narration. An "
    "animation beat has a heading and summary like other beats, empty labels/details/image_brief, and an "
    "'animation' object: {title:'<=8 words',objective:'what the learner should see happen',complexity:'routine'|"
    "'involved'|'novel',visual:'what is on screen and what moves, in order',cues:[{phrase:'2-8 consecutive words "
    "spoken in THIS beat, copied exactly',show:'what changes on screen at that moment'}]}. Give 2-6 cues in spoken "
    "order; the first at the beat's opening words. An animation beat lasts at least 8 seconds of narration, usually "
    "one whole point; it may span several sentences. Prefer animation over a still figure or image whenever the "
    "narration describes a process or a change; keep image beats for people, places and the history of the idea.")

MIN_SECONDS = 8.0
TIERS = ('routine', 'involved', 'novel')


def options(figures):
    """The animation options of a film's figure options, or None."""
    return (figures or {}).get('animation') or None


def _norm(word):
    return re.sub(r"[^\w]", "", str(word).lower().replace("’", "'"))


def find_phrase(words, phrase, start=0):
    """Index in `words` (timing words) where `phrase` begins, at or after `start`, or None."""
    want = [w for w in (_norm(t) for t in str(phrase).split()) if w]
    have = [_norm(w["word"]) for w in words]
    if not want:
        return None
    for i in range(start, len(have) - len(want) + 1):
        if have[i:i + len(want)] == want:
            return i
    return None


def spans(scenes, timing):
    """(start seconds, end seconds, words) of every beat, as the timeline will show it."""
    words = timing["words"]
    positions = {w["id"]: i for i, w in enumerate(words)}
    indices = [positions.get(s.get("start_word_id"), 0) for s in scenes]
    duration = timing.get("audio_info", {}).get("duration_seconds") or (words[-1]["end"] if words else 0)
    starts = [0.0] + [words[i]["start"] for i in indices[1:]]
    ends = starts[1:] + [duration]
    return [(start, end, words[index:(indices[i + 1] if i + 1 < len(indices) else len(words))])
            for i, (start, end, index) in enumerate(zip(starts, ends, indices))]


def cues(scene, start, end, words):
    """The coder's cues on the beat clock: each starts at its phrase and ends where the next begins."""
    found, at = [], 0
    for cue in scene["animation"]["cues"]:
        i = find_phrase(words, cue["phrase"], at)
        found.append({"phrase": cue["phrase"], "show": cue.get("show", ""),
                      "start": round(max(0.0, words[i]["start"] - start), 2)})
        at = i + 1
    for c, nxt in zip(found, found[1:] + [None]):
        c["end"] = round((nxt["start"] if nxt else end - start), 2)
    return found


def scene_errors(scenes, timing, figures):
    """Every animation beat: its shape, its length, and cues found in its own words, in order."""
    errors = []
    allowed = options(figures)
    for scene, (start, end, words) in zip(scenes, spans(scenes, timing)):
        if scene.get("kind") != "animation":
            continue
        sid, spec = scene.get("id"), scene.get("animation")
        if not allowed:
            errors.append(f"{sid}: unsupported visual beat kind")
            continue
        if not isinstance(spec, dict):
            errors.append(f'{sid}: animation beats need an "animation" object')
            continue
        if not isinstance(spec.get("title"), str) or not 1 <= len(spec["title"].split()) <= 8:
            errors.append(f"{sid}: animation title must be 1-8 words")
        for key in ("objective", "visual"):
            if not isinstance(spec.get(key), str) or len(spec[key].split()) < 4:
                errors.append(f"{sid}: say the animation's {key}")
        if spec.get("complexity") not in TIERS:
            errors.append(f"{sid}: complexity must be routine, involved or novel")
        if end - start < MIN_SECONDS:
            errors.append(f"{sid}: an animation needs at least {MIN_SECONDS:.0f} s of narration; this beat has "
                          f"{end - start:.1f} s (start it earlier or let it run over the next sentence)")
        listed = spec.get("cues")
        if not isinstance(listed, list) or not 2 <= len(listed) <= 6:
            errors.append(f"{sid}: give 2-6 cues")
            continue
        at = 0
        for n, cue in enumerate(listed, 1):
            if not isinstance(cue, dict) or not isinstance(cue.get("phrase"), str) or not cue.get("show"):
                errors.append(f"{sid}: cue {n} needs a phrase and what it shows")
                break
            i = find_phrase(words, cue["phrase"], at)
            if i is None:
                spoken = " ".join(w["word"] for w in words)
                errors.append(f'{sid}: cue {n} phrase "{cue["phrase"]}" is not spoken (in order) in this beat, '
                              f'whose words are: "{spoken[:400]}"')
                break
            at = i + 1
    return errors


# ------------------------------------------------------------------ the coder, run as in Lesson Studio
class CoderJob:
    """What scivid.coded.code_chapter needs from a Lesson Studio job: settings, model clients and a log."""
    parallel_reviews = True

    def __init__(self, store, animation, folder, log):
        from scivid import models, review
        from scivid.settings import Settings
        data = Path(animation.get("scivid_data") or "/home/nitin/manim/scivid-data")
        self.models = models.Registry(data, dict).snapshot()
        review.configure(self.models.get("review_rules"))
        self.settings = Settings(manim_python=animation["manim_python"], python_base=animation["python_base"],
                                 width=animation.get("width", 1920), height=animation.get("height", 1080),
                                 fps=animation.get("fps", 30))
        self._models, self._data, self._folder, self._log = models, data, Path(folder), log
        self._clients, self._lock = {}, threading.Lock()

    def llm(self, step):
        with self._lock:
            if step not in self._clients:
                self._clients[step] = self._models.client_for(self.models, step, self._folder, self._data)
            return self._clients[step]

    def log(self, message):
        self._log(message)

    def chapter_status(self, cid, status, detail=""):
        pass


def _scivid(figures):
    root = str(Path(figures.get("code_root") or "/home/nitin/manim"))
    if root not in sys.path:
        sys.path.insert(0, root)
    from scivid import coded, kit_docs
    return coded, kit_docs


def evidence_text(evidence, scene, script):
    """The facts of the beat's narration point, then the film's other facts, as the coder's evidence."""
    point = next(p for p in script["points"] if p["id"] == scene["point_id"])
    facts = {f["id"]: f for f in evidence.get("facts", [])}
    first = [facts[f] for f in point.get("fact_ids", []) if f in facts]
    rest = [f for f in facts.values() if f not in first]
    return "\n".join(f"- {f['claim']}" for f in first + rest)[:6000]


def key(scene, span_cues, seconds):
    return hashlib.sha256(json.dumps([scene["animation"], span_cues, round(seconds, 2)], sort_keys=True)
                          .encode()).hexdigest()[:20]


def render(store, films, figures, evidence, brief=None, log=print, workers=3):
    """Code, render and review every animation beat (cached by its spec and timing); note the clip, its hash and the
    review outcome on the scene. Raises when a beat has no rendered clip at all."""
    animation = options(figures)
    coded, kit_docs = _scivid(figures)
    background = ((brief or {}).get("theme") or {}).get("background") or "#082d34"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", background):
        background = "#082d34"
    kit = coded.KIT + ("\n" + kit_docs.CHEM if "chem" in animation.get("kits", ["chem"]) else "")
    folder = Path(store.root) / "animations"
    jobs = []
    for film in films:
        scenes = film["storyboard"]["scenes"]
        for scene, (start, end, words) in zip(scenes, spans(scenes, film["timing"])):
            if scene.get("kind") == "animation":
                jobs.append((film, scene, start, end, words))
    if not jobs:
        return
    job = CoderJob(store, animation, folder, log)
    errors = []

    def one(item):
        film, scene, start, end, words = item
        seconds = end - start
        clock = cues(scene, start, end, words)
        spec = scene["animation"]
        work = folder / key(scene, clock, seconds)
        chapter = {"id": "a" + re.sub(r"\W", "", scene["id"]).lower(), "title": spec["title"],
                   "objective": spec["objective"], "complexity": spec["complexity"], "visual": spec["visual"],
                   "demonstrations": [c["show"] for c in clock]}
        plan = {"title": film["script"]["title"], "big_idea": spec["objective"], "theme": {"background": background},
                "colours": "nucleus red, electrons cyan; light in the colour of its wavelength (chem_kit)"}
        narration = " ".join(w["word"] for w in words)
        sc = {"points": [{"id": scene["point_id"], "narration": narration}]}
        timing = {"points": [{"point_id": scene["point_id"], "start": 0.0, "end": round(seconds, 2)}]}
        video, record = coded.code_chapter(job, plan, chapter, sc, timing, clock, seconds,
                                           evidence_text(evidence, scene, film["script"]), work, kit=kit)
        scene["animation_file"] = str(video)
        scene["animation_sha256"] = hashlib.sha256(Path(video).read_bytes()).hexdigest()
        scene["animation_seconds"] = round(seconds, 3)
        scene["animation_review"] = {"passed": "kept_attempt" not in record, "attempts": record.get("attempts"),
                                     "tier": record.get("tier"), "notes": record.get("notes", [])[:6]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item, outcome in zip(jobs, pool.map(lambda i: _safe(one, i), jobs)):
            if outcome:
                errors.append(f"{item[1].get('id')}: {outcome}"[:400])
    if errors:
        raise ValueError(f"Animations could not be rendered: {errors}")


def _safe(fn, item):
    try:
        fn(item)
    except Exception as exc:                                             # noqa: BLE001 - reported together
        return f"{type(exc).__name__}: {exc}"
    return None
