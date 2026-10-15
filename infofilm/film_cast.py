"""Film cast: the named people the film will show, found once from the accepted script(s) and evidence, with
portrait references researched once into the film-wide cache. Image prompts then use these canonical names, so
references are shared and invented names cannot slip in. Long films cast at the phase A gate (every chapter); a short
film is cast as a film of one chapter (SHORT_CHAPTER) just before its image prompts (user, 2026-10-15: in the Salt
March short, batches named Gandhi two ways and no one decided who must be seen)."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .core import ROOT, digest, read, write

TASK = (
    "List the real, named public or historical people this film will visibly show in its illustrations, using the "
    "chapter scripts and verified chapter facts below. Include a person only when the narration or facts name them and "
    "a scene would plausibly depict them; leave out people merely mentioned in passing, groups and unnamed people. "
    "Also leave out private individuals whose stories serve as examples (a victim, a witness, a villager) and experts or "
    "researchers who are only quoted as sources: they are not cast unless the story is about them, and a named "
    "private person's likeness is never generated. A film with no such people has an empty cast. "
    "Use each person's full canonical name exactly as the facts give it (never a guessed first name). birth_year is an "
    "integer (negative for BCE; best known estimate), or null when no reliable year is known. reference_kind is photograph when authentic photographs exist, "
    "historical_portrait for figures before photography. appearance_context gives the life period, age and role in "
    "this film, from the facts only; do not invent injuries, clothing or physical details. "
    "This is a historical documentary: the principal people of the story must be seen, not only named. show_in lists "
    "the chapters (at least one, all from chapters) where a scene must visibly depict the person: give the central "
    "figures several chapters and minor figures the one where they matter most. Choose chapters whose facts support a "
    "non-violent depiction (a meeting, a briefing, a public appearance, a documented place); violent moments stay "
    "indirect, but the people stay in the film. "
    "max_people is a hard budget: list the most important people; a review may ask to swap a less important person "
    "for a more important one, never to exceed the budget. fact_ids name supporting facts as '<chapter id>:<fact id>' "
    "(e.g. 'C01:F03'), using the ids given in each chapter. "
    "Return {people:[{name, birth_year, reference_kind:'photograph'|'historical_portrait', appearance_context, "
    "chapters:[chapter ids], show_in:[chapter ids], fact_ids:['C01:F03', ...]}]}."
)

# Repairable unit: one person, with an id code assigns (R01, R02, ...) so a repair can change the name safely.
CAST_UNITS = {"lists": {"people": "id"}, "assign": {"people": "R"}, "operations": ["people"]}


SHORT_CHAPTER = "F"


def required_in_chapter(cast, chapter_id):
    """Cast names a chapter's images must show. Casts made before show_in existed fall back to the person's first chapter,
    so everyone in the cast is seen at least once."""
    return [p["name"] for p in cast or []
            if chapter_id in (p.get("show_in") or p.get("chapters", [])[:1])]


def _tokens(name):
    return [t for t in re.findall(r"[\w'-]+", name.casefold()) if len(t) > 2 and t not in {"von", "van", "der", "den", "the"}]


def cast_errors(value, corpus, chapter_ids, maximum, fact_refs=None):
    people = value.get("people")
    if not isinstance(people, list) or len(people) > maximum:
        return [f"people must be a list of at most {maximum} people"]
    errors, seen, text = [], set(), corpus.casefold()
    for index, person in enumerate(people):
        if not isinstance(person, dict):
            errors.append(f"people[{index}]: must be an object"); continue
        name = person.get("name")
        label = str(person.get("id") or f"people[{index}]")
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 100:
            errors.append(f"{label}: needs a name"); continue
        missing = [t for t in _tokens(name) if t not in text]
        if missing or not _tokens(name):
            errors.append(f"{label} {name!r}: not named in the chapter scripts or facts ({', '.join(missing)}); use the name as the facts give it or drop the person")
        if name.casefold() in seen:
            errors.append(f"{label} {name!r}: is listed twice")
        seen.add(name.casefold())
        if person.get("birth_year") is not None and (type(person.get("birth_year")) is not int
                                                      or not -3000 <= person["birth_year"] <= 2030):
            errors.append(f"{label} {name!r}: birth_year must be an integer year, or null when unknown")
        if person.get("reference_kind") not in {"photograph", "historical_portrait"}:
            errors.append(f"{label} {name!r}: reference_kind must be photograph or historical_portrait")
        if not isinstance(person.get("appearance_context"), str) or not 5 <= len(person["appearance_context"]) <= 240:
            errors.append(f"{label} {name!r}: appearance_context must be 5-240 characters")
        if not isinstance(person.get("chapters"), list) or not set(person["chapters"]) <= chapter_ids:
            errors.append(f"{label} {name!r}: chapters must be chapter ids")
        elif (not isinstance(person.get("show_in"), list) or not person["show_in"]
              or not set(person["show_in"]) <= set(person["chapters"])):
            errors.append(f"{label} {name!r}: show_in must list at least one of the person's chapters "
                          "where a scene depicts them")
        if fact_refs is not None:
            bad = [f for f in person.get("fact_ids") or [] if f not in fact_refs]
            if not isinstance(person.get("fact_ids"), list) or bad:
                errors.append(f"{label} {name!r}: fact_ids must be '<chapter id>:<fact id>' pairs from the chapters"
                              + (f"; not found: {', '.join(map(str, bad[:5]))}" if bad else ""))
    return errors


def make_cast(models, records, maximum, experimental=True, planned_people=None):
    chapters, corpus = [], []
    for row in records:
        project = Path(row["project"])
        script = project / f"film-{row['duration']}" / "script.json"
        evidence = project / "evidence-final.json"
        if not script.exists() or not evidence.exists():
            continue
        points = [p["narration"] for p in read(script)["points"]]
        facts = [{"id": f["id"], "claim": f["claim"]} for f in read(evidence)["facts"]]
        chapters.append({"id": row["chapter"]["id"], "title": row["chapter"]["title"], "narration": points, "facts": facts})
        corpus += points + [f["claim"] for f in facts]
    if not chapters:
        return []
    ids = {c["id"] for c in chapters}
    refs = {f"{c['id']}:{f['id']}" for c in chapters for f in c["facts"]}
    text = "\n".join(corpus)
    context = {"chapters": chapters, "max_people": maximum}
    if planned_people:
        context["planned_people_by_chapter"] = planned_people          # from the central beat sheet
    value = models.loop("film-cast", TASK, context,
                        lambda v: cast_errors(v, text, ids, maximum, refs), experimental=experimental, units=CAST_UNITS)
    return value["people"]


def research_cast(config, store, people):
    """Portrait references for the whole cast, once, into the film's person-reference cache (shared by chapters)."""
    from .llm_settings import llm_config
    from .person_references import person_key, reference_settings
    settings = reference_settings(config)
    if not settings["enabled"] or not people:
        return None
    unique = {}
    for person in people[: settings["max_people"]]:
        entry = {k: person[k] for k in ("name", "appearance_context", "reference_kind", "birth_year")}
        unique[person_key(entry)] = entry
    cache_root = store.root / "person-references"
    archive = cache_root / "requests" / ("cast-" + digest({"people": unique, "settings": settings})[:16])
    if not (archive / "result.json").exists():
        request = write(archive / "request.json", {"people": unique, "settings": settings, "directory": str(archive),
                                                   "cache_root": str(cache_root), "project": str(store.root),
                                                   "model_config": llm_config(config)})
        interpreter = Path(config.get("person_reference_python", sys.executable))
        subprocess.run([str(interpreter), "-u", "-m", "infofilm.person_reference_worker", str(request)], cwd=ROOT, check=True)
    return read(archive / "result.json")["people"]


def curriculum_people(evidence):
    """People a textbook concept names (its coverage points), as planned people for the cast."""
    points = ((evidence.get("curriculum") or {}).get("record") or evidence.get("curriculum") or {}).get("coverage") or []
    return [p["point"][len("Person: "):].split(" - ")[0].strip() for p in points
            if isinstance(p, dict) and str(p.get("point", "")).startswith("Person: ")]


def cast_short(config, store, models, film, evidence, *, experimental=True):
    """The long films' cast step for a short film, treated as one chapter: every cast member has show_in [F], so each
    is depicted at least once. Reuses a saved cast. Never blocks the film: on failure the prompts go uncast, as before."""
    from .person_references import reference_settings
    settings = reference_settings(config)
    if not settings["enabled"]:
        return None
    path = store.root / "film-cast.json"
    try:
        store.event("cast", "running", {"scope": "short"})
        if path.exists():
            people = read(path)["people"]
        else:
            record = {"project": str(store.root), "duration": film["requested_seconds"],
                      "chapter": {"id": SHORT_CHAPTER, "title": film["script"].get("subject") or "Film"}}
            planned = curriculum_people(evidence)
            people = make_cast(models, [record], settings["max_people"], experimental=experimental,
                               planned_people={SHORT_CHAPTER: planned} if planned else None)
            write(path, {"people": people})
        references = research_cast(config, store, people) or {}
        store.event("cast", "succeeded", {"people": [p["name"] for p in people],
                    "referenced": sum(r.get("status") == "ready" for r in references.values())})
        return people
    except Exception as exc:                       # noqa: BLE001 - the cast improves images; it never stops a film
        store.event("cast", "failed", {"error": f"{type(exc).__name__}: {exc}"[:300]})
        return None
