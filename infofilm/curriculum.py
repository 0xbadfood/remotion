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
import re

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
    " This is a short simplified film for {audience} on ONE key idea: {idea}. The learner should come away able to "
    "say: {understanding} Simplification comes before everything else, including the advice above about a general "
    "audience, coverage and the book's style. Build the whole film on the plan's one analogy (evidence.idea_plan."
    "analogy; add no second analogy) and let it breathe: introduce it, play it out step by step in everyday words, "
    "let the learner feel the 'aha', and only then map it onto the real thing. Book terms: only {terms}, each "
    "introduced after the picture is clear and explained in everyday words; use no other technical word, symbol or "
    "unit. The analogy carries most of the film, not just the opening. Be creative and warm: a question the learner "
    "actually has, a friendly guide's voice, short sentences, one idea per point, a 'so that is why' moment, and a "
    "short recap. Leave out whatever the idea does not need: no tour of the model's limits, other theories or extra "
    "results. Say where the analogy stops working in one light sentence, so it leaves no wrong belief. Equations and "
    "numbers are rarely needed here: at most {equations} point may speak an equation, a calculation or a measured "
    "number, and only if it helps the idea land, exactly as the evidence gives it. Never state something false to "
    "make it simpler. Each point also carries uses_analogy: true when the analogy carries or is mapped in that "
    "point; at least half the points must use it. evidence.idea_plan is the film's plan: narrate its beats in order "
    "(a beat may take one or two points), with its analogy and mapping; keep to it.")

KEY_IDEA_CREATIVE_TASK = (
    " This is a short simplified explainer of one key idea for students. Choose a warm, playful, curious mood and a "
    "bright, friendly visual style; cartoon illustration is welcome and a recurring friendly cartoon guide character "
    "(describe it exactly once in visual_style so every image draws the same character) can appear in image scenes. "
    "Keep a dark theme background so on-screen text stays readable. evidence.idea_plan.world fixes the guide "
    "character, the analogy's setting and the drawing style: copy them into visual_style exactly.")

KEY_IDEA_BEATS_TASK = (
    " This film simplifies one key idea (evidence.idea_plan): show the analogy happening, then the real thing, "
    "ideally side by side or one turning into the other. Choose each beat's medium for what it must show. Motion: "
    "an image beat may carry action:'what visibly happens during the beat, in one or two plain sentences' (a video "
    "model animates the still from its first frame for the length of the beat, so image_brief describes the moment "
    "before the action, in the light, uncluttered world of evidence.idea_plan.world, with the guide character "
    "exactly as described there); use motion for the analogy's world: climbing, jumping, throwing, glowing. An "
    "action beat lasts 3-10 seconds of narration; split longer passages into several beats. Animation beats (coded, "
    "exact) show only the real thing: real molecules, atoms, energy levels, spectra, quantities, wherever shapes, "
    "positions or numbers must be right. A coder draws shapes and molecules, not people: never put the guide, the "
    "analogy's characters or its setting in an animation beat (those are image and motion beats). Each animation "
    "names the real thing it shows in animation.real (e.g. 'ethanol as framework, ball-and-stick and space-filling "
    "models', 'hydrogen energy levels n=1 to 4'). Once the analogy has played out, show the real thing it maps to in "
    "at least one animation beat, ideally the moment the analogy turns into it. Plain image beats: "
    "a still moment. Few words on screen; at most one equation figure in the whole film, built up slowly. Keep "
    "every image_brief simple: the guide in one pose in the plan's setting, nothing more. Never ask an image for "
    "brackets, dashed lines, arrows, labels, numbers, counted steps or measured gaps: an image model cannot draw them "
    "exactly; anything exact belongs in an animation beat.")

KEY_IDEA_IMAGE_TASK = (
    " This is a simplified key-idea film. Every image is one simple moment in the plan's world (creative_brief.world): "
    "draw the guide character and the setting exactly as described there, on its dark plain background, the guide "
    "large and clear. Do not ask for brackets, dashed lines, arrows, labels, numbers, an exact count of steps or exact "
    "positions. Acceptance criteria only check what matters here: the guide looks as described, the setting is the "
    "described one, the background is dark and plain, and the moment reads at a glance (for a motion scene: the "
    "moment before the action, with room for it). Never make acceptance depend on exact positions, counts or marks.")


KEY_IDEA_EQUATIONS = 1           # points that may speak an equation, calculation or measured number
KEY_IDEA_ANALOGY_SHARE = .5      # points that must use the analogy
KEY_IDEA_FIGURES = 1             # equation figures on screen
SPOKEN_MATHS = re.compile(r"\b(equals|divided by|to the power|times 10|squared|multiplied by|joules?|hertz|"
                          r"nanometres?|nanometers?)\b|=|\d+\.\d+", re.I)


def is_key_idea(curriculum):
    return (curriculum or {}).get('kind') == 'key_idea'


def context(curriculum):
    return {k: curriculum.get(k, '') for k in ('audience', 'concept', 'book', 'chapter_no', 'chapter')}


def idea_context(curriculum):
    idea = curriculum.get('idea') or {}
    terms = idea.get('terms')
    return {**context(curriculum), 'idea': idea.get('title', ''), 'understanding': idea.get('understanding', ''),
            'terms': ', '.join(f'"{t}"' for t in terms) if terms else 'the two or three the idea cannot do without'}


def evidence_task(curriculum):
    if is_key_idea(curriculum):
        return KEY_IDEA_EVIDENCE_TASK.format(**idea_context(curriculum))
    return EVIDENCE_TASK.format(**context(curriculum))


# Alkenes concept film (2026-10-21): P25 read "(CH3)3C-OH" aloud; the spoken form became "ch three 3c oh", three
# takes failed the audio check and the film stopped. Seven points read condensed formulas, two narrated the book's
# misprints. Narration is heard: names are spoken, formulas are shown.
HEARD_TASK = (
    " The narration is heard, not read. Say compounds by their names (2-methylpropan-2-ol, also called tert-butyl "
    "alcohol) and never read a condensed, bracketed or bonded formula aloud (CH3-CHCl-CH2Cl, (CH3)3C-OH, CH2=CH2, "
    "H-Cl): show such formulas on screen in the scene instead; a short formula said as letters is fine (HBr, H2O, CO2, "
    "Cl2, H2SO4). Teach the science as the book intends it: never narrate the book's misprints or corrections to it "
    "('printed as', 'a correction', 'should be read as'); where the book has a slip, simply say the correct version.")
FORMULA_TOKEN = re.compile(r"(?:[A-Z][a-z]?\d*|[()\[\]]\d*|[-=≡–—])+")
ELEMENT_GROUP = re.compile(r"[A-Z][a-z]?")
ERRATA = re.compile(r"\b(misprint\w*|errat(?:a|um)|printed as|printed equation|a correction|correction to|"
                    r"should be read)\b", re.I)


def spoken_formula(token):
    """A formula no listener can follow: bracketed, bonded (-, =) or more than three element groups (CH3CH2OH);
    short ones said as letters (HBr, H2O, CO2, H2SO4, KMnO4) pass."""
    token = token.strip('.,;:!?\'"')
    if not token or not FORMULA_TOKEN.fullmatch(token) or not token[0].isupper() and token[0] not in '([':
        return False
    groups = ELEMENT_GROUP.findall(token)
    if len(groups) < 2 and not re.search(r"[()\[\]]", token):
        return False
    if not re.search(r"\d|[()\[\]=≡]|(?<=[A-Za-z\d])[-–—](?=[A-Z(])", token):
        return False                                  # capitalised words and acronyms (UV, NCERT) are not formulas
    return bool(re.search(r"[()\[\]=≡]|[-–—]", token)) or len(groups) > 3


def heard_errors(script, evidence):
    """Curriculum films: no unspeakable formulas and no narrated misprints."""
    if not evidence.get('curriculum'):
        return []
    out = []
    for p in script.get('points') or []:
        text = str(p.get('narration', ''))
        bad = [t.strip('.,;:!?') for t in text.split() if spoken_formula(t)]
        if bad:
            out.append(f'{p.get("id")}: say the compound\'s name instead of reading {", ".join(bad[:3])} aloud; '
                       'show the formula on screen')
        hit = ERRATA.search(text)
        if hit:
            out.append(f'{p.get("id")}: do not narrate the book\'s misprints ("{hit.group(0)}"); just teach the '
                       'correct version')
    return out


def script_task(curriculum):
    if is_key_idea(curriculum):
        return KEY_IDEA_SCRIPT_TASK.format(**idea_context(curriculum), equations=KEY_IDEA_EQUATIONS) + HEARD_TASK
    return SCRIPT_TASK.format(**context(curriculum)) + HEARD_TASK


def key_idea_script_errors(script, evidence):
    """Key-idea films: few spoken equations, and the analogy carries at least half the points."""
    if not is_key_idea(evidence.get('curriculum')):
        return []
    points = script.get('points') or []
    errors = []
    maths = [p.get('id') for p in points if SPOKEN_MATHS.search(str(p.get('narration', '')))]
    if len(maths) > KEY_IDEA_EQUATIONS:
        errors.append(f'{", ".join(maths)}: {len(maths)} points speak equations or calculations; at most '
                      f'{KEY_IDEA_EQUATIONS} may. Tell the rest through the analogy in everyday words')
    analogy = [p for p in points if p.get('uses_analogy') is True]
    if points and len(analogy) < KEY_IDEA_ANALOGY_SHARE * len(points):
        errors.append(f'only {len(analogy)} of {len(points)} points use the analogy (uses_analogy: true); at least '
                      f'half must: let the analogy carry the explanation, not only open it')
    return errors


# Molecular-models key idea (2026-10-21): coded beats asked to animate "friends" and "hand-holds" drew grey circles
# that were neither the analogy's world nor a real molecule. Animation beats carry the real thing; the analogy's
# world is drawn by the image and motion beats.
ANALOGY_WORLD = re.compile(r"\b(guide|robot|friends?|characters?|cartoon|people|person|kids?|children|hand-?holds?|"
                           r"holding hands|playground)\b", re.I)


def key_idea_animation_errors(scenes):
    """Key-idea animation beats: each names the real thing it shows and draws no analogy character or setting."""
    out = []
    for scene in scenes or []:
        if scene.get('kind') != 'animation' or not isinstance(scene.get('animation'), dict):
            continue
        spec = scene['animation']
        if not isinstance(spec.get('real'), str) or len(spec['real'].split()) < 2:
            out.append(f'{scene.get("id")}: say in animation.real which real thing this animation shows (a molecule, '
                       'atoms, energy levels, a quantity); the analogy belongs to image and motion beats')
        words = ' '.join(str(spec.get(k, '')) for k in ('title', 'objective', 'visual', 'real'))
        hit = ANALOGY_WORLD.search(words)
        if hit:
            out.append(f'{scene.get("id")}: an animation beat draws the real thing, not the analogy ("{hit.group(0)}"); '
                       'show the molecule or quantity it stands for, or make this an image or motion beat')
    return out


def key_idea_beat_errors(storyboard, evidence):
    """Key-idea films: at most KEY_IDEA_FIGURES equation figures on screen; animation beats show the real thing."""
    if not is_key_idea(evidence.get('curriculum')):
        return []
    out = key_idea_animation_errors(storyboard.get('scenes'))
    figures = [s.get('id') for s in storyboard.get('scenes') or [] if s.get('kind') == 'figure']
    if len(figures) > KEY_IDEA_FIGURES:
        out.append(f'{", ".join(figures)}: {len(figures)} figure beats; a key-idea film may show at most '
                   f'{KEY_IDEA_FIGURES}. Show the idea through the analogy and animation instead')
    return out


# ------------------------------------------------------------------ does the idea land?
# User, 2026-10-21: "in this step we need to just use the reviewer as a student not a judge". A key-idea film is not
# complete coverage or every formula: it makes one analogy land and explains the idea in simple terms. I4 v3 failed
# its landing rewrite when the narration reviewer demanded the signed energy change, nu = |dE|/h and the worked values
# five rounds running.
STUDENT_REVIEW_TASK = (
    "You are a 15-year-old who finds science hard, watching a short film that explains one idea with one analogy. "
    "Read the narration (the CANDIDATE's points, in order) as that student would hear it. The film is meant to leave "
    "you able to say: {takeaway} It is NOT meant to cover the whole topic, give formulas or worked numbers, or teach "
    "rules beyond that, so never ask for more content. Report as major only: (1) a point where you got lost or could "
    "not follow the step from the one before; (2) a place where the analogy stops making sense, changes, or seems to "
    "say something the film later contradicts; (3) a statement, number or equation that is actually wrong (check it "
    "against the facts given). A word you did not know that the film never explained is a minor issue, unless it "
    "made you lose the thread. Everything else (wording, pace, what you would have liked to hear more about) is minor. "
    "Score how well you followed and could explain the idea back (0-100). Point each issue at the point id.")


def student_review(context):
    """review_as for key-idea scripts: the reviewer reads as a struggling student, with the takeaway, the analogy and
    the facts (to catch a wrong statement), not the book's statement of the idea or its coverage."""
    evidence = context.get("evidence") or {}
    plan = evidence.get("idea_plan") or {}
    return (STUDENT_REVIEW_TASK.format(takeaway=plan.get("takeaway", "")),
            {"analogy": plan.get("analogy"), "facts": evidence.get("facts", [])})


LEARNER_TASK = (
    "Play a learner: an average {audience_short} who finds science hard, has NOT studied this topic, and forgets "
    "anything that was not explained in everyday words. Below is the narration of a short film, and nothing else. "
    "Stay in character: use only what the film said, and when a symbol, formula, term or step was not made clear in "
    "plain words, you do not understand it; do not fill gaps from your own knowledge. Explain the film's main idea "
    "in your own words (3-5 sentences, as this student would talk), answer each question in one or two sentences "
    "(say 'I don't know' when the film did not make it clear to you), and list what confused you: every term, "
    "symbol, formula or jump you would need explained. Return {{explanation, answers:[{{question, answer}}], "
    "confusing:[string], lost_at:[string]}}.")

JUDGE_TASK = (
    "A short film tries to make one idea land, simply, for students who find science hard: {idea}. Understanding "
    "wanted: {understanding} A learner who had only the film's narration (playing a struggling student) explained "
    "it back, answered questions and listed what confused it (learner below). Judge two things. understood: does the "
    "learner's explanation capture the understanding wanted, with no wrong belief (for example an analogy taken "
    "literally)? simple: did the film reach this learner without losing it in symbols, formulas and terms? Do NOT "
    "ask for completeness, more facts or more of the book; ask for less when less would help. Suggest "
    "simplifications: a clearer analogy, a missing everyday step, an equation or term to drop or explain. "
    "Return {{understood:0..100, simple:0..100, wrong_beliefs:[string], fixes:[{{point_id:'P03', problem, suggestion}}]}}.")

LANDING = {'understood': 75, 'simple': 70, 'confusing': 2}     # what "the idea landed" means


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
    landed = (_score(verdict.get('understood')) >= LANDING['understood']
              and _score(verdict.get('simple')) >= LANDING['simple']
              and len(learner.get('confusing') or []) <= LANDING['confusing'] and not verdict.get('wrong_beliefs'))
    verdict['score'] = min(_score(verdict.get('understood')), _score(verdict.get('simple')))
    return {'learner': learner, 'verdict': verdict, 'landed': landed}


def _score(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
