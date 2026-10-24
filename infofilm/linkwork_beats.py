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
    " This film is drawn VECTOR FIRST: exact, code-made Linkwork scenes (kind:'linkwork') are the default picture, and "
    "an image is the exception that must earn its place. For every beat, ask first whether a verb in "
    "linkwork_options.verbs, built from the props in linkwork_options.props, can show what the passage says: an "
    "object moving along a line with its position, displacement and velocity (a car on a road is a prop doing physics, "
    "not an everyday scene); a car whose motion follows the book's formula, with a speedometer; path length against "
    "displacement on a route; an object's size against the distance it moves; a graph of one quantity against "
    "another; a mechanics situation acted out with props (a ball fired from a cannon, a block sliding or a ball "
    "rolling down a wedge, two carts colliding); and the general props, which belong in any beat: a person thinking "
    "as a bulb lights over their head (a question, a new idea), a stopwatch running (time, an interval), a "
    "speedometer reading a moving car (speed at an instant). If one can, the beat is a Linkwork beat. Use an image "
    "only for what no prop can be: real people in real places, history, a real laboratory or apparatus, nature, the "
    "feel of a place; never for a placeholder, an abstract mood, a notebook, a board, a dashboard or a road standing "
    "in for a graph. A linkwork beat has a heading and summary like other beats, empty labels/details/image_brief, and "
    "a 'linkwork' object that is the verb's spec, written as its card's fields and example show (start from the "
    "example). Times in the spec are seconds from the start of the beat, and its motion must end within the beat's "
    "own duration (the sentence timings show how long it lasts). Use only numbers the narration states or that follow "
    "from them: the numbers it draws are computed from the spec and shown on screen. When the passage states no "
    "numbers (a definition, a shape, a direction, a comparison: a car speeding up, a curve bending upwards, a body "
    "slowing down), draw it anyway with simple values that give the right shape and set illustrative:true in the "
    "spec: the scene keeps its motion, shapes and arrows and hides every number, so nothing on screen claims a value "
    "(a passage with no numbers is never a reason for an image). When the narration sets the same "
    "thing in two situations (a train beside a platform, then on a long route; a bus on its route, then in a parking "
    "bay), show both in ONE beat as the verb's cases, so the contrast is on screen together, and draw each as the "
    "narration names it (the case's setting). Code checks every spec and returns its errors. Reviewing: report as "
    "major an image beat that a listed verb with the listed props could have drawn, naming the verb and props. A "
    "spec with illustrative:true shows no numbers: its values only shape the picture and are not claims, so never "
    "report them; report only a shape or direction that contradicts the narration.")

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


def props(config):
    """{name: what it is} for every prop Linkwork draws exactly (objects and the walking figure)."""
    return json.loads(_node(config, "objects", "--json") or "{}")


PROPS_TASK = (
    " This film can draw some things exactly, in code, instead of as generated pictures: context.props lists them "
    "(name: what it is and does). A world built from them is drawn exactly, plays as light vector scenes and needs no "
    "image retries, so build the analogy, the guide and the setting from props whenever one can carry the idea as "
    "well (a car or a walker on a marked road rather than a bird; a ball, a block, a train, a pulley; a person thinking "
    "as a bulb lights for the moment of insight, a stopwatch for time, a speedometer for speed), and name the "
    "props the plan uses in world.props:[names]. A guide that is the figure prop is Linkwork's plain stick figure, "
    "exactly as drawn (it stands, walks, thinks, points, cheers and shrugs; a bulb can light over its head): give it "
    "no clothes, hats, scarves or colours it cannot have, and every beat with the guide is then drawn by code. Use a world that only a generated picture can show only when no prop "
    "can carry the analogy. Reviewing: report as major a guide character, setting or analogy object that is not a "
    "prop when a listed prop could play the same part just as well, naming the prop.")


def planner_options(cards, config=None):
    """The verbs the planner may use, and the props it can build them from (name: what it is and does)."""
    keys = ("name", "about", "use_when", "not_when", "fields", "example")
    out = {"verbs": [{k: c[k] for k in keys if k in c} for c in cards]}
    if config is not None:
        try:
            out["props"] = props(config)
        except Exception:                         # noqa: BLE001 - the verbs' own fields still name their props
            pass
    return out


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


FIT = 1.6           # a scene up to this much longer than its beat plays faster to fit it (a graph's own pacing)
TAIL = 1.5          # and every scene may run this far past its beat (its last frame holds; the cut ends it)


def speed_to_fit(scene_seconds, beat_seconds):
    """How much faster a Linkwork scene plays so its motion ends inside its beat: 1 when it fits, at most FIT."""
    if beat_seconds <= 0 or scene_seconds <= beat_seconds + TAIL:
        return 1.0
    return round(min(FIT, (scene_seconds - TAIL) / beat_seconds), 3)


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
        # A scene a little long plays faster (speed_to_fit); one far too long cannot, and the planner is told the
        # levers it really has: a longer beat, or less to show (a graph's own drawing pace has no time to shorten).
        if not r["errors"] and r["duration"] > FIT * length + TAIL:
            errors.append(f"{s['id']}: its {spec['verb']} needs about {r['duration'] - TAIL:.1f} s but the beat is only "
                          f"{length:.1f} s: join this passage with the next sentence for a longer beat, or show less (fewer "
                          f"steps, cases or events; any times in the spec inside 0 to {max(1.0, FIT * length):.1f} s)")
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
