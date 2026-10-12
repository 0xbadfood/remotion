"""Standalone chapter-planning sketch. Produces JSON; never starts production."""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import time
from collections import Counter
from pathlib import Path

from .core import ROOT, Store, configuration, digest, read, write
from .creative import MOODS
from .llm import Models, ModelOutputError

SCHEMA = 'infofilm.chapter-plan.v0'
RUBRIC = ('grounding', 'coverage', 'chapter_coherence', 'global_flow', 'mood_fit', 'production_readiness')
STRUCTURES = {'chronological', 'thematic', 'hybrid', 'comparison', 'procedure'}

CONTRACT = {
 'schema_version': SCHEMA,
 'film': {'title': 'Readable title', 'subject': 'exact requested subject',
  'central_question': 'Question answered across the entire film', 'narrative_arc': 'One connected explanation',
  'structure': 'thematic', 'structure_rationale': 'Explain why this arrangement fits the requested subject and coverage',
  'advisory_seconds': 900, 'language': 'en', 'orientation': 'landscape', 'narrator_id': 'default',
  'shared_visual_style': 'Subject-appropriate generated imagery; reconstructed history is identified as illustration',
  'continuity_rules': ['Same narrator and visual identity; each chapter advances the central question']},
 'coverage': [{'id': 'T01', 'question': 'One distinct explanatory question', 'priority': 'essential',
  'anchor_ids': ['F01'], 'owner_chapter_id': 'C01'}],
 'chapters': [{'id': 'C01', 'title': 'Short chapter title',
  'scope': {'start_year': None, 'end_year': None, 'description': 'Distinct chapter focus; each year endpoint independently optional and source-backed'},
  'advisory_seconds': 150, 'objective': 'Why this chapter exists in the global explanation',
  'topic_ids': ['T01'], 'source_ids': ['S1'],
  'research_queries': ['Focused query for later chapter research'],
  'opening_role': 'film_intro', 'ending_role': 'handoff',
  'entry_context': 'What viewers already know; first chapter has a meaningful hook',
  'exit_handoff': 'Question or change that motivates the next chapter; final chapter answers the central question',
  'exclude': ['Detailed material owned by another chapter; short reminders are permitted'],
  'mood': {'preset': 'serious', 'direction': '<specific narration delivery and emotional development for this chapter>',
           'rationale': 'Evidence and narrative reason'},
  'music': {'direction': '<specific instruments, energy, motif development and transition for this chapter; instrumental under speech>', 'bpm': 80},
  'visual_direction': 'Scenes and explanatory overlays suited to this chapter; image count is planned only after MFA. '
                      'Each image is one full-frame scene: ask for comparisons, labels and captions as native overlays, '
                      'never split screens, panels or text inside the generated image',
  'production': {'kind': 'illustrated', 'rationale': 'Why this visual medium serves the chapter objective',
                 'animation_brief': None},
  'section_card': {'title': 'Chapter title', 'date_label': '',
                   'image_selection_brief': 'Later select a relevant approved asset from the incoming chapter'}}],
 'assembly': {'chapter_order': ['C01'], 'section_card_seconds': 2.0,
  'narration_pause_seconds': 0.7, 'music_crossfade_seconds': 1.5,
  'narration_policy': 'separate_clean_stems', 'card_image_policy': 'approved_incoming_chapter_asset'},
 'research_gaps': [{'question': 'A material question still needing chapter research', 'chapter_id': 'C01'}]
}


def finite_number(value):
    return type(value) in {int, float} and math.isfinite(value)


ANIMATION_BRIEF = {'learning_objective': 'What viewers should understand at the end',
    'demonstrations': ['Observable changes or comparisons that explain the concept'],
    'assumptions': ['Explicit teaching simplifications, supported by sources'],
    'success_criteria': ['What the demonstration must make clear']}

ANIMATION_ROUTING_TASK = (
 'Animation chapters are enabled. For each chapter choose production.kind illustrated or animation, and explain why. '
 'Use animation when movement, changing relationships, mechanisms, flows or controlled comparisons teach the objective '
 'better than illustrated narration. Do not choose it merely because a subject is scientific. A chapter may contain '
 'multiple related demonstrations and reuse components; avoid a separate chapter for every tiny action. '
 'Choose the smallest useful set of independently understandable chapters. For a focused five-to-six-minute '
 'mechanism explainer, two or three substantial chapters are often clearer than five small ones; this is guidance, '
 'not a runtime constraint. Keep setup, changing variables and a recap together when they teach one connected '
 'mechanism. Do not split independent axes, constant horizontal speed and vertical acceleration into separate '
 'chapters if they can be demonstrated coherently in one shared workspace. '
 'For animation provide animation_brief with learning_objective, demonstrations, assumptions and success_criteria. '
 'State visible cause/effect and important misconceptions, not generic "show an animation" instructions. '
 'Demonstrations, assumptions and success criteria are factual instructions, not decorative prose: qualify '
 'what is held constant, what changes, where a measured quantity applies, and which comparisons are '
 'hypothetical. Label metaphors and distinguish them from the underlying quantities. Avoid shorthand '
 'that changes the science, even if the chapter objective is otherwise correct. '
 'This is a handoff to a frontier chapter planner: it owns the teaching sequence, narration, detailed canvas, '
 'asset requests, placement and timing after speech/MFA. Do not prescribe coordinates, component IDs, '
 'motion primitives, scripts or timing. SVG creation can pause for missing assets; that is a supported workflow, '
 'not a reason to reject a useful demonstration. Preserve grounded facts and scope and expose research gaps. '
 'For illustrated chapters animation_brief must be null. Section cards may reuse a rendered animation checkpoint.')


def production_kind(chapter):
    """Old plans stay illustrated; a declared route must still pass plan validation."""
    value = chapter.get('production')
    return value.get('kind', 'illustrated') if isinstance(value, dict) else 'illustrated'


def production_errors(chapter, request):
    value = chapter.get('production')
    if value is None:
        return ['Choose a production kind for this chapter'] if request.get('animation_chapters') else []
    if not isinstance(value, dict) or value.get('kind') not in {'illustrated', 'animation'}:
        return ['production needs kind illustrated or animation']
    errors = []
    if not text(value.get('rationale')):
        errors.append('production needs a concrete rationale')
    brief = value.get('animation_brief')
    if value['kind'] == 'illustrated':
        if brief is not None: errors.append('Illustrated chapters use null animation_brief')
        return errors
    if not request.get('animation_chapters'):
        errors.append('Animation chapters are disabled for this request')
    if not isinstance(brief, dict):
        return errors + ['Animation chapters need an animation_brief']
    if not text(brief.get('learning_objective')):
        errors.append('Animation brief needs a learning_objective')
    for key in ('demonstrations', 'assumptions', 'success_criteria'):
        rows = brief.get(key)
        if not isinstance(rows, list) or len(rows) > 12 or any(not text(row) for row in rows):
            errors.append('Animation brief needs a bounded ' + key + ' list')
        elif key != 'assumptions' and not rows:
            errors.append('Animation brief needs nonempty ' + key)
    return errors


def text(value, limit=1800):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def settings_errors(settings):
    errors = []
    if settings.get('schema_version') != 'infofilm.external-planner-settings.v0':
        errors.append('Unknown external planner settings schema')
    for key, low, high in [('max_chapters', 2, 20), ('max_topics', 4, 60), ('max_attempts', 1, 5),
                           ('draft_max_tokens', 1000, 16000), ('review_max_tokens', 1000, 8000),
                           ('max_brief_bytes', 2000, 100000), ('max_request_bytes', 10000, 200000)]:
        value = settings.get(key)
        if type(value) is not int or not low <= value <= high:
            errors.append(f'Invalid {key}: expected integer {low}–{high}')
    weights = settings.get('rubric_weights', {})
    if (not isinstance(weights, dict) or set(weights) != set(RUBRIC)
            or any(not finite_number(v) or v <= 0 for v in weights.values())
            or abs(sum(weights.values()) - 1) > .0001):
        errors.append('Rubric weights must contain six positive weights summing to one')
    if settings.get('planner_role') not in {'planner', 'reviewer'}:
        errors.append('planner_role must select one of the two configured LLMs')
    if not finite_number(settings.get('review_min_score')) or not 0 <= settings['review_min_score'] <= 100:
        errors.append('Invalid review_min_score')
    if type(settings.get('beat_sheet', False)) is not bool:
        errors.append('beat_sheet must be boolean')
    tolerance = settings.get('script_length_tolerance', 0.25)
    if not finite_number(tolerance) or not 0.05 <= tolerance <= 0.5:
        errors.append('script_length_tolerance must be 0.05-0.5')
    guide = settings.get('recommended_chapter_seconds')
    if not isinstance(guide, list) or len(guide) != 2 or any(not finite_number(v) or v <= 0 for v in guide) or guide[0] > guide[1]:
        errors.append('Need a positive recommended chapter duration range')
    return errors


def brief_errors(brief, settings):
    errors = []
    sources, anchors = brief.get('sources'), brief.get('anchors')
    if not isinstance(sources, list) or not sources or any(not isinstance(s, dict) for s in sources):
        return ['Brief needs sources[{id,url,title,excerpts:[text]}]']
    source_ids = [s.get('id') for s in sources]
    if any(not text(s, 40) for s in source_ids) or len(set(source_ids)) != len(source_ids):
        errors.append('Brief source IDs must be unique strings')
    for source in sources:
        if not text(source.get('url'), 2000) or not isinstance(source.get('excerpts'), list) or not source['excerpts'] or any(not text(e, 14000) for e in source['excerpts']):
            errors.append(f'Source {source.get("id")} needs an archived excerpt and URL')
    if not isinstance(anchors, list) or not anchors or any(not isinstance(a, dict) for a in anchors):
        return errors + ['Brief needs anchors[{id,claim,source_ids}]']
    anchor_ids = [a.get('id') for a in anchors]
    if any(not text(a, 40) for a in anchor_ids) or len(set(anchor_ids)) != len(anchor_ids):
        errors.append('Brief anchor IDs must be unique strings')
    for anchor in anchors:
        refs = anchor.get('source_ids')
        if not text(anchor.get('claim')) or not isinstance(refs, list) or not refs or any(not isinstance(s, str) or s not in source_ids for s in refs):
            errors.append(f'{anchor.get("id")}: needs a claim grounded in supplied source ids')
        years = anchor.get('event_years', [])
        if not isinstance(years, list) or any(type(y) is not int for y in years):
            errors.append(f'{anchor.get("id")}: event_years must be zero or one integer within the requested scope')
    required = brief.get('required_anchor_ids', [])
    if not isinstance(required, list) or any(not isinstance(a, str) or a not in anchor_ids for a in required):
        errors.append('Unknown required anchor IDs')
    if len(json.dumps(brief, ensure_ascii=False).encode()) > settings['max_brief_bytes']:
        errors.append('Orientation brief exceeds its code-enforced byte bound; condense it before planning')
    return errors


def enrich_plan(raw, brief):
    """Resolve redundant references/known enums without altering narrative choices."""
    plan, changes = copy.deepcopy(raw), []
    topics = plan.get('coverage', [])
    chapters = plan.get('chapters', [])
    if not isinstance(topics, list) or not isinstance(chapters, list):
        return plan, changes
    # This is a newly generated response to our contract, not an imported plan.
    # Fill missing metadata without asking the model to rewrite valid prose.
    if 'schema_version' not in plan and all(key in plan for key in ['film', 'coverage', 'chapters', 'assembly', 'research_gaps']):
        plan['schema_version'] = SCHEMA
        changes.append({'field': 'schema_version', 'before': None, 'after': SCHEMA,
                        'reason': 'Generated response uses the requested contract; full validation still follows'})
    anchors = {anchor['id']: anchor for anchor in brief['anchors']}
    for topic in topics:
        if isinstance(topic, dict) and topic.get('priority') == 'important':
            # Preserve the strongest coverage intent; never demote or discard a
            # topic because the model supplied this common priority synonym.
            changes.append({'field': f'coverage.{topic.get("id")}.priority',
                            'before': 'important', 'after': 'essential',
                            'reason': 'Conservative priority alias; question, grounding and ownership are unchanged'})
            topic['priority'] = 'essential'
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            continue
        expected = 'film_conclusion' if index == len(chapters) - 1 else 'handoff'
        if expected == 'film_conclusion' and chapter.get('ending_role') in {'conclusion', 'final_conclusion'}:
            changes.append({'field': f'chapters.{chapter.get("id")}.ending_role', 'before': chapter['ending_role'], 'after': expected,
                            'reason': 'Canonical enum alias on the final chapter'})
            chapter['ending_role'] = expected
        refs, listed = chapter.get('topic_ids'), chapter.get('source_ids')
        if not isinstance(refs, list) or not isinstance(listed, list) or any(not isinstance(s, str) for s in listed):
            continue
        needed = {s for topic in topics if isinstance(topic, dict) and topic.get('id') in refs and isinstance(topic.get('anchor_ids'), list)
                  for aid in topic['anchor_ids'] if isinstance(aid, str) and aid in anchors for s in anchors[aid]['source_ids']}
        combined = sorted(set(listed) | needed)
        if combined != listed:
            changes.append({'field': f'chapters.{chapter.get("id")}.source_ids', 'before': listed, 'after': combined,
                            'reason': 'Source union derived from existing owned topics and supplied anchors'})
            chapter['source_ids'] = combined
    return plan, changes


def plan_structure(plan):
    """Read declared structure; old plans remain usable without a schema migration."""
    film = plan.get('film', {})
    if isinstance(film, dict) and 'structure' in film:
        return film['structure']
    dates = [c.get('scope', {}) for c in plan.get('chapters', []) if isinstance(c, dict)]
    if dates and all(isinstance(s, dict) and type(s.get('start_year')) is int
                     and type(s.get('end_year')) is int for s in dates):
        return 'chronological' if all(b['start_year'] >= a['end_year'] for a, b in zip(dates, dates[1:])) else 'hybrid'
    return 'hybrid' if any(isinstance(s, dict) and s.get('start_year') is not None for s in dates) else 'thematic'


def scope_warnings(plan, request):
    """Surface timeline differences for semantic review, not automatic date repairs."""
    if plan_structure(plan) != 'chronological':
        return []
    dates = [(c.get('id'), c.get('scope', {})) for c in plan.get('chapters', []) if isinstance(c, dict)]
    dates = [(i,s) for i,s in dates if isinstance(s, dict) and type(s.get('start_year')) is int and type(s.get('end_year')) is int]
    warnings = []
    for (a, before), (b, after) in zip(dates, dates[1:]):
        if after['start_year'] > before['end_year'] + 1:
            warnings.append(f'{a} to {b}: date interval is unassigned; review whether substantive coverage is missing')
        elif after['start_year'] < before['end_year']:
            warnings.append(f'{a} and {b}: dates overlap; review concurrent developments and distinct topic ownership')
    if dates and request.get('start_year') is not None:
        if dates[0][1]['start_year'] != request['start_year'] or dates[-1][1]['end_year'] != request['end_year']:
            warnings.append('Chapter dates do not reach both requested bounds; review coverage against the sources and user scope')
    return warnings


def plan_errors(plan, request, brief, settings):
    errors = []
    if plan.get('schema_version') != SCHEMA:
        errors.append(f'Use schema_version {SCHEMA}')
    film, chapters, topics, assembly = [plan.get(k) for k in ['film', 'chapters', 'coverage', 'assembly']]
    if not isinstance(film, dict) or not isinstance(assembly, dict):
        return errors + ['Need film and assembly objects']
    for key in ['title', 'central_question', 'narrative_arc', 'shared_visual_style']:
        if not text(film.get(key)):
            errors.append(f'film.{key} needs concise text')
    for key in ['subject', 'language', 'orientation', 'narrator_id', 'advisory_seconds']:
        if film.get(key) != request[key]:
            errors.append(f'Preserve requested film.{key}')
    if not isinstance(film.get('continuity_rules'), list) or not film['continuity_rules'] or any(not text(rule) for rule in film['continuity_rules']):
        errors.append('Need shared continuity rules')
    if not isinstance(chapters, list) or not 2 <= len(chapters) <= settings['max_chapters'] or any(not isinstance(c, dict) for c in chapters):
        return errors + [f'Need 2–{settings["max_chapters"]} chapter objects']
    structure = plan_structure(plan)
    if not isinstance(structure, str) or structure not in STRUCTURES:
        errors.append('film.structure must be chronological, thematic, hybrid, comparison or procedure')
    if 'structure' in film and not text(film.get('structure_rationale')):
        errors.append('Explain the chosen film.structure in structure_rationale')
    chapter_ids = [c.get('id') for c in chapters]
    if any(not isinstance(cid, str) or not re.fullmatch(r'C\d{2}', cid) for cid in chapter_ids) or len(set(chapter_ids)) != len(chapter_ids):
        return errors + ['Chapter IDs must be unique C01-style strings']
    if not isinstance(topics, list) or not 1 <= len(topics) <= settings['max_topics'] or any(not isinstance(t, dict) for t in topics):
        return errors + ['Need a bounded coverage ledger']
    topic_ids = [t.get('id') for t in topics]
    if any(not isinstance(tid, str) or not re.fullmatch(r'T\d{2}', tid) for tid in topic_ids) or len(set(topic_ids)) != len(topic_ids):
        return errors + ['Coverage IDs must be unique T01-style strings']
    anchors = {a['id']: a for a in brief['anchors']}
    seen_anchors, owners = set(), {}
    for topic in topics:
        if not text(topic.get('question')):
            errors.append(f'coverage.{topic["id"]}.question must be nonempty text of at most 1800 characters')
        if topic.get('priority') not in {'essential', 'supporting'}:
            errors.append(f'coverage.{topic["id"]}.priority={topic.get("priority")!r}; use exactly "essential" or "supporting"')
        refs = topic.get('anchor_ids')
        if not isinstance(refs, list) or not refs or any(not isinstance(a, str) or a not in anchors for a in refs):
            errors.append(f'Invalid coverage anchors: {topic["id"]}')
        else:
            seen_anchors.update(refs)
        owner = topic.get('owner_chapter_id')
        if owner not in chapter_ids:
            errors.append(f'Unknown owner of {topic["id"]}')
        owners[topic['id']] = owner
    missing = set(brief.get('required_anchor_ids', [])) - seen_anchors
    if missing:
        errors.append('Required source anchors omitted: ' + ', '.join(sorted(missing)))
    source_ids = {s['id'] for s in brief['sources']}
    assigned = []
    previous_start = None
    chronological = structure == 'chronological'
    for index, chapter in enumerate(chapters):
        cid = chapter['id']
        errors.extend(cid + ': ' + error for error in production_errors(chapter, request))
        for key in ['title', 'objective', 'entry_context', 'exit_handoff', 'visual_direction']:
            if not text(chapter.get(key)):
                errors.append(f'{cid}.{key} needs concise text')
        duration = chapter.get('advisory_seconds')
        if not finite_number(duration) or duration <= 0:
            errors.append(f'{cid} needs a positive advisory duration; its sum is not a hard runtime constraint')
        for key in ['research_queries', 'exclude']:
            values = chapter.get(key)
            if not isinstance(values, list) or (key == 'research_queries' and not values) or any(not text(v) for v in values):
                errors.append(f'{cid}.{key} must be a concise text list')
        refs = chapter.get('topic_ids')
        if not isinstance(refs, list) or not refs or any(not isinstance(t, str) or owners.get(t) != cid for t in refs):
            errors.append(f'{cid} topic ownership is invalid')
        else:
            assigned.extend(refs)
        sources = chapter.get('source_ids')
        if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or s not in source_ids for s in sources):
            errors.append(f'{cid} has invalid source references')
        elif isinstance(refs, list) and all(isinstance(t, str) for t in refs):
            required_sources = {s for t in topics if t['id'] in refs and isinstance(t.get('anchor_ids'), list)
                                for aid in t['anchor_ids'] if isinstance(aid, str) and aid in anchors for s in anchors[aid]['source_ids']}
            if not required_sources <= set(sources):
                errors.append(f'{cid} source list omits sources supporting its assigned anchors')
        expected_opening = 'film_intro' if index == 0 else 'continuation'
        expected_ending = 'film_conclusion' if index == len(chapters) - 1 else 'handoff'
        if chapter.get('opening_role') != expected_opening:
            errors.append(f'{cid}.opening_role must be exactly {expected_opening!r}')
        if chapter.get('ending_role') != expected_ending:
            errors.append(f'{cid}.ending_role must be exactly {expected_ending!r}')
        scope = chapter.get('scope', {})
        if not isinstance(scope, dict) or not text(scope.get('description')):
            errors.append(f'{cid} needs an explicit scope')
            continue
        start, end = scope.get('start_year'), scope.get('end_year')
        valid_dates = all(v is None or type(v) is int for v in [start,end]) and (start is None or end is None or start <= end)
        if not valid_dates:
            errors.append(f'{cid} each date bound must be an integer or null; when both are known, start <= end')
        if valid_dates:
            if request.get('start_year') is not None and any(v is not None and not request['start_year'] <= v <= request['end_year'] for v in [start,end]):
                errors.append(f'{cid} dates extend outside the user-requested bounds')
            if chronological:
                if start is not None:
                    if previous_start is not None and start < previous_start:
                        errors.append(f'{cid} chronology reverses the preceding known start; use hybrid for deliberate temporal revisiting')
                    previous_start = start
                owned = {aid for topic in topics if topic.get('id') in (refs or []) and isinstance(topic.get('anchor_ids'), list)
                         for aid in topic['anchor_ids'] if isinstance(aid, str) and aid in anchors}
                outside = sorted((aid, year) for aid in owned for year in anchors[aid].get('event_years', [])
                                 if (start is not None and year < start) or (end is not None and year > end))
                if outside:
                    errors.append(f'{cid} owns dated events outside its {start}–{end} scope: {outside}. Adjust chapter boundaries or topic ownership, preserving the requested overall range.')
        mood, music, card = [chapter.get(k, {}) for k in ['mood', 'music', 'section_card']]
        if not isinstance(mood, dict) or mood.get('preset') not in set(MOODS) - {'auto'} or not text(mood.get('direction')) or not text(mood.get('rationale')):
            errors.append(f'{cid} needs subject-appropriate mood direction and an available preset')
        if not isinstance(music, dict) or not text(music.get('direction')) or type(music.get('bpm')) is not int or not 40 <= music['bpm'] <= 180:
            errors.append(f'{cid} needs a restrained instrumental music direction and BPM')
        for kind, generic in [('mood', 'Mood fits the events, not a moral label for a ruler'),
                              ('music', 'Restrained instrumental underscore below speech; coherent motif with chapter-specific development')]:
            value = chapter.get(kind)
            direction = value.get('direction') if isinstance(value, dict) else None
            if isinstance(direction, str) and (direction == generic or '<' in direction or '>' in direction):
                errors.append(f'{cid}.{kind}.direction is a contract placeholder; provide a concrete chapter-specific direction')
        if not isinstance(card, dict) or any(not text(card.get(k)) for k in ['title', 'image_selection_brief']) or not isinstance(card.get('date_label'), str):
            errors.append(f'{cid} needs a section-card selection brief; asset IDs do not exist yet')
    if Counter(assigned) != Counter(topic_ids):
        errors.append('Every coverage topic must be owned and assigned exactly once')
    music_directions = [c['music'].get('direction') for c in chapters if isinstance(c.get('music'), dict)]
    if len(music_directions) == len(chapters) and all(isinstance(v, str) for v in music_directions) and len(set(music_directions)) == 1:
        errors.append('Identical music directions throughout do not specify chapter development; vary instrumentation, energy or transitions while retaining the common motif')
    if assembly.get('chapter_order') != chapter_ids:
        errors.append('Assembly order must match the approved chapter sequence')
    for key in ['section_card_seconds', 'narration_pause_seconds', 'music_crossfade_seconds']:
        value = assembly.get(key)
        if not finite_number(value) or not 0 <= value <= 10:
            errors.append(f'Invalid assembly.{key}')
    if assembly.get('narration_policy') != 'separate_clean_stems' or assembly.get('card_image_policy') != 'approved_incoming_chapter_asset':
        errors.append('Preserve clean narration/music stems and select cards from approved incoming chapter assets')
    gaps = plan.get('research_gaps')
    if not isinstance(gaps, list) or any(not isinstance(g, dict) or not text(g.get('question')) or g.get('chapter_id') not in chapter_ids for g in gaps):
        errors.append('Research gaps must be explicit questions mapped to real chapters')
    return errors


def metrics(plan, request, brief, settings):
    chapters = plan.get('chapters', []) if isinstance(plan.get('chapters'), list) else []
    topics = plan.get('coverage', []) if isinstance(plan.get('coverage'), list) else []
    assembly = plan.get('assembly', {}) if isinstance(plan.get('assembly'), dict) else {}
    durations = [c.get('advisory_seconds', 0) for c in chapters if isinstance(c, dict)]
    durations = [v for v in durations if finite_number(v)]
    total = sum(durations)
    required = set(brief.get('required_anchor_ids', []))
    covered = {a for t in topics if isinstance(t, dict) and isinstance(t.get('anchor_ids'), list) for a in t['anchor_ids'] if isinstance(a, str)}
    low, high = settings['recommended_chapter_seconds']
    return {'chapters': len(chapters), 'topics': len(topics),
        'required_anchors': len(required), 'covered_required_anchors': len(required & covered),
        'chapter_advisory_seconds_total': total,
        'section_card_seconds_total': max(0, len(chapters) - 1) * (assembly.get('section_card_seconds', 0)
            if finite_number(assembly.get('section_card_seconds')) else 0),
        'requested_advisory_seconds': request['advisory_seconds'],
        'runtime_policy': 'Informational only; never reject a plan for missing an exact runtime sum',
        'structure': plan_structure(plan), 'scope_warnings': scope_warnings(plan, request),
        'downstream_duration_warnings': [c.get('id') for c in chapters if isinstance(c, dict) and finite_number(c.get('advisory_seconds')) and not low <= c['advisory_seconds'] <= high]}


def semantic_view(plan):
    result = copy.deepcopy(plan)
    if isinstance(result.get('film'), dict):
        result['film'].pop('advisory_seconds', None)
    chapters = result.get('chapters', []) if isinstance(result.get('chapters'), list) else []
    total = sum(c.get('advisory_seconds', 0) for c in chapters if isinstance(c, dict) and finite_number(c.get('advisory_seconds')))
    for chapter in chapters:
        if isinstance(chapter, dict):
            seconds = chapter.pop('advisory_seconds', None)
            if total and finite_number(seconds):
                chapter['relative_attention_percent'] = round(seconds / total * 100, 1)
    return result


def revision_errors(candidate, baseline, feedback):
    """Check that explicitly flagged fields were touched; semantics still need review."""
    def field(plan, path):
        value = plan
        for part in path.split('.'):
            if isinstance(value, list):
                value = next((item for item in value if isinstance(item, dict) and item.get('id') == part), None)
            elif isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value
    errors = []
    for path in feedback.get('must_change_fields', []):
        before, after = field(baseline, path), field(candidate, path)
        if before is None or after is None:
            errors.append(f'Explicit revision field {path} is missing; preserve its structure and address the finding')
        elif before == after:
            errors.append(f'Explicit revision field {path} was left unchanged; address its evidence-backed finding')
    return errors


def review_score(review, settings):
    rubric = review.get('rubric', {})
    if not isinstance(rubric, dict) or set(rubric) != set(RUBRIC) or any(not finite_number(v) or not 0 <= v <= 100 for v in rubric.values()):
        return None
    return round(sum(rubric[k] * settings['rubric_weights'][k] for k in RUBRIC), 2)


def passing_review(review, settings):
    score = review_score(review, settings)
    issues = review.get('issues')
    return (review.get('pass') is True and score is not None and score >= settings['review_min_score']
            and isinstance(issues, list) and all(isinstance(i, dict) and i.get('severity') in {'minor', 'major', 'blocker'} for i in issues)
            and not any(i['severity'] in {'major', 'blocker'} for i in issues))


def bounded_call(models, role, name, task, context, settings, tokens):
    prompt = task + '\nINPUT:\n' + json.dumps(context, ensure_ascii=False)
    feedback = ''
    for attempt in range(2):
        size = len((prompt + feedback).encode())
        if size > settings['max_request_bytes']:
            raise ValueError(f'Planner request is {size} bytes, exceeding the {settings["max_request_bytes"]}-byte bound; shorten the orientation brief')
        try:
            return models.call(role, name if not attempt else name + '-format-repair', prompt + feedback, max_tokens=tokens)
        except ModelOutputError as exc:
            if attempt:
                raise
            feedback = ('\nFORMAT RECOVERY: The previous response was malformed or incomplete JSON. '
                        'Return a complete compact JSON object, preserving the requested contract and content requirements. '
                        'Check all nested objects and arrays close correctly. Syntax error: ' + str(exc))


DRAFT_TASK = (
 'Plan a coherent general-audience informational film as independently producible chapters. '
 'This parent plan decomposes the requested subject into distinct, understandable questions; it is not narration or an image list. '
 'Interpret the exact user wording before choosing an outline. A broad entity permits selective overview; '
 'an explicit all/complete request requires exhaustive coverage of the researched set; named facets and comparisons must be preserved. '
 'Choose film.structure from chronological, thematic, hybrid, comparison or procedure and give a concrete structure_rationale. '
 'Select chapter count, boundaries and order for clarity and the central question. Do not force one chapter per entity, '
 'equal durations, a ruler sequence, historical periods or any other domain-specific outline. '
 'Chronological development, systems and functions, competing explanations, comparisons and procedural dependencies are alternatives, not universal templates. '
 'Any supplied classification/profile is planning guidance, not a list of mandatory chapters or permission to expand the user scope. '
 'Runtime is advisory: retain important coverage and natural delivery. Usually 60–180 seconds per chapter suits the child pipeline; unresolved scope is a research gap. '
 'Build a coverage ledger from source anchors, with each explanatory question owned by one chapter. Cover required anchors and preserve qualifications. '
 'Each coverage.priority must be exactly essential or supporting; do not invent intermediate priority labels. '
 'Choose useful boundaries supported by sources. Optional user year bounds constrain scope, not structure: '
 'a bounded subject can be thematic or hybrid, and an undated historical request can be chronological. '
 'Use optional chapter years only where useful and supported; either endpoint can independently be null if unknown or irrelevant. Do not invent dates for timeless concepts. '
 'In a chronological plan order known starts forwards. Concurrent developments may overlap, and exact adjoining dates are not required. '
 'Put discrete dated events in their appropriate chronological chapter; earlier background and later consequences may support transitions. '
 'Hybrid or thematic chapters can overlap in time when they explain distinct questions. Distinguish real coverage omissions from gaps between chosen dates. '
 'Separate mechanisms, observations, interpretations and reconstructions as appropriate to the subject; unsupported claims become research questions. '
 'Only C01 introduces the film (opening_role film_intro); all later chapters use continuation. '
 'Only the final chapter concludes it (ending_role film_conclusion); earlier chapters use handoff. '
 'Provide entry knowledge, outgoing handoffs, focused research queries and exclusions that prevent repeated exposition. '
 'Match narration mood to evidence, stakes and explanatory purpose; avoid simplistic moral or emotional labels. '
 'Preserve a common narrator, generated visual style and instrumental musical identity. Give actionable chapter-specific delivery, '
 'instruments, energy, motif development and transitions; do not copy contract placeholders. '
 'Illustrated imagery is generated. Native explanatory overlays and, when enabled, code-drawn animation chapters are available; do not depend on actual footage, scanned artwork or archival assets. '
 'Identify illustrations and reconstructions appropriately; do not portray them as observed evidence. '
 'Plan visual direction, not image counts: scene planning follows narration and MFA. '
 'Section cards reuse approved incoming-chapter images and native title/optional date overlays. Keep clean narration and music stems separate. '
 'Entry context must reflect earlier chapters, not knowledge that this chapter has yet to explain. '
 'Do not create scripts, API calls, asset IDs or job IDs. Return concise JSON matching the supplied contract '
 'with schema_version exactly infofilm.chapter-plan.v0 and valid source/anchor/topic references.'
)
REVIEW_TASK = (
 'Independently review the chapter plan against exact user scope and supplied sources. '
 'Assess grounding, coverage, chapter coherence, global flow, mood fit and production readiness. '
 'Check that the chosen structure suits the subject and preserves named focuses and comparisons. '
 'Do not demand chronology, rulers, political/social context, a system-by-system anatomy outline, '
 'or any domain template unless the subject and evidence call for it. '
 'A broad overview allows editorial selection; an explicit exhaustive request needs coverage of its researched set. '
 'Optional year bounds constrain scope but do not force a chronological plan. Absence of bounds does not forbid historical dates. '
 'Chronological, thematic, hybrid, comparison and procedure are legitimate choices. Thematic/hybrid date overlap is intentional when topics differ. '
 'Flag substantive omitted coverage, reversed causal sequence, misleading boundaries, unsupported dates or claims; '
 'do not reject a plan merely because chapter dates do not form an exact continuous partition. '
 'The orientation brief is limited: missing evidence belongs in research_gaps, not invented facts. '
 'Check entry contexts, chapter boundaries, transitions, distinct topic ownership and final resolution of the central question. '
 'Earlier context or later consequences can support a handoff without becoming another chapter. '
 'Required anchors and source cautions are authoritative. Optional phrasing and stylistic preferences are advisory, not blockers. '
 'Runtime is advisory; do not impose word counts, equal chapter lengths or exact runtime matching. '
 'Require actionable mood/music directions and feasible generated visuals with native overlays. '
 'For animation routes, scrutinize every demonstration and success criterion as a factual instruction. '
 'A correct objective does not excuse a misleading comparison, missing qualifier, unlabelled metaphor '
 'or claim attached to the wrong component or location. Flag these concrete defects for repair. '
 'Music is creative accompaniment, not a factual claim about authentic historical sound. '
 'Code checks references, order and data shapes separately. Do not invent schema fixes or enums. '
 'When revision findings are supplied, verify evidence-backed defects were actually resolved, not merely reworded. '
 'Do not ask for facts, reasons or details the supplied sources do not contain: a plan that leaves an unsupplied detail '
 'unstated (or says it is not supplied) is correct. Implied or approximate precision (a year bound, a section date) is '
 'minor unless the plan explicitly states an unsupported fact. Only demonstrable contradictions of the sources are major. '
 'When prior_findings are supplied, check each against the CURRENT candidate before repeating it; drop resolved ones. '
 'Pass only when no major/blocker issue remains. Each issue must identify field, evidence and actionable correction. '
 'Return {pass:boolean,rubric:{grounding:0..100,coverage:0..100,chapter_coherence:0..100,global_flow:0..100,mood_fit:0..100,production_readiness:0..100},'
 'issues:[{severity:"minor"|"major"|"blocker",field,problem,evidence,correction}],strengths:[string]}.'
)


def flagged_chapters(plan, issues):
    """Chapter ids named by review findings ("chapters[C05].scope", "chapters[2].title", "C05.objective")."""
    ids = [c.get('id') for c in plan.get('chapters', [])]
    found = []
    for issue in issues:
        for token in re.findall(r'chapters\[([^\]]+)\]|\b(C\d{2})\b', str(issue.get('field', ''))):
            value = token[0] or token[1]
            chapter = value if value in ids else (ids[int(value)] if value.isdigit() and int(value) < len(ids) else None)
            if chapter and chapter not in found:
                found.append(chapter)
    return found


def keep_unflagged_chapters(previous, candidate, flagged):
    """Targeted repair enforced by code: the previous chapters, with only the flagged ones taken from the candidate
    (matched by id). Chapters the candidate added, dropped or renumbered are ignored."""
    if not flagged or not isinstance(previous.get('chapters'), list):
        return candidate
    after = {c.get('id'): c for c in candidate.get('chapters', []) if isinstance(c, dict)}
    return {**candidate, 'chapters': [after[c.get('id')] if c.get('id') in flagged and c.get('id') in after else c
                                      for c in previous['chapters']]}


def plan_film(config, store, request, brief, settings, *, models=None, compare=True, seed_plan=None, seed_feedback=None, subject_handoff=None):
    errors = settings_errors(settings)
    if errors:
        raise ValueError('; '.join(errors))
    if subject_handoff is not None:
        if (subject_handoff.get('ready_for_planning') is not True or subject_handoff.get('subject') != request.get('subject')
                or subject_handoff.get('output_schema') != CONTRACT['schema_version']
                or not isinstance(subject_handoff.get('subject_guidance'), dict)
                or not isinstance(subject_handoff.get('messages'), list) or len(subject_handoff['messages']) != 2
                or any(not isinstance(m, dict) or not text(m.get('content'), settings['max_request_bytes'])
                       for m in subject_handoff['messages'])
                or [m.get('role') for m in subject_handoff['messages']] != ['system', 'user']):
            raise ValueError('Subject graph handoff must be ready and match this planning subject and contract')
    errors = brief_errors(brief, settings)
    if (not text(request.get('subject')) or not finite_number(request.get('advisory_seconds'))
            or request['advisory_seconds'] <= 0):
        errors.append('Need a subject and positive advisory duration')
    start, end = request.get('start_year'), request.get('end_year')
    if (start is None) != (end is None) or (start is not None and (type(start) is not int or type(end) is not int or start > end)):
        errors.append('Need integer chronology bounds with start <= end or two nulls when no year bounds were supplied')
    if (seed_plan is None) != (seed_feedback is None) or (seed_plan is not None and (not isinstance(seed_plan, dict) or not isinstance(seed_feedback, dict))):
        errors.append('Provide both a prior plan and object feedback for a focused revision')
    elif seed_feedback is not None:
        fields = seed_feedback.get('must_change_fields', [])
        if not isinstance(fields, list) or any(not text(path, 200) for path in fields):
            errors.append('must_change_fields must be a list of film/chapter field paths')
    if errors:
        raise ValueError('; '.join(errors))
    config = dict(config)
    if settings['planner_role'] == 'reviewer':
        config['planner_url'], config['reviewer_url'] = config['reviewer_url'], config['planner_url']
    models = models or Models(config, store)
    policy_sha = digest({'draft_task': DRAFT_TASK, 'review_task': REVIEW_TASK, 'contract': CONTRACT,
                         'animation_routing': ANIMATION_ROUTING_TASK, 'animation_brief': ANIMATION_BRIEF,
                         'validation': 'subject-autonomous-animation-routing-v9-priority-alias'})
    signature = digest({'request': request, 'brief': brief, 'settings': settings, 'planner': config['planner_url'], 'reviewer': config['reviewer_url'], 'policy_sha': policy_sha,
                        'seed_plan': seed_plan, 'seed_feedback': seed_feedback})
    if subject_handoff is not None:
        signature = digest({'legacy_signature': signature, 'subject_handoff': subject_handoff})
    checkpoint = store.root / 'request.json'
    payload = {'signature': signature, 'policy_sha': policy_sha, 'request': request, 'brief': brief, 'settings': settings,
               'llms': {'planner_url': config['planner_url'], 'reviewer_url': config['reviewer_url']}}
    if subject_handoff is not None:
        payload['subject_handoff'] = subject_handoff
    if checkpoint.exists() and read(checkpoint)['signature'] != signature:
        raise ValueError('This experiment directory belongs to different inputs; choose another --output')
    write(checkpoint, payload)
    write(store.root / 'contract.json', CONTRACT)
    if seed_plan is not None:
        write(store.root / 'baseline.json', seed_plan)
        write(store.root / 'baseline-feedback.json', seed_feedback)
    started = time.monotonic()
    history, previous, feedback, selected = [], seed_plan, seed_feedback, None
    prior_findings = []
    for attempt in range(settings['max_attempts']):
        context = {'request': request, 'orientation_brief': brief, 'contract': CONTRACT,
                   'available_moods': sorted(set(MOODS) - {'auto'})}
        draft_task = DRAFT_TASK
        routing_task = ANIMATION_ROUTING_TASK if request.get('animation_chapters') else (
            'Animation chapters are disabled. Use production.kind illustrated with animation_brief null.')
        draft_task += ' ' + routing_task
        if request.get('animation_chapters'):
            context['animation_brief_shape'] = ANIMATION_BRIEF
        if subject_handoff is not None:
            # The rendered shape already contains the sourced brief and contract.
            draft_task += '\n\n' + '\n\n'.join(m['content'] for m in subject_handoff['messages'])
            context.pop('orientation_brief')
            context.pop('contract')
        if previous is not None:
            context.update(previous_plan=previous, repair_feedback=feedback)
            if (feedback or {}).get('repair_scope', {}).get('chapters'):
                draft_task += (' TARGETED REPAIR: change only the chapters and fields in repair_feedback.repair_scope and '
                               'copy every other chapter and field unchanged from previous_plan. Fix a finding by removing or '
                               'qualifying unsupported precision (e.g. a null year bound, "date not supplied"); never add facts, '
                               'reasons or details that the orientation brief does not contain.')
        raw = bounded_call(models, 'planner', f'chapter-plan-{signature[:8]}-{attempt}', draft_task, context, settings, settings['draft_max_tokens'])
        write(store.root / f'raw-candidate-{attempt}.json', raw)
        candidate, enrichment = enrich_plan(raw, brief)
        if isinstance(candidate.get('film'), dict):
            # Request-owned fields are copied by code, never a reason for a repair round (e.g. a tidied subject).
            for key in ('subject', 'language', 'orientation', 'narrator_id', 'advisory_seconds'):
                if key in request and candidate['film'].get(key) != request[key]:
                    enrichment.append({'field': 'film.' + key, 'before': candidate['film'].get(key), 'after': request[key],
                                       'reason': 'Requested value restored by code'})
                    candidate['film'][key] = request[key]
        if previous is not None and (feedback or {}).get('repair_scope'):
            candidate = keep_unflagged_chapters(previous, candidate, feedback['repair_scope']['chapters'])
        write(store.root / f'structural-enrichment-{attempt}.json', enrichment)
        write(store.root / f'candidate-{attempt}.json', candidate)
        errors = plan_errors(candidate, request, brief, settings)
        if seed_plan is not None:
            errors += revision_errors(candidate, seed_plan, seed_feedback)
        review = bounded_call(models, 'reviewer', f'chapter-review-{signature[:8]}-{attempt}', REVIEW_TASK + ' ' + routing_task,
            {'request_scope': {k: v for k, v in request.items() if k != 'advisory_seconds'}, 'orientation_brief': brief,
             'candidate': semantic_view(candidate), 'scope_warnings': scope_warnings(candidate, request),
             **({'subject_guidance': subject_handoff['subject_guidance']} if subject_handoff is not None else {}),
             **({'editorial_findings_to_verify': seed_feedback} if seed_feedback else {}),
             **({'prior_findings': prior_findings} if prior_findings else {})}, settings, settings['review_max_tokens'])
        scope = (feedback or {}).get('repair_scope') if previous is not None else None
        if scope and isinstance(review.get('issues'), list):
            # Chapters locked in a targeted round were already reviewed: a new finding about them alone is advisory.
            locked = {c.get('id') for c in candidate.get('chapters', [])} - set(scope['chapters'])
            for issue in review['issues']:
                named = flagged_chapters(candidate, [issue]) if isinstance(issue, dict) else []
                if named and set(named) <= locked and issue.get('severity') in {'major', 'blocker'}:
                    issue.update(severity='minor', downgraded='finding about a chapter locked in this targeted repair')
            if review.get('pass') is not True and not any(isinstance(i, dict) and i.get('severity') in {'major', 'blocker'}
                                                          for i in review['issues']):
                review['pass'] = True
        write(store.root / f'review-{attempt}.json', review)
        score = review_score(review, settings)
        accepted = not errors and passing_review(review, settings)
        history.append({'attempt': attempt + 1, 'code_errors': errors, 'review_score': score,
                        'accepted': accepted, 'plan_sha256': digest(candidate)})
        store.event('external-planner', 'accepted' if accepted else 'needs_repair', history[-1])
        if accepted:
            selected = candidate
            write(store.root / 'chapter-plan.json', candidate)
            break
        previous = candidate
        blocking = [i for i in review.get('issues') or [] if isinstance(i, dict) and i.get('severity') in {'major', 'blocker'}]
        prior_findings = [{k: i.get(k) for k in ('severity', 'field', 'problem')} for i in blocking]
        # Code sits between reviewer and planner: structured blocking findings only, never the raw review.
        feedback = {'code_errors': errors,
                    'findings': [{k: i.get(k) for k in ('field', 'severity', 'problem', 'correction')} for i in blocking],
                    'repair_instruction': 'Address concrete defects while preserving useful correct material; do not chase cosmetic changes or runtime matching.'}
        flagged = flagged_chapters(candidate, blocking)
        if flagged and not errors:
            # Targeted repair: only the chapters the review names change; code restores the rest.
            feedback['repair_scope'] = {'chapters': flagged, 'fields': [i.get('field') for i in blocking]}
    comparison = None
    if selected is not None and compare:
        initial = seed_plan if seed_plan is not None else read(store.root / 'candidate-0.json')
        if initial != selected:
            swap = int(signature[0], 16) % 2 == 0
            a, b = (selected, initial) if swap else (initial, selected)
            comparison = bounded_call(models, 'reviewer', f'chapter-comparison-{signature[:8]}',
                'Compare anonymized chapter plans A and B against the supplied sources and subject scope. Do not match runtime. '
                'Prefer demonstrable grounding, coverage, coherent boundaries and handoffs. Return {preferred:"A"|"B"|"tie",'
                'reasons:[{criterion,evidence_A,evidence_B}],regressions:[string],limitations:[string]}.',
                {'subject': request['subject'], 'orientation_brief': brief, 'A': semantic_view(a), 'B': semantic_view(b)}, settings, settings['review_max_tokens'])
            comparison['label_mapping'] = {'A': 'selected' if swap else 'initial', 'B': 'initial' if swap else 'selected'}
            write(store.root / 'comparison.json', comparison)
    calls = []
    for path in sorted((store.root / 'requests').glob('*.response.json')):
        raw = read(path)
        meta = read(path.with_name(path.name.replace('.response.json', '.request.json')))
        calls.append({'role': meta['role'], 'model': meta['model'], 'url': meta['url'],
                      'latency_seconds': raw.get('measured_latency_seconds'), 'usage': raw.get('usage', {})})
    evaluated = selected or previous
    report = {'status': 'accepted_sketch' if selected else 'needs_review', 'production_started': False,
              'signature': signature, 'attempts': history,
              'initial_metrics': metrics(seed_plan if seed_plan is not None else read(store.root / 'candidate-0.json'), request, brief, settings),
              'selected_metrics': metrics(evaluated, request, brief, settings),
              'comparison': comparison, 'provider_calls': calls,
              'total_reported_tokens': sum(c['usage'].get('total_tokens', 0) for c in calls),
              'evaluation_wall_seconds': round(time.monotonic() - started, 3),
              'limitations': ['Model review and blinded preference do not establish human acceptance.',
                             'The orientation brief is not full chapter research.',
                             'No narration, images, music or video production was launched.',
                             'Duration allocations are estimates; actual audio and MFA remain authoritative.']}
    write(store.root / 'evaluation.json', report)
    (store.root / 'plan-review.md').write_text(render_summary(evaluated, report))
    if selected is None:
        raise RuntimeError(f'Parent plan still needs review after {len(history)} bounded attempts; see {store.root / "evaluation.json"}')
    return selected, report


def render_summary(plan, report):
    film = plan.get('film', {}) if isinstance(plan.get('film'), dict) else {}
    chapters = plan.get('chapters', []) if isinstance(plan.get('chapters'), list) else []
    lines = [f'# {film.get("title", "Chapter plan")}', '',
             f'Status: {report["status"]}. Planning sketch only; production was not started.', '',
             film.get('central_question', ''), '', film.get('narrative_arc', ''), '',
             '| Chapter | Scope | Advisory seconds | Mood |', '|---|---|---:|---|']
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        scope = chapter.get('scope', {}) if isinstance(chapter.get('scope'), dict) else {}
        mood = chapter.get('mood', {}) if isinstance(chapter.get('mood'), dict) else {}
        period = f'{scope.get("start_year")}–{scope.get("end_year")}' if scope.get('start_year') is not None else 'Undated focus'
        lines.append(f'| {chapter.get("id")} · {chapter.get("title")} | {period} | {chapter.get("advisory_seconds")} | {mood.get("preset")} |')
    for chapter in chapters:
        if not isinstance(chapter, dict):
            continue
        music = chapter.get('music', {}) if isinstance(chapter.get('music'), dict) else {}
        mood = chapter.get('mood', {}) if isinstance(chapter.get('mood'), dict) else {}
        card = chapter.get('section_card', {}) if isinstance(chapter.get('section_card'), dict) else {}
        lines.extend(['', f'## {chapter.get("id")} · {chapter.get("title")}', '',
                      chapter.get('objective', ''), '', f'Entry: {chapter.get("entry_context", "")}', '',
                      f'Handoff: {chapter.get("exit_handoff", "")}', '',
                      f'Mood: {mood.get("direction", "")}', '', f'Music ({music.get("bpm", "?")} BPM): {music.get("direction", "")}', '',
                      f'Visuals: {chapter.get("visual_direction", "")}', '',
                      f'Section-card selection: {card.get("image_selection_brief", "")}', '',
                      f'Sources: {", ".join(chapter.get("source_ids", []))}' if isinstance(chapter.get('source_ids'), list) and all(isinstance(s,str) for s in chapter['source_ids']) else 'Sources: invalid', '',
                      'Later research:'])
        if isinstance(chapter.get('research_queries'), list):
            lines.extend(f'- {query}' for query in chapter['research_queries'] if isinstance(query, str))
    lines.extend(['', '## Coverage ownership', ''])
    if isinstance(plan.get('coverage'), list):
        lines.extend(f'- {topic.get("id")} → {topic.get("owner_chapter_id")}: {topic.get("question")}' for topic in plan['coverage'] if isinstance(topic, dict))
    lines.extend(['', '## Research gaps', ''])
    if isinstance(plan.get('research_gaps'), list):
        lines.extend(f'- {gap.get("chapter_id")}: {gap.get("question")}' for gap in plan['research_gaps'] if isinstance(gap, dict))
    lines.extend(['', f'Model tokens across saved calls: {report["total_reported_tokens"]}.', '',
                  'Full coverage ownership, source references, research gaps and assembly intent are in chapter-plan.json.'])
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--subject', required=True)
    parser.add_argument('--brief', type=Path, required=True, help='Short sourced orientation JSON; full chapter research is a later stage')
    parser.add_argument('--duration-seconds', type=int, default=900, help='Advisory total, not a runtime constraint')
    parser.add_argument('--start-year', type=int)
    parser.add_argument('--end-year', type=int)
    parser.add_argument('--orientation', choices=['landscape', 'portrait', 'both'], default='landscape')
    parser.add_argument('--narrator', default='default')
    parser.add_argument('--language', default='en')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    parser.add_argument('--settings', type=Path, default=ROOT / 'external-planner.json')
    parser.add_argument('--planner-role', choices=['planner', 'reviewer'], help='Swap LLM roles to evaluate the larger-context model as planner')
    parser.add_argument('--no-comparison', action='store_true')
    parser.add_argument('--animation-chapters', action='store_true', help='Let the planner route suitable chapters to the frontier animation pipeline')
    parser.add_argument('--revise-plan', type=Path, help='Preserve this existing plan as baseline and revise it in a new output directory')
    parser.add_argument('--feedback', type=Path, help='JSON object with concrete editorial/reviewer findings for --revise-plan')
    args = parser.parse_args()
    if args.duration_seconds <= 0:
        parser.error('Advisory duration must be positive')
    if bool(args.revise_plan) != bool(args.feedback):
        parser.error('--revise-plan and --feedback must be provided together')
    if (args.start_year is None) != (args.end_year is None) or (args.start_year is not None and args.start_year > args.end_year):
        parser.error('Provide both chronological bounds with start <= end, or omit both to leave year scope open')
    settings = read(args.settings)
    if args.planner_role:
        settings['planner_role'] = args.planner_role
    request = {'subject': args.subject.strip(), 'advisory_seconds': args.duration_seconds,
               'start_year': args.start_year, 'end_year': args.end_year,
               'orientation': args.orientation, 'narrator_id': args.narrator, 'language': args.language}
    if args.animation_chapters:
        request['animation_chapters'] = True
    store = Store(args.output)
    try:
        _, report = plan_film(configuration(args.config), store, request, read(args.brief), settings, compare=not args.no_comparison,
                              seed_plan=read(args.revise_plan) if args.revise_plan else None,
                              seed_feedback=read(args.feedback) if args.feedback else None)
        print(json.dumps({'plan': str(store.root / 'chapter-plan.json'), 'status': report['status'],
                          'attempts': len(report['attempts']), 'tokens': report['total_reported_tokens']}))
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
