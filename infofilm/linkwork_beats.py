"""Linkwork beats: exact, code-drawn motion and diagrams (~/linkwork) as a visual beat kind (user, 2026-10-24).

A film whose curriculum record carries "linkwork" ({"subject": "physics"}; Physics and Maths Studios set it) gets the
beat kind 'linkwork'. In the same planning call that writes the other beats, the planner may give a beat a verb spec
instead of an image brief. It chooses among a few verb cards that code shortlists from Linkwork's own cards by what
the concept teaches, so the prompt carries only the verbs that could fit. Code then compiles and checks every spec
with Linkwork (one node process for all of them): a spec that fails, or whose motion outlasts its beat, goes back to
the planner with Linkwork's plain-language errors, like any other beat error. A passing spec's scene is saved and drawn
frame by frame in the film (renderer/src/Linkwork.tsx), with its times counted from the beat's start.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from .core import write

TASK = (
    " This film may draw exact, code-made scenes with Linkwork: use kind:'linkwork' for a beat whose passage describes "
    "something a verb in linkwork_options.verbs shows, when the verb's use_when fits and its not_when does not "
    "(an object moving along a line with its position, displacement and velocity; an object's size against the distance "
    "it moves; a graph of one quantity against another). Prefer a Linkwork beat to an image whenever one fits: it is "
    "exact, light and cheap; keep images for the world (people, places, everyday scenes, the feel of an experiment). A "
    "linkwork beat has a heading and summary like other beats, empty labels/details/image_brief, and a 'linkwork' "
    "object that is the verb's spec, written as its card's fields and example show (start from the example). Times "
    "in the spec are seconds from the start of the beat, and its motion must end within the beat's own duration "
    "(the sentence timings show how long it lasts). Use only numbers the narration states or that follow from them: "
    "the numbers it draws are computed from the spec and shown on screen. When the narration sets the same thing in "
    "two situations (a train beside a platform, then on a long route; a bus on its route, then in a parking bay), show "
    "both in ONE beat as the verb's cases, so the contrast is on screen together, and draw each as the narration names "
    "it (the case's setting). Every passage whose use_when fits is a Linkwork beat, not an image of the same thing. "
    "Code checks every spec and returns its errors.")

LINKWORK = Path.home() / "linkwork"
NODE = Path.home() / ".local/node-v24.4.0/bin/node"
_CHECKED: dict[str, dict] = {}


def options(evidence):
    """The film's Linkwork options, or None (films without them never see the kind)."""
    return ((evidence or {}).get("curriculum") or {}).get("linkwork") or None


def _node(config, *args, timeout=600):
    root = Path((config or {}).get("linkwork_root") or LINKWORK)
    node = str((config or {}).get("node") or NODE)
    done = subprocess.run([node, str(root / "src" / "cli.ts"), *args], cwd=root, capture_output=True, text=True,
                          timeout=timeout)
    if done.returncode not in (0, 1):
        raise RuntimeError(f"linkwork {args[0]} failed: {done.stderr.strip()[-500:]}")
    return done.stdout


def teaching_text(evidence):
    record = (evidence or {}).get("curriculum") or {}
    parts = [record.get("concept", ""), record.get("summary", ""), record.get("chapter", "")]
    parts += [str(c) for c in record.get("coverage") or []]
    return "\n".join(p for p in parts if p)


def shortlist(config, evidence, linkwork):
    """The verb cards worth showing the planner for this concept (trial verbs included: studios only)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as handle:
        handle.write(teaching_text(evidence))
        handle.flush()
        out = _node(config, "shortlist", handle.name, "--subject", linkwork.get("subject", "physics"), "--all",
                    "--limit", str(linkwork.get("limit", 5)))
    return json.loads(out or "[]")


def planner_options(cards):
    keys = ("name", "about", "use_when", "not_when", "fields", "example")
    return {"verbs": [{k: c[k] for k in keys if k in c} for c in cards]}


def _key(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:20]


def check_specs(config, specs):
    """{key: {errors, duration, facts, scene}} for each spec, compiled and checked by Linkwork (cached)."""
    todo = [{"id": _key(s), "spec": s} for s in specs if _key(s) not in _CHECKED]
    if todo:
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(todo, handle)
            handle.flush()
            for item in json.loads(_node(config, "batch", handle.name, "--every", "3")):
                _CHECKED[item["id"]] = item
    return {_key(s): _CHECKED[_key(s)] for s in specs}


def beat_errors(config, scenes, cards):
    """Errors of the linkwork beats among resolved scenes (with start and end seconds)."""
    beats = [s for s in scenes if s.get("kind") == "linkwork"]
    if not beats:
        return []
    names = {c["name"] for c in cards or []}
    errors, specs = [], []
    for s in beats:
        spec = s.get("linkwork")
        if not isinstance(spec, dict) or not isinstance(spec.get("verb"), str):
            errors.append(f"{s.get('id')}: a linkwork beat needs a 'linkwork' object: a verb spec")
        elif spec["verb"] not in names:
            errors.append(f"{s.get('id')}: verb {spec['verb']!r} is not one of linkwork_options.verbs ({', '.join(sorted(names))})")
        else:
            specs.append(spec)
    results = check_specs(config, specs) if specs else {}
    for s in beats:
        spec = s.get("linkwork")
        if not isinstance(spec, dict) or _key(spec) not in results:
            continue
        r = results[_key(spec)]
        errors += [f"{s['id']}: {e}" for e in r["errors"][:6]]
        length = s["end"] - s["start"]
        if not r["errors"] and r["duration"] > length + 1.5:
            errors.append(f"{s['id']}: its {spec['verb']} lasts {r['duration']:.1f} s but the beat lasts {length:.1f} s: "
                          f"fit every time in the spec inside 0 to {max(1.0, length - 1.5):.1f} s")
    return errors


def render(config, store, films):
    """Save each linkwork beat's checked scene under the project and note its file on the beat."""
    folder = Path(store.root) / "linkwork"
    specs = [s["linkwork"] for film in films for s in film["storyboard"]["scenes"] if s.get("kind") == "linkwork"]
    results = check_specs(config, specs)
    for film in films:
        for s in film["storyboard"]["scenes"]:
            if s.get("kind") != "linkwork":
                continue
            r = results[_key(s["linkwork"])]
            if r["errors"] or not r["scene"]:
                raise ValueError(f"{s['id']}: Linkwork scene failed its checks: {r['errors'][:3]}")
            path = folder / f"{_key(s['linkwork'])}.json"
            write(path, r["scene"])
            s.update(linkwork_file=str(path), linkwork_facts=r["facts"])
