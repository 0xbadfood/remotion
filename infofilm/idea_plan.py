"""Key-idea films, step one (user, 2026-10-20): plan the idea before any narration is written.

The plan fixes the analogy that carries the film, a mapping from each part of the analogy to the real thing, the
visual world (the guide character and the analogy's setting, drawn the same way in every image), and a beat outline:
what each beat teaches and where the analogy is at that moment. The script narrates these beats in order and the
creative brief takes its visual style from the world, so the analogy cannot fall away after the opening.
"""
from __future__ import annotations

TASK = (
    "Plan a short simplified film that makes ONE idea land for students who find science hard, before any narration "
    "is written. Idea: {idea}. The learner should come away understanding: {understanding} The learner usually "
    "struggles because: {why} Choose ONE strong analogy or everyday picture that carries the whole idea (the "
    "evidence may offer some; use your own if it fits better). Map each part of it onto the real thing. Design a "
    "simple, consistent visual world: a friendly guide character (describe exactly how it looks, so every picture "
    "draws the same character) and the analogy's setting, light and uncluttered, so short animations can play in "
    "it. Then outline {fewest}-{most} beats in teaching order: open with the learner's own question, let the analogy "
    "play out step by step with room to breathe, reach the 'aha', only then map it onto the real thing with the "
    "book's terms, say in one light beat where the analogy stops working, and close with a short recap. A beat "
    "teaches one thing. Use numbers or equations in at most {equations} beats, exactly as the evidence gives them. "
    "Return {{analogy:{{name, picture:'one or two sentences', stops_working:'where it stops being true'}}, "
    "mapping:[{{analogy:'part of the picture', real:'what it stands for'}}], "
    "world:{{guide:'exact look of the guide character', setting:'the analogy setting', style:'drawing style'}}, "
    "beats:[{{id:'B1', teaches:'one thing', analogy_moment:'what happens in the analogy, or empty', "
    "real_link:'the real thing it maps to, or empty'}}]}}.")

FEWEST, MOST = 6, 14


def errors(value, equations):
    out = []
    analogy, world = value.get('analogy'), value.get('world')
    if not isinstance(analogy, dict) or not all(isinstance(analogy.get(k), str) and analogy[k].strip()
                                                 for k in ('name', 'picture', 'stops_working')):
        out.append('analogy needs name, picture and stops_working')
    mapping = value.get('mapping')
    if not isinstance(mapping, list) or len(mapping) < 2 or not all(
            isinstance(m, dict) and m.get('analogy') and m.get('real') for m in mapping):
        out.append('mapping needs at least two {analogy, real} pairs')
    if not isinstance(world, dict) or not all(isinstance(world.get(k), str) for k in ('guide', 'setting', 'style')):
        out.append('world needs guide, setting and style')
    elif len(world['guide'].split()) < 8:
        out.append('world.guide: describe exactly how the guide looks (at least 8 words)')
    beats = value.get('beats')
    if not isinstance(beats, list) or not FEWEST <= len(beats) <= MOST:
        return out + [f'give {FEWEST}-{MOST} beats']
    for n, beat in enumerate(beats, 1):
        if not isinstance(beat, dict) or beat.get('id') != f'B{n}' or not str(beat.get('teaches', '')).strip():
            out.append(f'B{n}: needs id "B{n}" and what it teaches')
    with_analogy = [b for b in beats if isinstance(b, dict) and str(b.get('analogy_moment', '')).strip()]
    if len(with_analogy) < len(beats) / 2:
        out.append(f'only {len(with_analogy)} of {len(beats)} beats have an analogy_moment; let the analogy carry at '
                   'least half the film')
    return out


def make_idea_plan(models, evidence, experimental=True):
    from .curriculum import KEY_IDEA_EQUATIONS
    idea = evidence['curriculum'].get('idea') or {}
    task = TASK.format(idea=idea.get('title', ''), understanding=idea.get('understanding', ''),
                       why=idea.get('why', ''), fewest=FEWEST, most=MOST, equations=KEY_IDEA_EQUATIONS)
    context = {'evidence': {k: v for k, v in evidence.items() if k != 'idea_plan'}}
    return models.loop('idea-plan', task, context, lambda x: errors(x, KEY_IDEA_EQUATIONS),
                       experimental=experimental)
