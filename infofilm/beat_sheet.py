"""Central beat sheet for long films (BUILD NEXT item 3, option C).

After every chapter's research and evidence and before any chapter script, one central planner call sees all
chapters' facts and fixes, per chapter: the beats (each tied to that chapter's fact ids), what is revealed where (no
spoilers ahead of their chapter, no repeats anywhere in the film), the handoffs, the people seen on screen, and a word
budget that code computes from the narrator's measured speaking rate. Chapter scripts are then written in parallel,
each from its own beats; the continuity gate becomes a light check.

Units (repair_units): each chapter's frame (C01: opening, closing, people) and each beat (B01.., code-assigned ids,
anchored to its chapter), so a finding about one beat repairs one beat.
"""
from __future__ import annotations

from pathlib import Path

from .core import read

UNITS = {"lists": {"chapters": "id", "beats": "id"}, "assign": {"beats": "B"},
         "anchors": {"chapters": ["id", "word_budget"], "beats": ["chapter"]}, "operations": ["beats"]}

TASK = (
    "Plan the beats of one long documentary film, chapter by chapter, before any narration is written. You see every "
    "chapter's direction (role, entry context, exit handoff) and its verified facts. For each chapter, list beats in "
    "narration order: one beat is one narrative step (an event, explanation or turning point) supported by that "
    "chapter's own fact ids, with a short intent saying what the viewer learns. "
    "Across the whole film: reveal each event, outcome or verdict in the chapter that owns it, never earlier (no "
    "spoilers of later chapters); never tell the same fact, event or explanation in two chapters (a later chapter may "
    "refer back in a few words, marked as a reminder); every chapter opens from the previous chapter's closing and "
    "closes into the next chapter's opening. Only the first chapter introduces the film and only the last concludes it. "
    "word_budget per chapter is fixed by code from the narrator's measured speaking rate: allocate words to beats so "
    "that each chapter's beats sum to its budget; choose fewer, fuller beats rather than cramming. "
    "people lists the named real people a scene in that chapter should show (names exactly as the facts give them). "
    "Return {chapters:[{id, opening:string, closing:string, people:[string]}], "
    "beats:[{chapter:chapter id, intent:string, fact_ids:[that chapter's fact ids], words:integer, reminder:boolean}]} "
    "with chapters in the given order and beats grouped by chapter in narration order."
)


def chapter_inputs(plan, records, rate):
    """Per chapter: direction, facts and the code-computed word budget."""
    rows = []
    by_id = {row["chapter"]["id"]: row for row in records}
    for chapter in plan["chapters"]:
        row = by_id.get(chapter["id"])
        if row is None:
            continue
        evidence_path = Path(row["project"]) / "evidence-final.json"
        evidence = read(evidence_path) if evidence_path.exists() else {"facts": [], "uncertainties": []}
        rows.append({"id": chapter["id"], "title": chapter["title"], "objective": chapter.get("objective"),
                     "opening_role": chapter.get("opening_role"), "ending_role": chapter.get("ending_role"),
                     "entry_context": chapter.get("entry_context"), "exit_handoff": chapter.get("exit_handoff"),
                     "exclude": chapter.get("exclude"),
                     "word_budget": max(40, round(chapter.get("advisory_seconds", 120) * rate / 60)),
                     "facts": [{"id": f["id"], "claim": f["claim"]} for f in evidence.get("facts", [])],
                     "uncertainties": evidence.get("uncertainties", [])})
    return rows


def beat_errors(value, chapters, tolerance):
    errors = []
    expected = [c["id"] for c in chapters]
    frames = value.get("chapters")
    if not isinstance(frames, list) or [f.get("id") if isinstance(f, dict) else None for f in frames] != expected:
        return ["chapters must list exactly " + " ".join(expected) + " in this order"]
    beats = value.get("beats")
    if not isinstance(beats, list) or not beats or any(not isinstance(b, dict) for b in beats):
        return ["beats must be a non-empty list of objects"]
    facts = {c["id"]: {f["id"] for f in c["facts"]} for c in chapters}
    budgets = {c["id"]: c["word_budget"] for c in chapters}
    for frame in frames:
        for key in ("opening", "closing"):
            if not isinstance(frame.get(key), str) or not 5 <= len(frame[key]) <= 400:
                errors.append(f"{frame['id']}: {key} must be 5-400 characters")
        if not isinstance(frame.get("people", []), list):
            errors.append(f"{frame['id']}: people must be a list of names")
    order = []
    for beat in beats:
        label, chapter = str(beat.get("id") or "beat"), beat.get("chapter")
        if chapter not in facts:
            errors.append(f"{label}: chapter must be one of {' '.join(expected)}"); continue
        order.append(chapter)
        if not isinstance(beat.get("intent"), str) or not 5 <= len(beat["intent"]) <= 300:
            errors.append(f"{label}: intent must be 5-300 characters")
        ids = beat.get("fact_ids")
        if not isinstance(ids, list) or not ids or not set(ids) <= facts[chapter]:
            errors.append(f"{label}: fact_ids must be one or more of {chapter}'s own fact ids")
        if type(beat.get("words")) is not int or not 10 <= beat["words"] <= 400:
            errors.append(f"{label}: words must be an integer 10-400")
    if order != sorted(order, key=expected.index):
        errors.append("beats must be grouped by chapter in chapter order")
    for chapter in expected:
        own = [b for b in beats if b.get("chapter") == chapter]
        if len(own) < 2:
            errors.append(f"{chapter}: needs at least two beats")
        total = sum(b["words"] for b in own if type(b.get("words")) is int)
        if own and abs(total - budgets[chapter]) > budgets[chapter] * tolerance:
            errors.append(f"{chapter}: beats allocate {total} words; its budget is {budgets[chapter]} (±{round(tolerance * 100)}%)")
    return errors


def make_beat_sheet(models, plan, records, rate, *, tolerance=0.15, experimental=True):
    chapters = chapter_inputs(plan, records, rate)
    context = {"film": {k: v for k, v in plan["film"].items() if k not in {"advisory_seconds"}},
               "narrator_words_per_minute": round(rate, 1), "chapters": chapters}
    value = models.loop("beat-sheet", TASK, context, lambda v: beat_errors(v, chapters, tolerance),
                        experimental=experimental, units=UNITS)
    budgets = {c["id"]: c["word_budget"] for c in chapters}
    for frame in value["chapters"]:
        frame["word_budget"] = budgets[frame["id"]]          # code-owned, whatever the planner wrote
    return value


def chapter_slice(sheet, chapter_id):
    """What one chapter's script is written from."""
    if not sheet:
        return None
    frame = next((f for f in sheet["chapters"] if f["id"] == chapter_id), None)
    if frame is None:
        return None
    return {"opening": frame["opening"], "closing": frame["closing"], "people": frame.get("people", []),
            "word_budget": frame["word_budget"], "length_tolerance": frame.get("length_tolerance", 0.15),
            "beats": [{k: b.get(k) for k in ("id", "intent", "fact_ids", "words", "reminder")}
                      for b in sheet["beats"] if b.get("chapter") == chapter_id]}
