"""Checkpointed parent planning and isolated chapter production."""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .core import ROOT, Store, configuration, digest, read, write
from .external_planner import brief_errors, plan_errors, plan_film, settings_errors
from .llm import Models
from .planning import evidence_context
from .narrators import select_reference

LONG_MIN_SECONDS, LONG_MAX_SECONDS = 300, 1800


def year_bounds(subject, start=None, end=None):
    if (start is None) != (end is None):
        raise ValueError('Provide both historical year bounds or neither')
    if start is None:
        match = re.search(r'\b(\d{3,4})\s*(?:to|[-–—])\s*(\d{3,4})\b', subject, re.I)
        if match:
            start, end = map(int, match.groups())
    if start is not None and (type(start) is not int or type(end) is not int or not 1 <= start <= end <= 9999):
        raise ValueError('Historical years must satisfy 1 <= start <= end <= 9999')
    return start, end


def planner_settings(config):
    settings = copy.deepcopy(config.get('longform_settings') or read(ROOT / 'external-planner.json'))
    # Model assignment is operational configuration, not inferred from title dates.
    if 'longform_planner_role' in config:
        settings['planner_role'] = config['longform_planner_role']
    errors = settings_errors(settings)
    if errors:
        raise ValueError('; '.join(errors))
    return settings


def make_orientation(models, extraction, subject, settings, start=None, end=None, *, subject_guidance=None):
    """Create a bounded source-backed parent brief; child research follows later."""
    context = evidence_context(extraction, subject)
    sources = []
    source_rows = context['sources'][:12]
    budget = min(3500, max(600, (settings['max_brief_bytes'] - 18000) // max(1, len(source_rows))))
    for row in source_rows:
        passages = [p['text'] for p in row.get('passages', []) if p.get('text')]
        if not passages:
            continue
        # Preserve a slice from every archived passage rather than only a page's introduction.
        each = max(100, budget // len(passages))
        excerpts = [p.encode()[:each].decode('utf-8', 'ignore') for p in passages]
        sources.append({'id': row['id'], 'url': row['url'], 'title': row['title'], 'excerpts': excerpts})
    if not sources:
        raise ValueError('Parent research has no readable source excerpts')
    task = (
        'Prepare a compact source-backed orientation brief for planning a long informational film, not final narration. '
        'Cover the requested subject focus, major concepts, relationships, phases or mechanisms as applicable, and important qualifications. '
        'Respect named facets and comparisons. Broad subjects permit selective overview; explicitly exhaustive requests need the researched set. '
        'Do not force historical periods, rulers, regional/social/economic context or another domain template onto every subject. '
        'Return {anchors:[{id:"F01",claim,source_ids:["S1"],event_years:[integer]}],required_anchor_ids:["F01"],'
        'coverage_requirements:[string],cautions:[string]}. Use 8–20 distinct anchors supported by the supplied excerpts. '
        'Use event_years only for one discrete dated event per anchor, with zero or one integer; omit it for background or spanning processes. '
        'A system or entity existing across a date range is background, not two discrete events. '
        'Separate important distinct events into different anchors rather than locking them into one chapter. '
        'For an explicit historical scope, event_years must lie within its requested bounds. Later aftermath may be a qualification without an owned event year. '
        'Do not bind a single anchor to the entire requested historical range; split discrete events into separate anchors. '
        'Do not invent missing facts. State important missing evidence as later research requirements. '
        'Required anchors represent minimum useful subject coverage, not a fully researched chapter plan.'
    )
    def validate(value):
        merged = {**value, 'sources': sources}
        errors = brief_errors(merged, settings)
        if not isinstance(value.get('anchors'), list) or not 8 <= len(value['anchors']) <= 20:
            errors.append('Need 8–20 distinct orientation anchors')
        if not value.get('required_anchor_ids'):
            errors.append('Specify the essential anchors')
        for anchor in value.get('anchors', []) if isinstance(value.get('anchors'), list) else []:
            if isinstance(anchor, dict) and isinstance(anchor.get('event_years'), list) and len(anchor['event_years']) > 1:
                errors.append(f"{anchor.get('id')}: use at most one discrete event year; split distinct events or omit years for spanning background")
        return errors
    orientation_context = {'subject': subject, 'scope': {'start_year': start, 'end_year': end}, 'sources': sources}
    if subject_guidance is not None:
        task += (' Use the reviewed subject profile to identify relevant questions and qualifications, '
                 'not as evidence or a fixed outline. Preserve the exact requested focus; unsupported answers remain research gaps.')
        orientation_context['subject_guidance'] = subject_guidance
    result = models.loop('longform-orientation', task, orientation_context, validate, experimental=True)
    return {**result, 'sources': sources}


def chapter_creative(shared, chapter, requested_mood='auto'):
    result = copy.deepcopy(shared)
    mood = chapter['mood']['preset'] if requested_mood == 'auto' else requested_mood
    result.update(resolved_mood=mood, requested_mood=requested_mood,
                  rationale=chapter['mood']['rationale'],
                  narration_direction=chapter['mood']['direction'],
                  visual_style=shared['visual_style'] + ' Chapter direction: ' + chapter['visual_direction'])
    result['music'].update(caption=('Instrumental under speech; use the film-wide musical identity. ' +
        chapter['music']['direction'])[:1800], bpm=chapter['music']['bpm'])
    result['chapter_id'] = chapter['id']
    return result


def chapter_context(plan, chapter, shared, brief):
    index = plan['chapters'].index(chapter)
    topics = [t for t in plan['coverage'] if t['id'] in chapter['topic_ids']]
    anchors = {a['id']: a for a in brief['anchors']}
    film_context = {k:v for k,v in plan['film'].items() if k != 'advisory_seconds'}
    chapter_direction = {k:v for k,v in chapter.items() if k != 'advisory_seconds'}
    return {'film': film_context, 'chapter': chapter_direction, 'owned_topics': topics,
            'orientation_anchors': [anchors[a] for a in sorted({a for t in topics for a in t['anchor_ids']})],
            'previous_chapter': plan['chapters'][index-1]['title'] if index else None,
            'next_chapter': plan['chapters'][index+1]['title'] if index+1 < len(plan['chapters']) else None,
            'research_gaps': [g for g in plan['research_gaps'] if g['chapter_id'] == chapter['id']],
            'shared_creative': shared,
            'research_subject': (f"{plan['film']['subject']}: {chapter['title']}. Scope: {chapter['scope']['description']}. " +
                (f"Date scope: from {chapter['scope']['start_year'] if chapter['scope']['start_year'] is not None else 'an unspecified beginning'} "
                 f"through {chapter['scope']['end_year'] if chapter['scope']['end_year'] is not None else 'an unspecified ending'}. "
                 if chapter['scope']['start_year'] is not None or chapter['scope']['end_year'] is not None else '') +
                f"Objective: {chapter['objective']}. Research questions: " + '; '.join(chapter['research_queries']) +
                '. Include context and source qualifications relevant to these research questions.')[:2600]}


def checked_delivery(project, orientations):
    path = Path(project) / 'delivery.json'
    if not path.exists():
        return None
    delivery = read(path)
    outputs = delivery.get('outputs', [])
    if {o.get('orientation') for o in outputs} != set(orientations):
        return None
    for output in outputs:
        video = Path(output['video'])
        qa = read(video.parent / 'qa.json')
        layout = read(video.parent / 'layout/layout-results.json')
        if not qa.get('passed') or not layout.get('passed') or digest(video.read_bytes()) != qa['video_sha256']:
            raise ValueError(f'Chapter delivery failed integrity checks: {project}')
    return delivery


def child_stage(project):
    path = Path(project) / 'jobs.sqlite3'
    if path.exists():
        try:
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=2) as db:
                row = db.execute("SELECT stage,state FROM events WHERE stage LIKE 'pipeline:%' ORDER BY id DESC LIMIT 1").fetchone()
                if row:
                    return {'stage': row[0].removeprefix('pipeline:'), 'state': row[1]}
        except sqlite3.Error:
            pass
    return {'stage': 'starting', 'state': 'running'}


def chapter_alive(row):
    if not row.get('attempt_directory'):
        return False
    if not row.get('child_pid'):
        expected = str(Path(row['attempt_directory']) / 'result.json').encode()
        for process in Path('/proc').iterdir():
            if not process.name.isdigit():
                continue
            try:
                command = (process / 'cmdline').read_bytes().split(b'\0')
                if b'infofilm.worker' in command and expected in command:
                    row['child_pid'] = int(process.name)
                    break
            except OSError:
                continue
        if not row.get('child_pid'):
            return False
    try:
        command = Path(f"/proc/{row['child_pid']}/cmdline").read_bytes().split(b'\0')
        return b'infofilm.worker' in command and str(Path(row['attempt_directory']) / 'result.json').encode() in command
    except OSError:
        return False


class ChapterLogRelay:
    """Forward durable child output without tying worker survival to a pipe.

    A persisted byte cursor also works when a parent reattaches to a worker.
    Partial lines wait for their newline; a final drain includes a trailing line.
    """
    def __init__(self, attempt, project, store, chapter_id, position, total, row=None, save=None):
        self.path = Path(attempt) / 'worker.log'
        self.project, self.store = project, store
        self.context = {'chapter': chapter_id, 'position': position, 'total': total}
        if row and row.get('chapter', {}).get('title'):
            self.context['title'] = row['chapter']['title']
        self.row, self.save = row, save
        self.offset = int(row.get('log_relay_offset', 0)) if row else 0
        self.started = self.last_output = self.last_report = time.monotonic()
        self.stage_started = self.started
        self.last_stage = None

    def drain(self, *, final=False):
        before = self.offset
        try:
            with self.path.open('rb') as log:
                if log.seek(0, 2) < self.offset:
                    self.offset = 0
                log.seek(self.offset)
                # Bound each ordinary poll. A completed worker can be drained fully.
                budget = 256 * 1024
                while final or budget > 0:
                    line = log.readline(min(budget, 256 * 1024) if not final else 256 * 1024)
                    if not line:
                        break
                    if not line.endswith(b'\n') and not final and len(line) < budget:
                        break
                    self.offset += len(line)
                    budget -= len(line)
                    elapsed = int(time.monotonic() - self.started)
                    prefix = f"[chapter {self.context['chapter']} {self.context['position']}/{self.context['total']} +{elapsed // 60:02d}:{elapsed % 60:02d}]"
                    print(prefix, line.decode('utf-8', errors='replace').rstrip('\r\n'), flush=True)
                    self.last_output = time.monotonic()
        except FileNotFoundError:
            return
        if self.offset != before and self.row is not None:
            self.row['log_relay_offset'] = self.offset
            if self.save:
                self.save()

    def poll(self):
        self.drain()
        now = time.monotonic()
        current = child_stage(self.project)
        if current != self.last_stage:
            self.last_stage = current
            self.stage_started = self.last_report = now
            self.store.event('pipeline:chapters', 'running', {**self.context, **current,
                'elapsed_seconds': round(now - self.started)})
        elif now - self.last_output >= 60 and now - self.last_report >= 60:
            self.last_report = now
            self.store.event('pipeline:chapters', 'running', {**self.context, **current,
                'heartbeat': True, 'elapsed_seconds': round(now - self.started),
                'stage_elapsed_seconds': round(now - self.stage_started),
                'quiet_seconds': round(now - self.last_output)})


def run_chapter(request, directory, store, chapter_id, position, total, row=None, save=None):
    root = Path(directory)
    if row and chapter_alive(row):
        attempt = Path(row['attempt_directory'])
        store.event('pipeline:chapters', 'running', {'chapter': chapter_id, 'reattached': True})
        relay = ChapterLogRelay(attempt, request['project'], store, chapter_id, position, total, row, save)
        while chapter_alive(row):
            relay.poll()
            if (attempt / 'result.json').exists():
                break
            time.sleep(2)
        relay.drain(final=True)
        outcome = read(attempt / 'result.json') if (attempt / 'result.json').exists() else {}
        if outcome.get('status') != 'succeeded':
            raise RuntimeError(outcome.get('error', 'Reattached chapter worker stopped without a checked outcome'))
        return attempt
    attempt = root / 'attempts' / str(time.time_ns())
    request_path = write(attempt / 'request.json', request)
    result = attempt / 'result.json'
    if row is not None:
        row.update(attempt_directory=str(attempt), child_pid=None, log_relay_offset=0)
        if save:
            save()
    with (attempt / 'worker.log').open('wb', buffering=0) as log:
        process = subprocess.Popen([sys.executable, '-u', '-m', 'infofilm.worker', '--request', str(request_path),
            '--result', str(result)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        if row is not None:
            row.update(child_pid=process.pid, attempt_directory=str(attempt))
            if save:
                save()
        # The child shares the parent's process group and continues through a web restart.
        relay = ChapterLogRelay(attempt, request['project'], store, chapter_id, position, total, row, save)
        while process.poll() is None:
            relay.poll()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        relay.drain(final=True)
    outcome = read(result) if result.exists() else {'status': 'failed', 'error': f'Chapter worker exited {process.returncode} without an outcome'}
    if process.returncode != 0 or outcome.get('status') != 'succeeded':
        raise RuntimeError(outcome.get('error') or f'Chapter worker failed with exit {process.returncode}')
    return attempt


def continuity_review(models, plan, records):
    """Verify the actual adjacent narrations before joining; no silent script/audio rewriting."""
    for left, right in zip(records, records[1:]):
        context = {'film_question': plan['film']['central_question'], 'planned_handoff': left['chapter']['exit_handoff']}
        for key, row in [('left', left), ('right', right)]:
            project = Path(row['project']); duration = row['duration']
            context[key] = {'chapter': {k:v for k,v in row['chapter'].items() if k != 'advisory_seconds'}, 'script': read(project / f'film-{duration}/script.json'),
                            'evidence': read(project / 'evidence-final.json')['facts']}
            context[key]['script'].pop('requested_seconds', None)
        task = (
            'Review the actual adjacent chapter narrations of one informational film. '
            'Check chronological/causal contradictions, missing transitions, substantial duplicated exposition, '
            'premature film conclusions, repeated film introductions and misleading scope boundaries. '
            'Use the supplied chapter evidence for facts, not unverified outline claims. A short contextual reminder is allowed. '
            'Do not impose runtime limits, prefer cosmetic wording or demand every source fact. '
            'Minor editorial imprecision and optional wording improvements are advisory; pass with minor issues. '
            'Return {pass:boolean,score:0..100,issues:[{severity:"minor"|"major"|"blocker",field,problem,evidence,correction}],strengths:[string]}.'
        )
        # No numeric global runtime is sent to this semantic review.
        prompt = task + '\nINPUT:\n' + json.dumps(context, ensure_ascii=False)
        if len(prompt.encode()) > 110000:
            raise ValueError('Adjacent chapter review exceeded the bounded context; retain the chapters for targeted review')
        result = models.call('reviewer', f"continuity-{left['chapter']['id']}-{right['chapter']['id']}", prompt, max_tokens=3500)
        issues = result.get('issues')
        accepted = (result.get('pass') is True and type(result.get('score')) in {int, float} and 82 <= result['score'] <= 100
                    and isinstance(issues, list) and all(isinstance(i, dict) and i.get('severity') == 'minor' for i in issues))
        models.store.event('continuity', 'accepted' if accepted else 'needs_attention', {
            'left': left['chapter']['id'], 'right': right['chapter']['id'], 'score': result.get('score'),
            'issues': issues, 'saved_narrations_retained': True})
        if not accepted:
            reasons = '; '.join(i.get('problem', 'Unspecified continuity issue') for i in issues
                                if isinstance(i, dict) and i.get('severity') in {'major', 'blocker'}) if isinstance(issues, list) else 'Invalid review shape'
            raise RuntimeError(f"Chapter continuity needs attention at {left['chapter']['id']} → {right['chapter']['id']}: "
                               f"{reasons or 'Review did not meet the acceptance threshold'}; saved narrations are retained")


def create_long(args):
    from .pipeline import research
    from .creative import make_creative_brief
    from .delivery import preview
    from .longform_video import assemble
    duration = args.durations[0]
    if len(args.durations) != 1 or not LONG_MIN_SECONDS <= duration <= LONG_MAX_SECONDS:
        raise ValueError('Long generation needs one advisory length between 5 and 30 minutes')
    image_resume = getattr(args, 'resume_from', None) == 'images'
    audio_resume = getattr(args, 'resume_from', None) == 'scenes'
    audio_only = args.until == 'audio'
    if getattr(args, 'resume_from', None) and not (image_resume or audio_resume):
        raise ValueError('Long films support image revisions or video continuation from audio')
    if args.until not in {'plan', 'audio', 'video'}:
        raise ValueError('Long generation supports --until plan, audio or video')
    if audio_resume and args.until != 'video':
        raise ValueError('Continuation from audio must target video')
    config = configuration(args.config)
    config = select_reference(config, narrator=getattr(args, 'narrator', None), reference_audio=args.reference_audio,
                              reference_transcript=getattr(args, 'reference_transcript', None))
    if getattr(args, 'image_provider_policy', None):
        registry = config.get('image_provider_registry', {})
        if args.image_provider_policy not in registry.get('policies', {}):
            raise ValueError('Unknown long-film image provider policy')
        config['image_provider_policy'] = args.image_provider_policy
    start, end = year_bounds(args.subject, getattr(args, 'start_year', None), getattr(args, 'end_year', None))
    settings = planner_settings(config)
    store = Store(args.project)
    try:
        graph_enabled = getattr(args, 'subject_graph', False)
        if graph_enabled:
            from .graph_planning import freeze_graph
            config = freeze_graph(config, store.root / 'subject-graph')
        def event(stage, state, **details):
            store.event('pipeline:' + stage, state, details)
        identity = digest({'subject': args.subject, 'duration': duration, 'orientation': args.orientation,
            'years': [start, end], 'settings': settings, 'config': config,
            'supplied_inputs': {k: digest(Path(getattr(args, k)).read_bytes()) for k in ['sources', 'chapter_plan', 'orientation_brief'] if getattr(args, k, None)},
            'reference_sha': digest(Path(args.reference_audio or config['reference_audio']).read_bytes()),
            'controls': {k: getattr(args, k, None) for k in ['mood', 'mood_intensity', 'visual_balance', 'music_engine',
                          'tts_engine', 'omni_speed', 'reference_transcript', 'narrator', 'voice_id', 'language']}})
        if graph_enabled:
            identity = digest({'legacy_identity': identity, 'subject_graph': True})
        record = store.root / 'longform-project.json'
        if not (image_resume or audio_resume) and record.exists() and read(record)['identity'] != identity:
            raise ValueError('This long-film folder has different frozen settings; choose a new project')
        if not (image_resume or audio_resume):
            write(record, {'identity': identity, 'subject': args.subject, 'generation_mode': 'long', 'duration': duration,
                'orientation': args.orientation, 'start_year': start, 'end_year': end, 'subject_graph': graph_enabled})
        models = Models(config, store)
        if image_resume or audio_resume:
            # Retain frozen subject segmentation, research and creative direction.
            from .image_recovery import SETTINGS_KEYS
            from .resume import images_checkpoint
            saved = read(record)
            if saved['subject'] != args.subject or saved['orientation'] != args.orientation:
                raise ValueError('Image revision must retain the saved film subject and orientation')
            plan = read(store.root / 'chapter-plan.json')
            brief = read(store.root / 'orientation-brief.json')
            shared = read(store.root / 'creative-brief.json')
            selected = set(getattr(args, 'image_chapters', None) or [c['id'] for c in plan['chapters']])
            if not selected or selected - {c['id'] for c in plan['chapters']}:
                raise ValueError('Unknown chapter selection for image revision')
            overrides = (getattr(args, 'image_recovery_settings', None) or {}) if image_resume else {}
            if set(overrides) - set(SETTINGS_KEYS):
                raise ValueError('Image revision may change only the image recovery settings')
            config.update(overrides)
            if audio_resume:
                from .audio_delivery import delivery_checkpoint
                delivery_checkpoint(store.root, long=True)
            for chapter in plan['chapters']:
                if chapter['id'] in selected:
                    images_checkpoint(store.root / 'chapters' / f"{store.root.name}-{chapter['id'].lower()}",
                                      [max(30, round(chapter['advisory_seconds']))])
            for name in (['classification'] if graph_enabled else []) + ['research', 'evidence', 'planning', 'creative']:
                event(name, 'succeeded', reused=True, resume_from='scenes' if audio_resume else 'images')
        else:
            guidance = seeds = None
            if graph_enabled:
                from .graph_planning import classify_subject
                event('classification', 'running', subject=args.subject)
                seeds, guidance = classify_subject(config, store, models, args.subject)
            event('research', 'running', scope='Film orientation')
            if args.sources:
                sources = read(args.sources)
            elif getattr(args, 'orientation_brief', None):
                prepared = read(args.orientation_brief)
                sources = {'results': [{'url': s['url'], 'title': s.get('title'), 'raw_content': '\n\n'.join(s['excerpts'])}
                                      for s in prepared['sources']]}
            else:
                sources = research(config, store, args.subject,
                                   **({'subject_guidance': guidance} if guidance is not None else {}))
            write(store.root / 'sources.json', sources)
            write(store.root / 'orientation-sources.json', sources)
            event('research', 'succeeded', source_count=len(sources['results']))
            brief_path = store.root / 'orientation-brief.json'
            event('evidence', 'running', task='Bounded long-film orientation')
            if getattr(args, 'orientation_brief', None):
                brief = read(args.orientation_brief)
            elif brief_path.exists():
                brief = read(brief_path)
            else:
                brief = make_orientation(models, sources, args.subject, settings, start, end,
                                         **({'subject_guidance': guidance} if guidance is not None else {}))
            errors = brief_errors(brief, settings)
            if errors:
                raise ValueError('; '.join(errors))
            write(brief_path, brief)
            evidence = {'title': args.subject, 'facts': [{'id': a['id'], 'claim': a['claim'], 'source_ids': a['source_ids']} for a in brief['anchors']],
                        'uncertainties': brief.get('cautions', []), 'narrative_priorities': brief.get('coverage_requirements', [])}
            write(store.root / 'evidence-final.json', evidence)
            write(store.root / 'orientation-evidence.json', evidence)
            event('evidence', 'succeeded', facts=len(evidence['facts']))
            event('planning', 'running', requested_seconds=duration)
            request = {'subject': args.subject, 'advisory_seconds': duration, 'start_year': start, 'end_year': end,
                       'orientation': args.orientation, 'language': 'en', 'narrator_id': getattr(args, 'voice_id', None) or getattr(args, 'narrator', None) or 'default'}
            plan_path = store.root / 'chapter-plan.json'
            handoff = None
            if graph_enabled:
                from .graph_planning import make_handoff
                handoff = make_handoff(config, store, seeds, guidance, brief, duration, settings)
            if getattr(args, 'chapter_plan', None):
                plan = read(args.chapter_plan)
            elif plan_path.exists():
                plan = read(plan_path)
            else:
                # An exhausted draft loop must get a fresh request namespace on
                # resume, rather than replaying the same failed cached candidates.
                parent_store = Store(store.root / 'parent-planning' / 'attempts' / str(time.time_ns()))
                try:
                    plan, _ = plan_film(config, parent_store, request, brief, settings,
                                        **({'subject_handoff': handoff} if handoff is not None else {}))
                finally:
                    parent_store.db.close()
            errors = plan_errors(plan, request, brief, settings)
            if errors:
                raise ValueError('Parent plan is invalid: ' + '; '.join(errors))
            write(plan_path, plan)
            event('planning', 'succeeded', chapters=len(plan['chapters']),
                  planned_advisory_seconds=sum(c['advisory_seconds'] for c in plan['chapters']),
                  chapter_outline=[{'id': c['id'], 'title': c['title'], 'advisory_seconds': c['advisory_seconds']} for c in plan['chapters']])
            if args.until == 'plan':
                return
            event('creative', 'running', mood=args.mood)
            shared_path = store.root / 'creative-brief.json'
            shared = read(shared_path) if shared_path.exists() else make_creative_brief(models, evidence, args.mood,
                args.mood_intensity, args.visual_balance, experimental=args.review_mode == 'experimental')
            shared['visual_style'] = plan['film']['shared_visual_style']
            write(shared_path, shared)
            event('creative', 'succeeded', resolved_mood=shared['resolved_mood'])
        orientations = ['landscape', 'portrait'] if args.orientation == 'both' else [args.orientation]
        status_path = store.root / 'longform-status.json'
        old = read(status_path).get('chapters', []) if status_path.exists() else []
        records = []
        for chapter in plan['chapters']:
            project = store.root / 'chapters' / f"{store.root.name}-{chapter['id'].lower()}"
            row = next((copy.deepcopy(r) for r in old if r['chapter']['id'] == chapter['id']), {})
            row.update(chapter=chapter, project=str(project), duration=max(30, round(chapter['advisory_seconds'])))
            records.append(row)
        def save():
            write(status_path, {'chapters': records, 'completed': sum(r.get('status') in ({'audio_ready', 'succeeded'} if audio_only else {'succeeded'}) for r in records),
                               'production_target': 'audio' if audio_only else 'video',
                               'audio_ready': sum(r.get('audio_status') == 'ready' for r in records),
                               'failed': sum(r.get('status') == 'failed' for r in records), 'total': len(records), 'updated': time.time()})
        save()
        for position, row in enumerate(records, 1):
            chapter = row['chapter']; project = Path(row['project'])
            try:
                if audio_only and not chapter_alive(row):
                    from .audio_delivery import checked_audio
                    try:
                        checked_audio(project, [row['duration']])
                    except (ValueError, OSError):
                        pass
                    else:
                        row.update(status='audio_ready', audio_status='ready', error=None, reused=True); save()
                        event('chapters', 'chapter_audio_reused', chapter=chapter['id'], position=position, total=len(records))
                        continue
                complete = None if chapter_alive(row) else checked_delivery(project, orientations)
                if complete and not audio_only:
                    row.update(status='succeeded', error=None, reused=True); save()
                    event('chapters', 'chapter_reused', chapter=chapter['id'], position=position, total=len(records))
                    continue
                if image_resume and chapter['id'] not in selected:
                    raise RuntimeError('Unselected chapter has no checked delivery; select it explicitly')
                context = chapter_context(plan, chapter, shared, brief)
                context_path = write(project / 'chapter-context.json', context)
                child_config = config
                if image_resume or audio_resume:
                    child_config = {**configuration(project / 'config-snapshot.json'), **overrides}
                snapshot = write(project / 'config-snapshot.json', child_config)
                payload = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
                payload.update(generation_mode='short', subject_graph=False, subject=f"{args.subject} — {chapter['title']}", project=str(project),
                    config=str(snapshot), durations=[row['duration']], sources=None, chapter_context=str(context_path),
                    use_evidence=True, until='audio' if audio_only else 'video',
                    audio_checkpoint_only=audio_only,
                    resume_from='scenes' if audio_resume else 'images' if image_resume else None, chapter_plan=None, orientation_brief=None,
                    script_dir=None, visual_plan_dir=None, image_manifest=None)
                calibration = store.root / 'voice_calibration.json'
                if calibration.exists() and not (project / 'voice_calibration.json').exists():
                    write(project / 'voice_calibration.json', read(calibration))
                row.update(status='running', error=None, started=time.time()); save()
                event('chapters', 'running', chapter=chapter['id'], title=chapter['title'],
                      position=position, total=len(records))
                attempt = run_chapter(payload, project, store, chapter['id'], position, len(records), row, save)
                row['attempt_directory'] = str(attempt)
                if audio_only:
                    from .audio_delivery import checked_audio
                    checked_audio(project, [row['duration']])
                    row.update(status='audio_ready', audio_status='ready', error=None, finished=time.time())
                else:
                    if not checked_delivery(project, orientations):
                        raise RuntimeError('Chapter worker finished without a checked delivery')
                    row.update(status='succeeded', error=None, finished=time.time())
                save()
                event('chapters', 'chapter_audio_ready' if audio_only else 'chapter_succeeded',
                      chapter=chapter['id'], position=position, total=len(records))
            except Exception as exc:
                row.update(status='failed', error=f'{type(exc).__name__}: {exc}', finished=time.time()); save()
                event('chapters', 'chapter_failed', chapter=chapter['id'], position=position, total=len(records),
                      error=row['error'], continuing_to_next=True)
            if (project / 'voice_calibration.json').exists():
                write(store.root / 'voice_calibration.json', read(project / 'voice_calibration.json'))
        failures = [r['chapter']['id'] for r in records if r['status'] not in ({'audio_ready', 'succeeded'} if audio_only else {'succeeded'})]
        if failures:
            event('chapters', 'failed', failed_chapters=failures, completed=len(records)-len(failures))
            raise RuntimeError('Long film is incomplete; chapters needing attention: ' + ', '.join(failures) + '. Remaining chapters were processed; resume to reuse successful chapters.')
        event('chapters', 'succeeded', chapters=len(records))
        event('continuity', 'running')
        continuity_review(models, plan, records)
        event('continuity', 'succeeded')
        event('assembly', 'running')
        if audio_only:
            from .audio_delivery import assemble_audio
            destination = assemble_audio(config, store, plan, records, duration, args.music_engine)
            event('assembly', 'succeeded', output_directory=str(destination), audio_first=True,
                  awaiting_video_request=True)
            return
        outputs = [assemble(config, store, plan, records, orientation, duration) for orientation in orientations]
        all_sources = []
        for row in records:
            all_sources.extend({**source, 'id': row['chapter']['id'] + f'_S{index}'}
                for index, source in enumerate(read(Path(row['project']) / 'sources.json')['results'], 1))
        write(store.root / 'sources.json', {'results': all_sources})
        reference_gaps = {}
        references = {}
        for row in records:
            chapter_project = Path(row['project'])
            gaps = read(chapter_project / 'delivery.json').get('person_reference_gaps')
            if gaps:
                reference_gaps[row['chapter']['id']] = gaps
            manifest = chapter_project / 'person-references' / 'manifest.json'
            if manifest.exists():
                references[row['chapter']['id']] = read(manifest)
        if references:
            write(store.root / 'person-references' / 'manifest.json', {'chapters': references})
        write(store.root / 'delivery.json', {'subject': args.subject, 'generation_mode': 'long', 'outputs': outputs,
            'review_mode': args.review_mode, 'tts_engine': args.tts_engine, 'mood': args.mood,
            'image_review': getattr(args, 'image_review', None) if getattr(args, 'image_review', None) is not None else config.get('image_review', True),
            'person_reference_gaps': reference_gaps,
            'resolved_mood': shared['resolved_mood'], 'music_engine': args.music_engine, 'chapters': len(records),
            'human_acceptance': 'pending'})
        destination = preview(store)
        event('assembly', 'succeeded', output_directory=str(destination))
    finally:
        store.db.close()
