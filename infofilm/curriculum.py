"""Curriculum films: one textbook concept as a short film, rooted in the book.

The curriculum record (built from a textbook digest by History Studio) names the book, chapter and concept, the
audience, and the coverage points the film must teach. Its sources are the book's own text for the concept (S1,
authoritative) and checked additions that fill gaps the book leaves (each with the pages its research fetched).

Rules, enforced in the evidence and script prompts and checked by code:
- every coverage point is carried by at least one evidence fact, and narrated through a point that uses that fact;
- the book is never contradicted; additions only fill what the book leaves out;
- length comes from coverage: a covered concept beats a round duration.
"""
from __future__ import annotations

EVIDENCE_TASK = (
    " This is a curriculum film for {audience}, teaching one textbook concept: {concept} (NCERT {book}, Chapter "
    "{chapter_no} \"{chapter}\"). curriculum.coverage lists what the book teaches about it: every coverage point must "
    "be carried by at least one fact, and each fact lists the coverage ids it carries in \"coverage\". Source S1 is the "
    "textbook itself and is authoritative: keep its facts, dates, names, terms and framing, and never contradict it. "
    "The other sources are checked additions that fill gaps the book leaves (an unexplained term, a missing date, a "
    "person or place it names without explaining); use them only for that, and leave out any claim that disagrees "
    "with the book. Add no opinions of your own. Up to 30 facts are allowed so that the whole concept is covered.")

SCRIPT_TASK = (
    " This is a curriculum film for {audience}, teaching one textbook concept. Coverage overrides the advice above "
    "about omitting subsidiary material: narrate every coverage point in evidence.curriculum.coverage, in a clear "
    "order that follows the book, through points whose fact_ids carry it. Explain in the book's terms and keep its "
    "framing; define each key term when it first appears; never contradict the textbook or add opinions. Open with a "
    "question or scene that makes a student want to know more, and close by drawing the concept together.")


def context(curriculum):
    return {k: curriculum.get(k, '') for k in ('audience', 'concept', 'book', 'chapter_no', 'chapter')}


def evidence_task(curriculum):
    return EVIDENCE_TASK.format(**context(curriculum))


def script_task(curriculum):
    return SCRIPT_TASK.format(**context(curriculum))


def coverage_ids(curriculum):
    return [c['id'] for c in curriculum.get('coverage', [])]


def evidence_coverage_errors(evidence, curriculum):
    """Every coverage point is carried by a fact; facts name only known coverage ids."""
    known = set(coverage_ids(curriculum))
    carried, errors = set(), []
    for fact in evidence.get('facts', []):
        ids = fact.get('coverage') or []
        if not isinstance(ids, list) or any(c not in known for c in ids):
            errors.append(f'{fact.get("id")}: "coverage" must list coverage ids from {sorted(known)}')
            continue
        carried |= set(ids)
    missing = [c for c in coverage_ids(curriculum) if c not in carried]
    if missing:
        points = {c['id']: c['point'] for c in curriculum['coverage']}
        errors.append('these coverage points are carried by no fact: ' +
                      '; '.join(f'{c} ({points[c][:80]})' for c in missing))
    return errors


def script_coverage_errors(script, evidence):
    """Every coverage point is narrated: some point's fact_ids reach a fact that carries it."""
    curriculum = evidence.get('curriculum')
    if not curriculum:
        return []
    by_fact = {f.get('id'): set(f.get('coverage') or []) for f in evidence.get('facts', [])}
    narrated = {c for p in script.get('points', []) for f in p.get('fact_ids') or [] for c in by_fact.get(f, ())}
    missing = [c for c in coverage_ids(curriculum) if c not in narrated]
    if not missing:
        return []
    points = {c['id']: c['point'] for c in curriculum['coverage']}
    facts = {c: [f for f, ids in by_fact.items() if c in ids] for c in missing}
    return ['the narration leaves out coverage points (narrate each through a point using one of its facts): ' +
            '; '.join(f'{c} ({points[c][:80]}; facts {", ".join(facts[c])})' for c in missing)]
