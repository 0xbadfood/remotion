"""Film cast for long films (phase A gate): the named people the film will show, found once from every chapter's
accepted script and evidence, with portrait references researched once into the film-wide cache. Chapter image
prompts then use these canonical names, so references are shared and invented names cannot slip in."""
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
    "Use each person's full canonical name exactly as the facts give it (never a guessed first name). birth_year is an "
    "integer (negative for BCE; best known estimate). reference_kind is photograph when authentic photographs exist, "
    "historical_portrait for figures before photography. appearance_context gives the life period, age and role in "
    "this film, from the facts only; do not invent injuries, clothing or physical details. "
    "This is a historical documentary: the principal people of the story must be seen, not only named. show_in lists "
    "the chapters (at least one, all from chapters) where a scene must visibly depict the person: give the central "
    "figures several chapters and minor figures the one where they matter most. Choose chapters whose facts support a "
    "non-violent depiction (a meeting, a briefing, a public appearance, a documented place); violent moments stay "
    "indirect, but the people stay in the film. "
    "Return {people:[{name, birth_year, reference_kind:'photograph'|'historical_portrait', appearance_context, "
    "chapters:[chapter ids], show_in:[chapter ids], fact_ids:[supporting fact ids]}]}."
)


def required_in_chapter(cast, chapter_id):
    """Cast names a chapter's images must show. Casts made before show_in existed fall back to the person's first chapter,
    so everyone in the cast is seen at least once."""
    return [p["name"] for p in cast or []
            if chapter_id in (p.get("show_in") or p.get("chapters", [])[:1])]


def _tokens(name):
    return [t for t in re.findall(r"[\w'-]+", name.casefold()) if len(t) > 2 and t not in {"von", "van", "der", "den", "the"}]


def cast_errors(value, corpus, chapter_ids, maximum):
    people = value.get("people")
    if not isinstance(people, list) or len(people) > maximum:
        return [f"people must be a list of at most {maximum} people"]
    errors, seen, text = [], set(), corpus.casefold()
    for index, person in enumerate(people):
        if not isinstance(person, dict):
            errors.append(f"people[{index}] must be an object"); continue
        name = person.get("name")
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 100:
            errors.append(f"people[{index}] needs a name"); continue
        missing = [t for t in _tokens(name) if t not in text]
        if missing or not _tokens(name):
            errors.append(f"people[{index}] {name!r}: not named in the chapter scripts or facts ({', '.join(missing)}); use the name as the facts give it or drop the person")
        if name.casefold() in seen:
            errors.append(f"people[{index}] {name!r} is listed twice")
        seen.add(name.casefold())
        if type(person.get("birth_year")) is not int or not -3000 <= person["birth_year"] <= 2030:
            errors.append(f"people[{index}] {name!r}: birth_year must be an integer year")
        if person.get("reference_kind") not in {"photograph", "historical_portrait"}:
            errors.append(f"people[{index}] {name!r}: reference_kind must be photograph or historical_portrait")
        if not isinstance(person.get("appearance_context"), str) or not 5 <= len(person["appearance_context"]) <= 240:
            errors.append(f"people[{index}] {name!r}: appearance_context must be 5-240 characters")
        if not isinstance(person.get("chapters"), list) or not set(person["chapters"]) <= chapter_ids:
            errors.append(f"people[{index}] {name!r}: chapters must be chapter ids")
        elif (not isinstance(person.get("show_in"), list) or not person["show_in"]
              or not set(person["show_in"]) <= set(person["chapters"])):
            errors.append(f"people[{index}] {name!r}: show_in must list at least one of the person's chapters "
                          "where a scene depicts them")
    return errors


def make_cast(models, records, maximum, experimental=True):
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
    text = "\n".join(corpus)
    value = models.loop("film-cast", TASK, {"chapters": chapters, "max_people": maximum},
                        lambda v: cast_errors(v, text, ids, maximum), experimental=experimental)
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
