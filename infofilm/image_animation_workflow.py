"""CLI-only research → speech/MFA → image-backed educational animation.

Small packets, immutable checkpoints and explicit asset/controller requests.
This workflow does not submit jobs to, or change, the production web interface.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .animation_experiment import captions, load_narration, passing_review, review_errors
from .animation_frontier import chapter_handoff, chapter_requirement
from .animation_narration import OUTPUT_SCHEMA, paragraph_errors
from .animation_workflow import TEACHING_SCHEMA, beat_word_spans, teaching_errors
from .core import ROOT, Store, configuration, digest, read, write
from .image_animation_assets import (IMPORT_SCHEMA, discover_capabilities, generate_assets,
                                     import_assets, prepare_asset_requests)
from .image_animation_contracts import (CANVAS_SCHEMA, GROUNDING_SCHEMA, MOTION_SCHEMA, PARENT_SCHEMA,
    canvas_errors, compile_cues, grounding_errors, nonempty, parent_errors, safe)
from .image_animation_controllers import compile_scene, compile_timeline, controller_capabilities, state_at, value_at
from .image_animation_models import ACTIVITIES, ROUTES, ImageAnimationModels, load_registry
from .llm import ModelOutputError
from .planning import evidence_context

CONTRACT = 'infofilm.image-animation-cli.v1'
STAGES = ['classification', 'research', 'evidence', 'parent', 'teaching', 'narration', 'audio',
          'canvas', 'assets', 'grounding', 'motion', 'render', 'complete']


class Waiting(RuntimeError):
    def __init__(self, state, message, details=None):
        super().__init__(message)
        self.state, self.details = state, details or {}


class ReachedStage(Exception):
    pass


def file_pin(path):
    if path is None:
        return None
    path = Path(path).expanduser().resolve()
    return {'path': str(path), 'sha256': digest(path.read_bytes())}


def extraction_errors(value):
    if not isinstance(value, dict) or not isinstance(value.get('results'), list) or not value['results']:
        return ['Sources must contain nonempty results']
    return ['Each source needs a URL and full raw_content'] if any(not isinstance(r, dict)
        or not nonempty(r.get('url')) or not nonempty(r.get('raw_content')) for r in value['results']) else []


def evidence_errors(value, sources):
    if not isinstance(value, dict) or not isinstance(value.get('facts'), list) or not value['facts']:
        return ['Need source-grounded facts']
    facts = value['facts']
    if len(facts) > 20:
        return ['Keep the evidence packet to at most twenty useful facts']
    ids = [f.get('id') for f in facts if isinstance(f, dict)]
    if len(ids) != len(facts) or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        return ['Fact IDs must be distinct strings']
    return ['Each fact needs a supported claim, source_ids and support note'] if any(
        not nonempty(f.get('claim')) or not nonempty(f.get('evidence_note'))
        or not isinstance(f.get('source_ids'), list) or not f['source_ids']
        or any(s not in sources for s in f['source_ids']) for f in facts) else []


class Workflow:
    def __init__(self, args, *, models_factory=ImageAnimationModels):
        self.args = args
        self.root = args.output.expanduser().resolve()
        self.store = Store(self.root)
        self.config = configuration(args.config)
        saved_registry = self.root / 'provider-registry.json'
        registry_path = args.providers or (saved_registry if args.resume and saved_registry.exists() else None)
        self.registry = load_registry(registry_path, overrides=args.routes)
        self.models = models_factory(self.config, self.store, registry=self.registry)
        self.status = {'schema_version': CONTRACT, 'state': 'running', 'chapters': [], 'updated': time.time()}

    def save_status(self, **changes):
        self.status.update(changes, updated=time.time())
        write(self.root / 'state.json', self.status)

    def checkpoint(self, directory, name, inputs, work, validator=None):
        """Completed stages survive routing changes; incomplete calls use the new route."""
        directory = Path(directory)
        identity = digest({'contract': CONTRACT, 'inputs': inputs})
        path = directory / 'checkpoints' / (name + '.json')
        if path.exists():
            old = read(path)
            if old['inputs_sha256'] != identity:
                raise ValueError(f'{name} inputs changed after freezing; use a new --output')
            if old.get('state') == 'succeeded':
                result = old['result']
                if validator:
                    errors = validator(result)
                    if errors:
                        raise ValueError(f'Saved {name} no longer validates: {errors}')
                for item in old.get('files', []):
                    if file_pin(item['path']) != item:
                        raise ValueError(f'Saved {name} output changed: {item["path"]}')
                self.store.event('image-animation:' + name, 'reused', {'directory': str(directory)})
                return result
        write(path, {'inputs_sha256': identity, 'state': 'running'})
        self.store.event('image-animation:' + name, 'running', {'directory': str(directory)})
        result = work()
        if validator:
            errors = validator(result)
            if errors:
                raise ValueError(name + ': ' + '; '.join(errors))
        write(path, {'inputs_sha256': identity, 'state': 'succeeded', 'result': result,
                     'files': result.pop('_file_pins', []) if isinstance(result, dict) else [],
                     'routes': self.models.describe_routes()})
        write(directory / (name + '.json'), result)
        self.store.event('image-animation:' + name, 'succeeded', {'directory': str(directory)})
        return result

    def json_plan(self, directory, activity, name, task, context, validator, *, images=(), budget=5000):
        facade = self.models.for_activity(activity)
        signature = digest({'task': task, 'context': context, 'route': facade.fingerprint,
                            'images': [file_pin(p) for p in images]})
        attempt_dir = Path(directory) / 'planning' / name / signature[:16]
        feedback, history = '', []
        for number in range(self.args.max_attempts):
            try:
                candidate = facade.call_json('planner', name + '-' + signature[:8] + '-' + str(number),
                    task + '\nINPUT JSON:\n' + json.dumps(context, ensure_ascii=False) + feedback,
                    image_path=images[0] if images else None,
                    reference_image_paths=images[1:] if images else None, max_tokens=budget)
                errors = validation_errors(validator, candidate)
                if errors and isinstance(candidate, dict):
                    from .animation_schema_repair import LocalSchemaRepair
                    repaired, receipt = LocalSchemaRepair(facade, self.store, max_attempts=1).repair(
                        name, candidate, context, errors,
                        lambda value: validation_errors(validator, value), attempt_dir)
                    if receipt and not validation_errors(validator, repaired):
                        candidate = repaired
                        errors = []
            except (ModelOutputError, ValueError) as exc:
                candidate, errors = None, [str(exc)]
                if getattr(exc, 'output_budget_exhausted', False):
                    write(attempt_dir / 'evaluation.json', {'accepted': False, 'errors': errors,
                        'retry_policy': 'Do not repeat an identical truncated call; reduce packet or change output budget'})
                    raise Waiting('needs_review', f'{name} exceeded its model output budget',
                                  {'evaluation': str(attempt_dir / 'evaluation.json')}) from exc
            review = None
            if candidate is not None:
                write(attempt_dir / f'candidate-{number}.json', candidate)
            if not errors and self.args.review != 'off' and activity != 'review':
                try:
                    review = facade.call_json('reviewer', name + '-review-' + signature[:8] + '-' + str(number),
                    'Review ONLY this phase for concrete factual or teaching defects, source support, scope, '
                    'and the current renderer capabilities. Be proportionate for casual learners. Runtime is advisory. '
                    'Missing assets/controllers are valid requests. Do not demand coordinates before grounding, '
                    'or numerical animation geometry when a controller owns it. For images inspect visible components, '
                    'count them, compare anchors and check complete silhouettes; do not infer absent parts. '
                    'Return JSON {pass:boolean,score:0..100,issues:[{severity:major|minor,field,problem,correction}],strengths:[string]}. '
                    'Pass requires score>=82 and no major defects.\nTASK:\n' + task + '\nINPUT:\n' +
                    json.dumps(context, ensure_ascii=False) + '\nCANDIDATE:\n' + json.dumps(candidate, ensure_ascii=False),
                    image_path=images[0] if images else None, reference_image_paths=images[1:] if images else None,
                        max_tokens=2200)
                    errors += validation_errors(review_errors, review)
                    if not errors and not passing_review(review):
                        errors.append('Semantic review needs a scoped correction')
                except (ModelOutputError, ValueError) as exc:
                    errors.append('Reviewer output: ' + str(exc))
                    if getattr(exc, 'output_budget_exhausted', False):
                        write(attempt_dir / 'evaluation.json', {'accepted': False, 'errors': errors})
                        raise Waiting('needs_review', name + ' reviewer exceeded its output budget',
                            {'evaluation': str(attempt_dir / 'evaluation.json')}) from exc
            history.append({'attempt': number + 1, 'errors': errors, 'review': review})
            write(attempt_dir / 'evaluation.json', {'accepted': not errors, 'history': history})
            if not errors:
                write(attempt_dir / 'selected.json', candidate)
                return candidate
            self.store.event('image-animation:' + name, 'needs_repair', {'attempt': number + 1,
                'max_attempts': self.args.max_attempts, 'errors': errors, 'review_score': (review or {}).get('score')})
            feedback = '\nREPAIR ONLY the defects, preserving accepted scope and identities:\n' + json.dumps({
                'last_candidate': candidate, 'code_errors': errors, 'review': review}, ensure_ascii=False)
        raise Waiting('needs_review', name + ' needs review after bounded attempts',
                      {'evaluation': str(attempt_dir / 'evaluation.json')})

    def at(self, stage):
        if self.args.until == stage:
            raise ReachedStage(stage)

    def review_images(self, directory, name, context, images):
        """A failing verdict requests a repair of its artifact; never retry to seek a higher score."""
        facade = self.models.for_activity('review')
        identity = digest({'context': context, 'images': [file_pin(p) for p in images], 'route': facade.fingerprint})
        def assess():
            result = self.json_plan(directory, 'review', name,
                'Inspect the supplied actual images in order against this exact learning message, component '
                'requirements and controller state. Count components, verify placement/anchors and visibility. '
                'Check complete silhouettes and meaningful causal relationships. Be proportionate for casual '
                'learners, but reject visible defects that teach the wrong thing. Labels are composited cards. '
                'Return JSON {pass:boolean,score:0..100,issues:[{severity:major|minor,field,problem,correction}],strengths:[string]}. '
                'Pass needs score>=82 and no major issue.', context, review_errors, images=images, budget=2200)
            verdict = write(Path(directory) / (name + '-verdict.json'), result)
            if not passing_review(result):
                raise Waiting('needs_review', 'Actual ' + name + ' failed visual inspection',
                    {'verdict': str(verdict), 'instruction': 'Repair the responsible grounding/motion import and --resume; accepted narration is retained'})
            return result
        return self.checkpoint(directory, name + '-' + identity[:12], {'identity': identity}, assess, review_errors)

    def research(self, directory, subject, *, sources=None, guidance=None):
        def run():
            if sources:
                return read(sources)
            target = Path(directory) / 'research-source-archive'
            prior_request = read(target / 'request.json') if (target / 'request.json').exists() else None
            previous_route = (ImageAnimationModels(prior_request['config'], self.store,
                registry=prior_request['registry']).for_activity('research').fingerprint
                if prior_request and prior_request.get('subject') == subject else None)
            request = write(target / 'request.json', {'directory': str(target), 'subject': subject,
                'config': self.config, 'registry': self.registry, 'guidance': guidance,
                'previous_route': previous_route})
            interpreter = Path(self.config['kbagent_root']) / 'env/bin/python'
            if not interpreter.is_file():
                raise RuntimeError('kbagent Python missing; supply --sources to use saved research')
            subprocess.run([str(interpreter), '-u', '-m', 'infofilm.image_animation_research', str(request)],
                           cwd=ROOT, check=True)
            return read(target / 'extracted.json')
        return self.checkpoint(directory, 'research', {'subject': subject, 'sources': file_pin(sources),
            'guidance': guidance}, run, extraction_errors)

    def evidence(self, directory, extraction, subject, handoff=None):
        context = evidence_context(extraction, subject)
        sources = {s['id'] for s in context['sources']}
        return self.checkpoint(directory, 'evidence', {'extraction': digest(extraction), 'handoff': handoff},
            lambda: self.json_plan(directory, 'evidence', 'evidence',
                'Prepare a concise source-grounded evidence packet for the exact subject and owned chapter questions. '
                'Include essential qualifications. Do not treat source digests, scope or graph prompts as evidence '
                'independently of their cited passages. Return JSON {title,facts:[{id,claim,source_ids:[S1],'
                'evidence_note}],uncertainties:[string]}. Select useful facts without padding to meet a count.',
                {'subject': subject, 'source_evidence': context, 'chapter_context': handoff},
                lambda v: evidence_errors(v, sources)), lambda v: evidence_errors(v, sources))

    def classification(self, subject):
        if not self.args.subject_graph:
            return None
        from .graph_planning import freeze_graph
        from .subject_taxonomy import classify, classification_errors, seed_subject
        config = freeze_graph(self.config, self.root / 'subject-graph')
        graph = config['subject_graph_snapshot']
        def make():
            picked = self.json_plan(self.root, 'classification', 'classification',
                'Classify the exact subject within this graph. Choose a useful terminal category and independent '
                'secondary facets. Ambiguity should remain explicit. Return JSON '
                '{primary,secondary:[category],lens,scope_extent:broad|focused|unknown,ambiguity:clear|ambiguous|unknown,rationale}.',
                {'subject': subject, 'categories': {k: {'label': v.get('label'), 'parent': v.get('parent')}
                    for k, v in graph['nodes'].items()}, 'lenses': list(graph['lenses']), 'initial_hint': classify(graph, subject)},
                lambda v: classification_errors(v, graph), budget=2200)
            return seed_subject(subject, graph, classification=picked)
        return self.checkpoint(self.root, 'classification', {'subject': subject, 'graph': digest(graph)}, make)

    def parent(self, subject, extraction, evidence, profile):
        sources = {s['id'] for s in evidence_context(extraction, subject)['sources']}
        return self.checkpoint(self.root, 'parent', {'subject': subject, 'evidence': evidence,
            'profile': profile, 'mode': self.args.mode, 'seconds': self.args.duration_seconds},
            lambda: self.json_plan(self.root, 'parent', 'parent',
                'Plan this educational film into coherent non-confusing chapters. Choose chronology, mechanism, '
                'comparison, or another grouping based on the subject and user scope. The graph is guidance only. '
                'Dates are optional and never required for a conceptual subject. Give each chapter owned questions '
                'and smooth entry/exit context; avoid redundant introductions/conclusions. '
                'Single mode means exactly one chapter. Long mode allows 1–12 chapters suited to the teaching flow. '
                'Runtime is advisory; do not pad chapters. Choose illustrated for contextual introductions or closure '
                'when motion adds no teaching value, animated for a useful causal demonstration. Unsupported controllers '
                'are legitimate later requests. Return the supplied JSON shape.',
                {'subject': subject, 'audience': self.args.audience, 'mode': self.args.mode,
                 'advisory_seconds': self.args.duration_seconds, 'evidence': evidence, 'subject_profile': profile,
                 'controller_capabilities': controller_capabilities(), 'output_schema': PARENT_SCHEMA},
                lambda v: parent_errors(v, subject, sources, self.args.mode), budget=7000),
            lambda v: parent_errors(v, subject, sources, self.args.mode))

    def audio(self, directory, script):
        def make():
            if self.args.narration_input:
                bundle = load_narration(self.args.narration_input)
                if bundle['transcript'] != '\n\n'.join(p['narration'].strip() for p in script['points']):
                    raise ValueError('Supplied narration differs from the accepted script')
            else:
                # Existing point speech path includes Whisper audits, failure-only LLM adjudication,
                # silence gating, retries and per-point CPU MFA. Its GPU preflight preserves other work.
                command = [sys.executable, '-u', str(ROOT / 'scripts/prepare-animation-narration.py'),
                    '--script-plan', str(directory / 'narration.json'), '--config', str(self.root / 'engine-config.json'),
                    '--output', str(directory / 'speech')]
                try:
                    subprocess.run(command, cwd=ROOT, check=True)
                except subprocess.CalledProcessError as exc:
                    raise Waiting('audio_needs_attention', 'Speech/MFA did not complete; accepted plans are retained',
                                  {'directory': str(directory / 'speech')}) from exc
                bundle = load_narration(directory / 'speech/narration-input.json')
            # Route provenance lives on the script checkpoint; do not mislabel a local script as Luna.
            bundle = copy.deepcopy(bundle)
            bundle.setdefault('provenance', {})['script_source'] = 'activity-routed narration checkpoint'
            return {**bundle, '_file_pins': [file_pin(bundle['audio']), file_pin(bundle['timing'])]}
        return self.checkpoint(directory, 'audio', {'script': script,
            'supplied_bundle': file_pin(self.args.narration_input)}, make,
            lambda v: [] if load_narration_value(v) else ['Invalid narration bundle'])

    def assets(self, directory, requests, *, seed_canvas=None):
        directory = Path(directory)
        package = prepare_asset_requests(requests, directory, self.config)
        if seed_canvas:
            record = next(r for r in package['requests'] if r['kind'] == 'canvas')
            destination = directory / 'imported-canvas.png'
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copyfile(seed_canvas, destination)
            if digest(destination.read_bytes()) != digest(Path(seed_canvas).read_bytes()):
                raise ValueError('Imported canvas differs from the approved source')
            seed = write(directory / 'canvas-import.json', {'schema_version': IMPORT_SCHEMA,
                'requests_sha256': package['requests_sha256'], 'assets': [{'id': record['id'],
                'file': destination.name, 'request_sha256': record['request_sha256']}]})
            import_assets(seed, package, directory)
        if (directory / 'asset-import.json').exists():
            import_assets(directory / 'asset-import.json', package, directory)
        local = Store(directory)
        try:
            result = generate_assets(self.config, local, package, execute=self.args.generate_assets,
                capabilities=None)
        finally:
            local.db.close()
        if result['unresolved']:
            raise Waiting(result['status'], 'Image assets need execution or cache fill', {
                'requests': str(directory / 'missing-asset-requests.json'),
                'import_template': str(directory / 'asset-import-template.json'),
                'instruction': 'Fill the template, save it as asset-import.json alongside it, then --resume'})
        return result

    def canvas_request(self, canvas):
        return {'id': 'canvas', 'kind': 'canvas', 'prompt': canvas['prompt'],
                'width': self.args.canvas_width, 'height': self.args.canvas_height,
                'reference_ids': [], 'transparent_required': False,
                'controller': {'type': canvas['controller_type']}}

    def grounded_layers(self, directory, canvas, image, requirement):
        dimensions = [self.args.canvas_width, self.args.canvas_height]
        manual = directory / 'grounding-import.json'
        inputs = {'canvas': canvas, 'image': file_pin(image), 'requirement': requirement}
        def make():
            if manual.exists():
                return read(manual)
            return self.json_plan(directory, 'grounding', 'grounding',
                'Inspect the supplied actual image. Measure source-pixel geometry (top-left origin), identify moving '
                'parts and anchors, and bind a supported controller. You do not calculate frame-by-frame physics. '
                'Plan a clean static background edit and COMPLETE isolated native-alpha part edits, including any '
                'shape hidden by other parts. Parts use the full source canvas at their original placement, with '
                'transparent surroundings. Explicitly mark unclear/occluded geometry in incomplete_flags; never '
                'guess it away. Preserve measured part positions: the controller start_px is the actual source '
                'position of its orbiter, not a new position invented to fit an estimated radius. For a circular '
                'orbit you may omit radius_px and radius_xy_px; code derives the radius from center_px and start_px. '
                'Part landmarks name points on that part; controller center_px names the stationary path center. '
                'Supply every landmark requested in canvas_plan.component_roles, even when two names refer to '
                'the same point. A background edit retains the fixed subject while clearing the extracted moving '
                'part; a part edit keeps the original full-canvas placement and pose. '
                'Do not include text as moving pixels; code draws label cards. For engine parts require '
                'piston wrist_pin_px, crankshaft rotation_anchor_px, and rod endpoint_a_px/endpoint_b_px. '
                'Return the supplied JSON schema with actual measured controller parameters and scene asset IDs.',
                {'learning_objective': requirement, 'canvas_plan': canvas,
                 'actual_source_dimensions': dimensions, 'controllers': controller_capabilities(),
                 'output_schema': GROUNDING_SCHEMA}, lambda v: grounding_errors(v, canvas, dimensions),
                images=[image], budget=5500)
        # Explicit manual repairs retain the original proposal and create a new layer request namespace.
        revision = '-manual-' + digest(read(manual))[:12] if manual.exists() else ''
        ground = self.checkpoint(directory, 'grounding' + revision, inputs, make,
                                  lambda v: grounding_errors(v, canvas, dimensions))
        from .image_animation_contracts import grounding_incomplete_flags
        incomplete = grounding_incomplete_flags(ground)
        if incomplete:
            raise Waiting('waiting_grounding', 'Source geometry needs inspection before producing moving layers',
                {'grounding': str(directory / ('grounding' + revision + '.json')), 'flags': incomplete,
                 'instruction': 'Save a corrected grounding-import.json, then --resume'})
        requests = [self.canvas_request(canvas), {'id': 'background', 'kind': 'background',
            'prompt': ground['background_prompt'], 'width': dimensions[0], 'height': dimensions[1],
            'reference_ids': ['canvas'], 'transparent_required': False}]
        for part in ground['parts']:
            requests.append({'id': part['id'], 'kind': 'part', 'prompt': part['prompt'],
                'width': dimensions[0], 'height': dimensions[1], 'reference_ids': ['canvas'],
                'transparent_required': True, 'landmarks': part.get('landmarks', {}),
                'required_landmarks': next(p['required_landmarks'] for p in canvas['component_roles'] if p['role'] == part['role']),
                'grounding': {'source_bbox': part['bbox_xyxy_px'], 'full_canvas_rgba': True},
                'controller': {'type': canvas['controller_type'], 'role': part['role']}})
        layer_dir = directory / ('layers-' + digest(ground)[:12])
        manifest = self.assets(layer_dir, requests, seed_canvas=image)
        scene = copy.deepcopy(ground['scene'])
        # Controller names describe mechanics, not the teaching claim. Preserve
        # the accepted message and simplification limits for later visual review.
        scene.setdefault('provenance', {}).update({
            'canvas_id': canvas['id'], 'visual_message': canvas['visual_message'],
            'limitations': canvas.get('limitations', []),
            'grounding_revision': ground.get('provenance', {})})
        for identity, asset in scene['assets'].items():
            asset['path'] = manifest['assets'][identity]['file']
            if identity != 'background':
                asset['landmarks'] = manifest['assets'][identity].get('landmarks', asset.get('landmarks', {}))
        if scene.get('incomplete_flags'):
            raise Waiting('waiting_grounding', 'Measured geometry or complete component shapes need attention',
                          {'scene': str(directory / 'grounding.json'), 'flags': scene['incomplete_flags']})
        # Actual layer review occurs once after edits; a failed layer pauses for scoped cache repair.
        if self.args.review != 'off':
            from .image_animation_review import build_layer_review_sheet
            review_inputs = {'grounding': ground, 'canvas_plan': canvas,
                'learning_objective': requirement,
                'assets': {i: a['sha256'] for i, a in manifest['assets'].items()}}
            sheet, visualization = build_layer_review_sheet(scene, directory / 'scene.json', image,
                layer_dir / 'review-visualization')
            self.review_images(directory, 'layer-review', {**review_inputs,
                'visualization': visualization,
                'image_order': ['labeled layer contact sheet; transparent pixels composited for inspection']}, [sheet])
        compile_scene(scene)
        write(directory / 'scene.json', scene)
        return scene

    def render_canvas(self, directory, scene, timing, orientations):
        scene_path = write(directory / 'scene.json', scene)
        timing_path = write(directory / 'timing.json', timing)
        outputs = []
        # Keep the venv executable symlink: resolving it selects the base Python and loses Manim.
        interpreter = self.args.manim_python.expanduser().absolute()
        if not interpreter.is_file():
            raise Waiting('waiting_runtime', 'Manim Python is missing', {'python': str(interpreter)})
        for orientation in orientations:
            width, height = ((self.args.width, self.args.height) if orientation == 'landscape'
                             else (self.args.height, self.args.width))
            target = directory / 'render' / orientation
            command = [str(interpreter), '-u', str(ROOT / 'scripts/render-image-animation.py'),
                '--scene', str(scene_path), '--timings', str(timing_path), '--output-dir', str(target),
                '--width', str(width), '--height', str(height), '--fps', str(self.args.fps)]
            subprocess.run(command, cwd=ROOT, check=True)
            result = read(target / 'render-results.json')
            if result['status'] != 'passed':
                raise Waiting('needs_review', 'Rendered canvas did not pass preflight/clock checks', {'result': str(target)})
            if self.args.review != 'off':
                from .image_animation_review import select_review_times, build_rendered_review_sheet
                controller = compile_scene(scene)
                clock = compile_timeline(timing, controller, fps=self.args.fps)
                duration = clock['source_duration_seconds']
                samples, states = [], []
                for index, seconds in enumerate(select_review_times(clock, self.args.fps)):
                    frame = target / f'review-frame-{index}.png'
                    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-ss', str(seconds),
                        '-i', str(target / 'animation-silent.mp4'), '-frames:v', '1', str(frame)], check=True)
                    if not frame.is_file():
                        raise ValueError('Rendered-frame inspection could not extract a visible frame')
                    samples.append(frame)
                    owner = next(s for s in clock['scenes'] if s['start'] <= seconds <= s['end'])
                    scalar = value_at(seconds, owner['segments'], owner['initial_value'])
                    states.append({'seconds': seconds, 'teaching': owner['title'], 'controller_value': scalar,
                        'expected_state': state_at(controller, scalar),
                        'active_labels': [a['label'] for a in owner.get('actions', [])
                            if a.get('label') and a['start'] <= seconds <= a['end']]})
                review_context = {'orientation': orientation, 'controller': controller, 'samples': states,
                    'presentation_policy': {
                        'output_dimensions_px': [width, height],
                        'title_role': 'The title explains the entire scene in a separate label region; it is not an object-attached label and does not need a leader or anchor to a globe.',
                        'cue_cards': 'Narration-timed explanatory cards also occupy the separate label region. Inspect the recorded active labels at their sample times.',
                        'legibility': 'Contact-sheet frames are reduced thumbnails. Assess legibility at the recorded original video resolution; do not treat the thumbnail scale as the delivery scale.'}}
                if scene.get('provenance'):
                    review_context['teaching_context'] = scene['provenance']
                sheet, visualization = build_rendered_review_sheet(samples, states, target / 'frame-review-visualization')
                review_context['visualization'] = visualization
                self.review_images(target, 'rendered-frame-review', review_context, [sheet])
            outputs.append({'orientation': orientation, 'video': str(target / 'animation-silent.mp4'),
                            'result': str(target / 'render-results.json')})
        return outputs

    def chapter(self, row, handoff, extraction, continuity):
        directory = self.root / 'chapters' / row['id']
        handoff_file = directory / 'chapter-handoff.json'
        scoped = read(handoff_file) if handoff_file.exists() else {**handoff, 'continuity_digest': continuity}
        scoped.pop('chapter_evidence', None)
        if not self.args.sources and self.args.mode == 'long' and not self.args.chapter_input:
            extraction = self.research(directory, handoff['research_subject'])
        evidence = self.evidence(directory, extraction, handoff['research_subject'], scoped)
        scoped['chapter_evidence'] = evidence
        requirement = chapter_requirement(scoped)
        write(handoff_file, scoped)
        teaching = self.checkpoint(directory, 'teaching', {'requirement': requirement}, lambda: self.json_plan(directory,
            'teaching', 'teaching', 'Plan a clear teaching sequence of visible demonstrations before writing narration. '
            'Use controller-backed motion when it explains something, static illustrated holds otherwise. Avoid '
            'unnecessary animation. Respect what previous chapters already taught. Guided actions may become a '
            'loop or replay later for a stated purpose. Request unknown behaviors honestly. Return the supplied JSON.',
            {'requirement': requirement, 'production_kind': row.get('production_kind', 'animated'),
             'controllers': controller_capabilities(), 'output_schema': TEACHING_SCHEMA},
            lambda v: teaching_errors(v, requirement)), lambda v: teaching_errors(v, requirement))
        self.at('teaching')
        script = self.checkpoint(directory, 'narration', {'requirement': requirement, 'teaching': teaching},
            lambda: self.json_plan(directory, 'narration', 'narration',
                'Write a natural, source-grounded spoken explanation around the accepted teaching sequence. '
                'Each coherent paragraph belongs to exactly one beat and follows teaching order. Numbers must be '
                'written as they are naturally spoken. Respect exclusions and chapter continuity. No stage directions. '
                'Runtime is advisory, not a word-count cut-off. Return the supplied JSON.',
                {'requirement': requirement, 'teaching': teaching, 'advisory_seconds': row['advisory_seconds'],
                 'output_schema': OUTPUT_SCHEMA}, lambda v: paragraph_errors(v, requirement, teaching)),
            lambda v: paragraph_errors(v, requirement, teaching))
        self.at('narration')
        bundle = self.audio(directory, script)
        self.at('audio')
        canvases = self.checkpoint(directory, 'canvas', {'requirement': requirement, 'teaching': teaching,
            'audio_sha256': bundle['audio_sha256'], 'words': bundle['words']}, lambda: self.json_plan(directory,
            'canvas', 'canvas', 'Plan a few reusable image-backed canvases after narration/MFA. A canvas may own '
            'several adjacent beats. Use one clear visual idea per canvas, but reuse its components for several '
            'guided demonstrations. Supply self-contained positive image descriptions with contextual styling. '
            'Image prompts describe visible illustration only; code label wording and explanatory caveats belong '
            'outside the prompt. Typography is composited by code. Select exactly one controller ID per canvas, '
            'with no prose or alternatives in controller_type. Include ONLY its executable moving roles, using '
            'the exact supported role IDs and required landmarks; static_canvas has an empty component_roles list. '
            'Unavailable future enhancements belong in limitations, not controller_type or component_roles. '
            'Preserve the accepted teaching choice of a held comparison or an explicitly simplified proxy. '
            'If no supported controller expresses the concept, name the needed controller; do not disguise it as '
            'a static substitute. Illustrated chapters may use static_canvas. Return the supplied JSON.',
            {'requirement': requirement, 'teaching': teaching, 'script': script,
             'actual_duration_seconds': bundle['duration_seconds'], 'controllers': controller_capabilities(),
             'output_schema': CANVAS_SCHEMA}, lambda v: canvas_errors(v, teaching)), lambda v: canvas_errors(v, teaching))
        self.store.event('image-animation:canvas', 'planned', {'chapter': row['id'], 'canvases': len(canvases['canvases']),
            'moving_roles': sum(len(c['component_roles']) for c in canvases['canvases'])})
        self.at('canvas')
        spans = beat_word_spans(script, bundle)
        beats = teaching['teaching_sequence']
        windows = narration_windows(beats, spans, bundle['duration_seconds'], self.args.fps)
        render_outputs = []
        for position, canvas in enumerate(canvases['canvases'], 1):
            self.store.event('image-animation:canvas', 'processing', {'chapter': row['id'], 'canvas': canvas['id'],
                             'position': position, 'total': len(canvases['canvases'])})
            target = directory / 'canvases' / canvas['id']
            canvas_repair = target / 'canvas-prompt-import.json'
            canvas_asset_name = 'canvas-assets'
            if canvas_repair.exists():
                from .image_animation_repairs import apply_canvas_prompt_repair
                repair_document = read(canvas_repair)
                canvas = apply_canvas_prompt_repair(canvas, repair_document)
                canvas_asset_name += '-manual-' + digest(repair_document)[:12]
            if canvas['controller_type'] not in controller_capabilities():
                request = write(target / 'controller-request.json', {'canvas': canvas,
                    'supported': controller_capabilities(), 'instruction': 'Implement and verify a controller, then --resume'})
                raise Waiting('waiting_controller', 'This concept needs a new motion controller', {'request': str(request)})
            manifest = self.assets(target / canvas_asset_name, [self.canvas_request(canvas)])
            image = Path(manifest['assets']['canvas']['file'])
            self.at('assets')
            if canvas['controller_type'] == 'static_canvas':
                scene = {'schema_version': 'infofilm.image-animation-scene.v1',
                    'canvas': {'width': self.args.canvas_width, 'height': self.args.canvas_height},
                    'assets': {'background': {'path': str(image)}},
                    'controller': {'type': 'static_canvas', 'parameters': {}, 'bindings': {}}, 'incomplete_flags': []}
            else:
                scene = self.grounded_layers(target, canvas, image, requirement)
            if canvas_repair.exists():
                scene.setdefault('provenance', {}).update({
                    'canvas_prompt_repairs': canvas['provenance']['canvas_prompt_repairs'],
                    'canvas_asset_manifest': str(target / canvas_asset_name / 'asset-manifest.json'),
                    'visual_message': canvas['visual_message'], 'limitations': canvas.get('limitations', [])})
            self.at('grounding')
            controller = compile_scene(scene)
            initial = controller['initial_value']
            scene_rows = []
            canvas_start = windows[canvas['beat_ids'][0]][0]
            canvas_end = windows[canvas['beat_ids'][-1]][1]
            for beat_id in canvas['beat_ids']:
                beat = next(b for b in beats if b['id'] == beat_id)
                start, end = windows[beat_id]
                beat_controller = {**controller, 'initial_value': initial}
                def validate_cues(value):
                    try:
                        compile_cues(value, beat_id, spans[beat_id], start, end, beat_controller, fps=self.args.fps)
                        return []
                    except (ValueError, TypeError, KeyError) as exc:
                        return [str(exc)]
                imported = target / ('motion-import-' + beat_id + '.json')
                revision = '-manual-' + digest(read(imported))[:12] if imported.exists() else ''
                motion_name = 'motion-' + beat_id + '-' + digest(beat_controller)[:8] + revision
                cues = self.checkpoint(target, motion_name, {'beat': beat, 'controller': beat_controller,
                    'words': spans[beat_id], 'window': [start, end]}, lambda: read(imported) if imported.exists() else self.json_plan(target, 'motion',
                    'motion-' + beat_id, 'Plan ONLY this beat’s narration-synchronized actions. Choose actual word IDs '
                    'for cue edges. The controller owns geometry/physics; select scalar progress/angle and guided, '
                    'hold, replay or loop behavior. Continue from the supplied initial value. Any reset must be '
                    'an explicit replay; hold the last state between actions. Loop mechanisms only where it helps '
                    'the accepted explanation. Static canvases have only hold/highlight/label actions. '
                    'Short text uses code-rendered label cards. Return the supplied JSON.',
                    {'beat': beat, 'controller': beat_controller, 'incoming_value': initial,
                     'incoming_state': state_at(beat_controller, initial),
                     'value_unit': controller_capabilities()[controller['type']]['value_unit'],
                     'actual_mfa_words': spans[beat_id],
                     'output_schema': MOTION_SCHEMA}, validate_cues, budget=3200), validate_cues)
                compiled = compile_cues(cues, beat_id, spans[beat_id], start, end, beat_controller, fps=self.args.fps)
                checked = compile_timeline(compiled, beat_controller, fps=self.args.fps)
                initial = checked['scenes'][-1]['final_value']
                packet = compiled['scenes'][0]
                offset = start-canvas_start
                packet['start'], packet['end'] = offset, end-canvas_start
                packet['title'] = beat['message']
                for action in packet['actions']:
                    action['start'] += offset; action['end'] += offset
                scene_rows.append(packet)
            timing = {'schema_version': 'infofilm.image-animation-timing.v1',
                'duration_seconds': canvas_end-canvas_start, 'scenes': scene_rows,
                'narration_provenance': {'audio_sha256': bundle['audio_sha256'],
                    'timing_sha256': bundle['timing_sha256'], 'source_start_seconds': canvas_start,
                    'source_end_seconds': canvas_end}}
            compile_timeline(timing, controller, fps=self.args.fps)
            timing = allocate_canvas_frames(timing, canvas_start, canvas_end, self.args.fps)
            write(target / 'timing.json', timing)
            self.at('motion')
            render_outputs.append(self.render_canvas(target, scene, timing, self.orientations))
        self.at('render')
        delivery = self.deliver_chapter(directory, script, bundle, render_outputs, row)
        return {'id': row['id'], 'title': row['title'], 'state': 'succeeded', 'delivery': delivery,
                'duration_seconds': bundle['duration_seconds'],
                'continuity': {'chapter_id': row['id'], 'covered_messages': [b['message'] for b in beats],
                               'spoken_points': [p['narration'] for p in script['points']]}}

    def deliver_chapter(self, directory, script, bundle, canvas_outputs, chapter):
        from .media import audio_info
        from .music import prepare_music
        film = {'audio': Path(bundle['audio']), 'audio_info': audio_info(bundle['audio']),
                'actual_seconds': bundle['duration_seconds'], 'requested_seconds': chapter['advisory_seconds'],
                'script': script}
        music = prepare_music(self.config, self.store_for(directory), film,
            {'music': {'caption': 'Sparse instrumental educational underscore; mood supports ' + chapter['objective'],
                       'bpm': 80, 'keyscale': 'C major', 'timesignature': '4'}}, engine=self.args.music)
        write(directory / 'music.json', music)
        subtitles = write_subtitles(directory / 'narration.srt', bundle['words'])
        outputs = []
        for orientation in self.orientations:
            clips = [Path(next(o['video'] for o in group if o['orientation'] == orientation)) for group in canvas_outputs]
            output = directory / 'delivery' / orientation / 'film.mp4'
            joined = join_videos(clips, output, audio=Path(music['master_audio']), subtitles=subtitles,
                expected_frames=round(bundle['duration_seconds']*self.args.fps), fps=self.args.fps)
            outputs.append({'orientation': orientation, **joined})
        result = {'outputs': outputs, 'music': music, 'narration_sha256': bundle['audio_sha256']}
        write(directory / 'delivery.json', result)
        return result

    def store_for(self, directory):
        # Music needs a chapter root but can log through the parent's existing sqlite connection.
        parent = self.store
        class ChapterStore:
            root = Path(directory)
            event = parent.event
        return ChapterStore()

    @property
    def orientations(self):
        return ['landscape', 'portrait'] if self.args.orientation == 'both' else [self.args.orientation]

    def run(self):
        pins = {'sources': file_pin(self.args.sources), 'chapter_input': file_pin(self.args.chapter_input),
                'narration_input': file_pin(self.args.narration_input)}
        frozen = {'contract': CONTRACT, 'subject': self.args.subject, 'mode': self.args.mode,
            'duration_seconds': self.args.duration_seconds, 'audience': self.args.audience,
            'orientation': self.args.orientation, 'fps': self.args.fps, 'size': [self.args.width, self.args.height],
            'canvas_size': [self.args.canvas_width, self.args.canvas_height],
            'subject_graph': self.args.subject_graph, 'chapter_id': self.args.chapter_id,
            'music': self.args.music, 'review': self.args.review, 'pins': pins}
        inputs = self.root / 'inputs.json'
        if inputs.exists():
            if not self.args.resume:
                raise ValueError('Output already has a run; add --resume or choose a new --output')
            if read(inputs) != frozen:
                raise ValueError('Frozen subject/input/settings changed; restore them or choose a new --output')
            engines = read(self.root / 'engine-config.json')
            # Preserve speech/image settings, refresh only LLM endpoints for pending activity routes.
            self.config = {**engines, 'planner_url': self.config['planner_url'], 'reviewer_url': self.config['reviewer_url']}
        else:
            write(inputs, frozen)
            write(self.root / 'engine-config.json', self.config)
        write(self.root / 'model-routes.json', self.models.describe_routes())
        write(self.root / 'provider-registry.json', self.registry)
        self.save_status()
        if self.args.chapter_input:
            supplied = read(self.args.chapter_input)
            if supplied.get('schema_version') == PARENT_SCHEMA['schema_version']:
                supplied = {**supplied, 'schema_version': 'infofilm.chapter-plan.v0'}
            handoff = chapter_handoff(supplied, self.args.chapter_id)
            if not safe(handoff['chapter']['id']):
                raise ValueError('Imported chapter ID must be a safe identifier, never a filesystem path')
            subject = handoff['film']['subject']
            handoff.setdefault('research_subject', subject + ': ' + handoff['chapter']['title'] +
                               '. Scope: ' + handoff['chapter']['scope']['description'])
            plan = {'film': handoff['film'], 'chapters': [{**handoff['chapter'],
                'advisory_seconds': handoff['chapter'].get('advisory_seconds', self.args.duration_seconds)}]}
            extraction = self.research(self.root, handoff.get('research_subject', subject), sources=self.args.sources)
            handoffs = [handoff]
        else:
            subject = self.args.subject
            profile = self.classification(subject)
            self.at('classification')
            extraction = self.research(self.root, subject, sources=self.args.sources, guidance=profile)
            self.at('research')
            evidence = self.evidence(self.root, extraction, subject)
            self.at('evidence')
            plan = self.parent(subject, extraction, evidence, profile)
            self.at('parent')
            handoffs = []
            for index, row in enumerate(plan['chapters']):
                handoffs.append({'film': plan['film'], 'chapter': row, 'owned_topics': [],
                    'orientation_anchors': evidence['facts'], 'research_gaps': evidence.get('uncertainties', []),
                    'previous_chapter': plan['chapters'][index-1]['title'] if index else None,
                    'next_chapter': plan['chapters'][index+1]['title'] if index+1 < len(plan['chapters']) else None,
                    'research_subject': subject + ': ' + row['title'] + '. Scope: ' + row['scope']['description']})
        if self.args.narration_input and len(plan['chapters']) != 1:
            raise ValueError('--narration-input attaches one existing chapter; select --chapter-input and --chapter-id')
        continuity, records = [], []
        for number, (row, handoff) in enumerate(zip(plan['chapters'], handoffs), 1):
            record = {'id': row['id'], 'title': row['title'], 'position': number, 'total': len(handoffs), 'state': 'running'}
            records.append(record)
            self.save_status(chapters=records)
            self.store.event('image-animation:chapters', 'running', record)
            try:
                result = self.chapter(row, handoff, extraction, continuity)
                record.update(result)
                continuity.append(result['continuity'])
                write(self.root / 'continuity-digest.json', continuity)
            except Waiting as exc:
                record.update(state=exc.state, error=str(exc), details=exc.details)
            except ReachedStage as exc:
                record.update(state='stopped_after_' + str(exc))
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, httpx.HTTPError) as exc:
                record.update(state='failed', error=str(exc))
            # Frozen narration supplies continuity even if this chapter pauses at assets or rendering.
            chapter_dir = self.root / 'chapters' / row['id']
            if 'continuity' not in record and (chapter_dir / 'narration.json').exists():
                saved_script = read(chapter_dir / 'narration.json')
                saved_teaching = read(chapter_dir / 'teaching.json')
                continuity.append({'chapter_id': row['id'],
                    'covered_messages': [b['message'] for b in saved_teaching['teaching_sequence']],
                    'spoken_points': [point['narration'] for point in saved_script['points']]})
                write(self.root / 'continuity-digest.json', continuity)
            self.save_status(chapters=records)
            self.store.event('image-animation:chapters', record['state'], record)
        ready = [r for r in records if r['state'] == 'succeeded']
        if len(ready) != len(records):
            intentional = all(r['state'].startswith('stopped_after_') for r in records)
            self.save_status(state='stopped' if intentional else 'waiting', resume_command=self.resume_command())
            return 0 if intentional else 2
        final = []
        for orientation in self.orientations:
            videos = [Path(next(o['video'] for o in r['delivery']['outputs'] if o['orientation'] == orientation)) for r in ready]
            final.append({'orientation': orientation, **join_videos(videos, self.root / 'delivery' / orientation / 'film.mp4')})
        write(self.root / 'delivery.json', {'schema_version': CONTRACT, 'outputs': final, 'chapters': records})
        self.save_status(state='succeeded', outputs=final)
        return 0

    def resume_command(self):
        import shlex
        return shlex.join([sys.executable, str(ROOT / 'scripts/create-image-animation.py'),
                           '--output', str(self.root), '--resume'])


def load_narration_value(bundle):
    # Validate the same verified bundle contract without relaxing hashes or word spans.
    from .animation_experiment import finite
    if bundle.get('schema_version') != 'infofilm.animation-narration.v1' or digest(bundle['transcript']) != bundle['transcript_sha256']:
        raise ValueError('Invalid narration bundle identity')
    for key in ('audio', 'timing'):
        if digest(Path(bundle[key]).read_bytes()) != bundle[key + '_sha256']:
            raise ValueError('Narration bundle ' + key + ' hash changed')
    timing = read(bundle['timing'])
    expected = [{k: w[k] for k in ('id', 'word', 'start', 'end')} for w in timing['words']]
    if timing['audio_sha256'] != bundle['audio_sha256'] or expected != bundle['words']:
        raise ValueError('MFA belongs to another audio or word map')
    if [w['word'] for w in expected] != bundle['transcript'].split():
        raise ValueError('MFA does not cover the exact script')
    prior, seen = 0, set()
    if not finite(bundle['duration_seconds']) or bundle['duration_seconds'] <= 0:
        raise ValueError('Invalid narration duration')
    for word in expected:
        if word['id'] in seen or not finite(word['start']) or not finite(word['end']) or not prior <= word['start'] <= word['end'] <= bundle['duration_seconds']+.001:
            raise ValueError('MFA words overlap or escape narration')
        seen.add(word['id']); prior = word['end']
    return True


def validation_errors(validator, candidate):
    try:
        return validator(candidate)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        return ['Malformed JSON structure: ' + str(exc)]


def narration_windows(beats, spans, duration, fps):
    boundaries = [0]
    for left, right in zip(beats, beats[1:]):
        a, b = spans[left['id']][-1]['end'], spans[right['id']][0]['start']
        boundary = round((a+b)/2*fps)/fps
        if not a <= boundary <= b:
            # Small gaps have no convenient frame boundary; retain the exact source edge.
            boundary = (a+b)/2
        boundaries.append(boundary)
    boundaries.append(duration)
    return {b['id']: (boundaries[i], boundaries[i+1]) for i, b in enumerate(beats)}


def allocate_canvas_frames(timing, source_start, source_end, fps):
    """Allocate cumulative chapter frames; retain source MFA and record subframe presentation adjustment."""
    frame_start, frame_end = round(source_start*fps), round(source_end*fps)
    if frame_end <= frame_start:
        raise ValueError('A canvas must own at least one frame of the narration')
    target_duration = (frame_end-frame_start)/fps
    actual_duration = source_end-source_start
    scale = target_duration/actual_duration
    result = copy.deepcopy(timing)
    for scene in result['scenes']:
        scene['start'] *= scale; scene['end'] *= scale
        for action in scene.get('actions', []):
            action['start'] *= scale; action['end'] *= scale
    result['duration_seconds'] = target_duration
    result['frame_allocation'] = {'fps': fps, 'chapter_start_frame': frame_start,
        'chapter_end_frame': frame_end, 'frames': frame_end-frame_start,
        'source_mfa_start_seconds': source_start, 'source_mfa_end_seconds': source_end,
        'presentation_time_scale': scale, 'policy': 'cumulative chapter frame ownership; source audio/MFA unchanged'}
    return result


def write_subtitles(path, words):
    def stamp(seconds):
        milliseconds = round(seconds*1000)
        return f'{milliseconds//3600000:02}:{milliseconds//60000%60:02}:{milliseconds//1000%60:02},{milliseconds%1000:03}'
    entries = [f'{i+1}\n{stamp(c["start"])} --> {stamp(c["end"])}\n{c["text"]}\n'
               for i, c in enumerate(captions(words))]
    Path(path).write_text('\n'.join(entries))
    return Path(path)


def join_videos(videos, output, audio=None, subtitles=None, expected_frames=None, fps=None):
    """Final stitching is stream-copy video; speech/mix mux never rerenders the scene."""
    from fractions import Fraction

    videos = [Path(p) for p in videos]
    if not videos:
        raise ValueError('Joining requires at least one video')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def probe_file(path):
        return json.loads(subprocess.run(['ffprobe', '-v', 'error', '-count_frames', '-show_entries',
                          'format=duration:stream=codec_type,nb_read_frames,avg_frame_rate,duration',
                          '-of', 'json', str(path)], capture_output=True, text=True, check=True).stdout)

    input_probes = [probe_file(path) for path in videos]
    input_video_streams = []
    inherited_audio, inherited_subtitles = [], []
    for probe in input_probes:
        streams = probe.get('streams', [])
        visual = [s for s in streams if s.get('codec_type') == 'video']
        if len(visual) != 1:
            raise ValueError('Each joined input must have exactly one video stream')
        input_video_streams.append(visual[0])
        inherited_audio.append(any(s.get('codec_type') == 'audio' for s in streams))
        inherited_subtitles.append(any(s.get('codec_type') == 'subtitle' for s in streams))
    if audio is None and len(set(inherited_audio)) != 1:
        raise ValueError('Joined inputs have inconsistent inherited audio streams')
    if subtitles is None and len(set(inherited_subtitles)) != 1:
        raise ValueError('Joined inputs have inconsistent inherited subtitle streams')
    try:
        input_frames = sum(int(s['nb_read_frames']) for s in input_video_streams)
        rates = [float(Fraction(s['avg_frame_rate'])) for s in input_video_streams]
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise ValueError('Joined inputs need measurable frame counts and frame rates') from None
    clock_fps = rates[0] if fps is None else fps
    if type(clock_fps) not in (int, float) or not math.isfinite(clock_fps) or clock_fps <= 0:
        raise ValueError('Joined video FPS must be positive and finite')
    if any(not math.isfinite(rate) or abs(rate-clock_fps) > 1e-6 for rate in rates):
        raise ValueError('Joined inputs must share the requested frame rate')
    if expected_frames is None:
        expected_frames = input_frames
    if type(expected_frames) is not int or expected_frames <= 0 or input_frames != expected_frames:
        raise ValueError('Joined input frames differ from the complete narration clock')
    expect_audio = audio is not None or all(inherited_audio)
    expect_subtitles = subtitles is not None or all(inherited_subtitles)
    passthrough = len(videos) == 1 and audio is None and subtitles is None
    strategy = 'single_input_copy' if passthrough else 'ffmpeg_stream_copy'
    concat = output.parent / 'concat.json'
    write(concat, {'videos': [file_pin(p) for p in videos], 'audio': file_pin(audio), 'subtitles': file_pin(subtitles),
                   'expected_frames': expected_frames, 'fps': clock_fps,
                   'expected_audio': expect_audio, 'expected_subtitles': expect_subtitles, 'strategy': strategy})
    # ffmpeg concat escaping is distinct from shell quoting. Paths remain argument values.
    listing = output.parent / 'concat.txt'
    if any('\n' in str(p) or '\r' in str(p) for p in videos):
        raise ValueError('Video paths must not contain newline characters')
    listing.write_text(''.join("file '" + str(p.resolve()).replace("'", "'\\''") + "'\n" for p in videos))
    command = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'concat', '-safe', '0', '-i', str(listing)]
    if audio is not None:
        command += ['-i', str(audio)]
    if subtitles is not None:
        command += ['-i', str(subtitles)]
    command += ['-map', '0:v:0', '-c:v', 'copy']
    if expect_audio:
        command += ['-map', '1:a:0' if audio is not None else '0:a:0', '-c:a', 'aac' if audio is not None else 'copy']
        if audio is not None:
            command += ['-b:a', '192k']
    if expect_subtitles:
        subtitle_index = 2 if audio is not None else 1
        command += ['-map', str(subtitle_index) + ':s:0' if subtitles is not None else '0:s:0',
                    '-c:s', 'mov_text' if subtitles is not None else 'copy']
        if subtitles is not None:
            command += ['-metadata:s:s:0', 'language=eng']
    if passthrough:
        # A no-op remux through concat exposes AAC priming and shifts video/captions.
        # Preserve the already verified chapter container, including its edit lists.
        if output.resolve() != videos[0].resolve() and not (output.exists() and output.samefile(videos[0])):
            shutil.copy2(videos[0], output)
    else:
        subprocess.run(command + ['-movflags', '+faststart', str(output)], check=True)
    probe = probe_file(output)
    streams = probe.get('streams', [])
    video_streams = [s for s in streams if s.get('codec_type') == 'video']
    audio_present = any(s.get('codec_type') == 'audio' for s in streams)
    subtitles_present = any(s.get('codec_type') == 'subtitle' for s in streams)
    observations = {'audio_present': audio_present, 'subtitles_present': subtitles_present,
                    'expected_audio': expect_audio, 'expected_subtitles': expect_subtitles}
    checks = {'video_present': len(video_streams) == 1, 'audio_presence_matches_inputs': audio_present == expect_audio,
              'subtitle_presence_matches_inputs': subtitles_present == expect_subtitles}
    video_stream = video_streams[0] if len(video_streams) == 1 else {}
    try:
        checks['complete_narration_frames'] = int(video_stream['nb_read_frames']) == expected_frames
        checks['video_duration_matches_clock'] = abs(float(video_stream['duration'])-expected_frames/clock_fps) <= 1/clock_fps+.001
        checks['frame_rate_matches_clock'] = abs(float(Fraction(video_stream['avg_frame_rate']))-clock_fps) <= 1e-6
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        checks.update(complete_narration_frames=False, video_duration_matches_clock=False, frame_rate_matches_clock=False)
    if not all(checks.values()):
        write(output.parent / 'join-checks.json', {'passed': False, 'checks': checks, 'streams': observations, 'probe': probe})
        raise ValueError('Joined delivery failed frame/audio clock checks')
    write(output.parent / 'join-checks.json', {'passed': True, 'checks': checks, 'streams': observations, 'probe': probe})
    return {'video': str(output.resolve()), 'sha256': digest(output.read_bytes()),
            'duration_seconds': float(probe['format']['duration']), 'checks': checks, 'join_strategy': strategy, **observations}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group()
    source.add_argument('--subject')
    source.add_argument('--chapter-input', type=Path, help='Existing parent plan or {film,chapter,...} handoff JSON')
    p.add_argument('--chapter-id')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--mode', choices=['single', 'long'], default=None)
    p.add_argument('--duration-seconds', type=float, default=None)
    p.add_argument('--audience', default=None)
    p.add_argument('--orientation', choices=['landscape', 'portrait', 'both'], default=None)
    p.add_argument('--config', type=Path, default=ROOT / 'config.json')
    p.add_argument('--providers', type=Path, help='Routing registry; resume keeps its saved registry by default')
    p.add_argument('--llm', action='append', default=[], metavar='ACTIVITY=ROUTE')
    for activity in ACTIVITIES:
        p.add_argument('--' + activity + '-model', choices=ROUTES)
    p.add_argument('--sources', type=Path, help='Reuse saved {results:[{url,raw_content,source_digest?...}]} research')
    p.add_argument('--narration-input', type=Path, help='Reuse a verified existing speech/MFA bundle for one chapter')
    p.add_argument('--subject-graph', action=argparse.BooleanOptionalAction, default=None)
    p.add_argument('--review', choices=['on', 'off'], default=None)
    p.add_argument('--max-attempts', type=int, default=2)
    p.add_argument('--until', choices=STAGES, default='complete')
    p.add_argument('--generate-assets', action='store_true', help='Execute Qwen jobs; otherwise pause with cache-fill requests')
    p.add_argument('--music', choices=['none', 'acestep'], default=None)
    p.add_argument('--width', type=int, default=None, help='Landscape output width (portrait swaps width/height)')
    p.add_argument('--height', type=int, default=None)
    p.add_argument('--fps', type=int, default=None)
    p.add_argument('--canvas-width', type=int, default=None)
    p.add_argument('--canvas-height', type=int, default=None)
    p.add_argument('--manim-python', type=Path, default=ROOT / '.venv-manim-pilot/bin/python')
    p.add_argument('--dry-run', action='store_true', help='Print stages and resolved model routes; make no external calls')
    return p


def resolve_args(args, p):
    saved = read(args.output / 'inputs.json') if args.resume and (args.output / 'inputs.json').exists() else None
    defaults = {'mode': 'single', 'duration_seconds': 180, 'audience': 'Casual viewers and junior students',
        'orientation': 'landscape', 'subject_graph': True, 'review': 'on', 'music': 'none',
        'width': 1280, 'height': 720, 'fps': 30, 'canvas_width': 1536, 'canvas_height': 864}
    if saved:
        for field in ('subject', 'mode', 'duration_seconds', 'audience', 'orientation', 'subject_graph', 'review', 'music', 'chapter_id'):
            if getattr(args, field) is None:
                setattr(args, field, saved.get(field))
        for field, (key, index) in {'width': ('size', 0), 'height': ('size', 1),
                                  'canvas_width': ('canvas_size', 0), 'canvas_height': ('canvas_size', 1)}.items():
            if getattr(args, field) is None:
                setattr(args, field, saved[key][index])
        if args.fps is None:
            args.fps = saved['fps']
        for field in ('sources', 'chapter_input', 'narration_input'):
            if getattr(args, field) is None and saved['pins'].get(field):
                setattr(args, field, Path(saved['pins'][field]['path']))
    for field, default in defaults.items():
        if getattr(args, field) is None:
            setattr(args, field, default)
    if not args.subject and not args.chapter_input:
        p.error('Provide --subject or --chapter-input, or resume an existing run')
    if not 0 < args.duration_seconds < 7200 or not math.isfinite(args.duration_seconds):
        p.error('--duration-seconds must be positive and below 7200; it is advisory')
    if not 1 <= args.max_attempts <= 5:
        p.error('--max-attempts must be 1–5')
    for key in ('width', 'height', 'canvas_width', 'canvas_height', 'fps'):
        if getattr(args, key) <= 0:
            p.error(key + ' must be positive')
    if args.width % 2 or args.height % 2 or args.canvas_width % 32 or args.canvas_height % 32:
        p.error('Video dimensions must be even and Qwen canvas dimensions multiples of 32')
    routes = {}
    for item in args.llm:
        activity, sep, route = item.partition('=')
        if not sep or activity not in ACTIVITIES or route not in ROUTES:
            p.error('--llm needs a supported ACTIVITY=luna|local|local-reviewer')
        routes[activity] = route
    for activity in ACTIVITIES:
        if getattr(args, activity + '_model'):
            routes[activity] = getattr(args, activity + '_model')
    args.routes = routes
    return args


def main(argv=None):
    p = parser()
    args = resolve_args(p.parse_args(argv), p)
    if args.dry_run:
        config = configuration(args.config)
        class DryStore:
            root = args.output
        saved_registry = args.output / 'provider-registry.json'
        registry_path = args.providers or (saved_registry if args.resume and saved_registry.exists() else None)
        routes = ImageAnimationModels(config, DryStore(), registry=load_registry(registry_path, overrides=args.routes))
        print(json.dumps({'schema_version': CONTRACT, 'stages': STAGES, 'routes': routes.describe_routes(),
            'until': args.until, 'output': str(args.output.resolve()), 'generate_assets': args.generate_assets,
            'controllers': controller_capabilities(), 'note': 'No external calls or generated assets'}, indent=2))
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    # One process owns a run's sqlite/checkpoints; different projects can still be separate CLI processes.
    with (args.output / '.workflow.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            p.error('Another CLI process owns this output directory')
        workflow = Workflow(args)
        try:
            return workflow.run()
        except ReachedStage as exc:
            workflow.save_status(state='stopped', stopped_after=str(exc), resume_command=workflow.resume_command())
            return 0
        except Waiting as exc:
            workflow.save_status(state=exc.state, error=str(exc), details=exc.details, resume_command=workflow.resume_command())
            workflow.store.event('image-animation', exc.state, {'error': str(exc), **exc.details})
            return 2
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, httpx.HTTPError) as exc:
            workflow.save_status(state='failed', error=str(exc), resume_command=workflow.resume_command())
            workflow.store.event('image-animation', 'failed', {'error': str(exc)})
            return 1
        finally:
            workflow.store.db.close()


if __name__ == '__main__':
    sys.exit(main())
