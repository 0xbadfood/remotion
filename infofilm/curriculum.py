"""Curriculum films: one textbook concept as a short film, rooted in the book.

The curriculum record (built from a textbook digest by History Studio) names the book, chapter and concept, the
audience, and the coverage points the film must teach. Its sources are the book's own text for the concept (S1,
authoritative) and checked additions that fill gaps the book leaves (each with the pages its research fetched).

Rules, enforced in the evidence and script prompts and checked by code:
- every coverage point is carried by at least one evidence fact, and narrated through a point that uses that fact;
- the book is never contradicted; additions only fill what the book leaves out;
- length comes from coverage: a covered concept beats a round duration.

A key-idea film (kind "key_idea", user 2026-10-20) is the second level: after the concept film, one of its key ideas as
a short simplified film. Simplification comes first: analogies, a playful style and cartoon characters are welcome,
completeness is not asked for, and the book's style may be left behind. Two things still hold: every number or
equation shown is correct, and the film must not leave a wrong idea behind. Whether the idea lands is checked by a
learner: a model that knows nothing of the subject reads the narration, explains the idea back and answers questions,
and a reviewer compares that with the idea (learner_check).
"""
from __future__ import annotations

import json

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


KEY_IDEA_EVIDENCE_TASK = (
    " This brief is for a short simplified film for {audience} on one key idea: {idea} (from \"{concept}\", NCERT "
    "{book}, Chapter {chapter_no} \"{chapter}\"). The learner should come away understanding: {understanding} "
    "Source S1 is the textbook: its facts, numbers and terms are authoritative. Other sources may show how others "
    "explain the idea simply: record their best analogies, everyday examples and pictures as facts too (say in the "
    "claim that it is an analogy and what it stands for), and record misconceptions learners commonly form. Leave out "
    "anything not needed to understand this one idea; completeness is not wanted.")

KEY_IDEA_SCRIPT_TASK = (
    " This is a short simplified film for {audience} on ONE key idea: {idea}. The learner should come away "
    "understanding: {understanding} Simplification comes before everything else, including the advice above about a "
    "general audience, coverage and the book's style. Build the film around one strong analogy or everyday picture "
    "(from the evidence, or your own when it fits better), develop it step by step, then map it onto the real thing "
    "and name the book's terms once the picture is clear. Be creative and warm: a question the learner actually has, "
    "a friendly guide's voice, short sentences, one idea per point, a 'so that is why' moment, and a short recap at "
    "the end. Leave out whatever the idea does not need. Say where the analogy stops working in one light sentence, "
    "so it leaves no wrong belief. Use numbers or equations only when they help the idea land, and then exactly as "
    "the evidence gives them. Never state something false to make it simpler.")

KEY_IDEA_CREATIVE_TASK = (
    " This is a short simplified explainer of one key idea for students. Choose a warm, playful, curious mood and a "
    "bright, friendly visual style; cartoon illustration is welcome and a recurring friendly cartoon guide character "
    "(describe it exactly once in visual_style so every image draws the same character) can appear in image scenes. "
    "Keep a dark theme background so on-screen text stays readable.")

KEY_IDEA_BEATS_TASK = (
    " This film simplifies one key idea: show the analogy and the real thing, ideally side by side or one turning "
    "into the other. Prefer animation beats for every process and change; image beats can show the analogy's "
    "everyday world and the cartoon guide character. Few words on screen; at most one equation, built up slowly.")


def is_key_idea(curriculum):
    return (curriculum or {}).get('kind') == 'key_idea'


def context(curriculum):
    return {k: curriculum.get(k, '') for k in ('audience', 'concept', 'book', 'chapter_no', 'chapter')}


def idea_context(curriculum):
    idea = curriculum.get('idea') or {}
    return {**context(curriculum), 'idea': idea.get('title', ''), 'understanding': idea.get('understanding', '')}


def evidence_task(curriculum):
    if is_key_idea(curriculum):
        return KEY_IDEA_EVIDENCE_TASK.format(**idea_context(curriculum))
    return EVIDENCE_TASK.format(**context(curriculum))


def script_task(curriculum):
    if is_key_idea(curriculum):
        return KEY_IDEA_SCRIPT_TASK.format(**idea_context(curriculum))
    return SCRIPT_TASK.format(**context(curriculum))


# ------------------------------------------------------------------ does the idea land?
LEARNER_TASK = (
    "You are a {audience_short} who has never studied this topic. Below is the narration of a short film, and nothing "
    "else. Using only what the film said, explain its main idea in your own words (3-5 sentences, as a student would), "
    "then answer each question in one or two sentences, and say where you got lost, if anywhere. Do not use outside "
    "knowledge. Return {{explanation, answers:[{{question, answer}}], lost_at:[string]}}.")

JUDGE_TASK = (
    "A film tries to make one idea land for students: {idea}. Understanding wanted: {understanding} A learner who "
    "had only the film's narration explained it back and answered questions (learner below). Judge whether the idea "
    "landed: the learner's explanation captures the understanding wanted, and the learner holds no wrong belief "
    "(for example an analogy taken literally). Do NOT ask for completeness, more facts or more of the book. Suggest "
    "simplifications that would help: a clearer analogy, a missing step, a sentence that confused the learner. "
    "Return {{landed:boolean, score:0..100, wrong_beliefs:[string], fixes:[{{point_id:'P03', problem, suggestion}}]}}.")


def learner_check(models, script, curriculum, name):
    """The learner's explanation and the judge's verdict on whether the key idea landed."""
    idea = curriculum.get('idea') or {}
    narration = '\n'.join(f'{p["id"]}: {p["narration"]}' for p in script['points'])
    questions = idea.get('questions') or [f'What is the main idea of "{idea.get("title", "")}"?']
    learner = models.call('planner', f'{name}-learner', LEARNER_TASK.format(
        audience_short=str(curriculum.get('audience', 'student')).split(' in ')[0].rstrip('s').lower() or 'student')
        + '\nQUESTIONS:\n' + '\n'.join(f'- {q}' for q in questions) + '\nNARRATION:\n' + narration, max_tokens=3000)
    verdict = models.call('reviewer', f'{name}-judge', JUDGE_TASK.format(
        idea=idea.get('title', ''), understanding=idea.get('understanding', ''))
        + '\nNARRATION:\n' + narration + '\nLEARNER:\n' + json.dumps(learner, ensure_ascii=False), max_tokens=3000)
    return {'learner': learner, 'verdict': verdict,
            'landed': bool(verdict.get('landed')) and not verdict.get('wrong_beliefs')}


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
