"""Exercise CLI stage handoffs, cache fill and resume without provider calls."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from infofilm.core import ROOT, digest, read, write
from infofilm.image_animation_models import DEFAULT_ROUTES
from infofilm.image_animation_repairs import CANVAS_REPAIR_SCHEMA
from infofilm.image_animation_workflow import (Workflow, Waiting, main, narration_windows,
    parser, resolve_args, validation_errors)


class FakeModels:
    calls = []
    chapters = 1
    unknown = False

    def __init__(self, config, store, registry=None):
        self.config, self.store, self.registry = config, store, registry
        self.fingerprint = digest(registry)

    def describe_routes(self):
        return self.registry['activities']

    def for_activity(self, activity):
        parent = self
        class Facade:
            fingerprint = activity + parent.fingerprint
            def call_json(self, role, name, prompt, **kwargs):
                parent.calls.append((activity, role, name, kwargs))
                if role == 'reviewer' or activity == 'review':
                    return {'pass': True, 'score': 95, 'issues': [], 'strengths': ['Grounded']}
                context = json.JSONDecoder().raw_decode(prompt.split('\nINPUT JSON:\n', 1)[1])[0]
                if activity == 'evidence':
                    return {'title': 'Circular motion', 'facts': [
                        {'id': 'F01', 'claim': 'The ball stays at a constant radius.',
                         'source_ids': ['S1'], 'evidence_note': 'The supplied circular path description.'}]}
                if activity == 'parent':
                    return {'schema_version': 'infofilm.image-animation-film.v1',
                        'film': {'subject': context['subject'], 'title': 'Circular motion', 'audience': context['audience']},
                        'chapters': [{'id': 'C0' + str(i+1), 'title': 'Follow the ball ' + str(i+1),
                            'objective': 'Understand constant radius.', 'advisory_seconds': 60,
                            'scope': {'description': 'Ball moving around the centre.', 'start_year': None, 'end_year': None},
                            'source_ids': ['S1'], 'research_queries': [], 'exclude': [], 'entry_context': 'Begin here.',
                            'exit_handoff': 'Conclude here.', 'production_kind': 'illustrated',
                            'visual_direction': 'Show a circle and ball.'} for i in range(parent.chapters)]}
                if activity == 'teaching':
                    requirement = context['requirement']
                    return {'schema_version': 'infofilm.animation-teaching.v2', 'requirement_id': requirement['id'],
                        'visual_message': requirement['visual_message'], 'explanation_strategy': 'Observe the path.',
                        'assumptions': [], 'teaching_sequence': [{'id': 'B01', 'message': 'The radius stays constant.',
                            'demonstration': 'Follow the circular path.', 'demonstration_mode': 'hold',
                            'required_behaviors': [], 'repeat_purpose': None}]}
                if activity == 'narration':
                    requirement = context['requirement']
                    return {'schema_version': 'infofilm.animation-narration-script.v1', 'requirement_id': requirement['id'],
                        'visual_message': requirement['visual_message'], 'title': 'Circular motion',
                        'points': [{'id': 'N01', 'beat_ids': ['B01'], 'narration': 'The radius stays constant.'}]}
                if activity == 'canvas':
                    return {'schema_version': 'infofilm.image-animation-canvases.v1', 'canvases': [
                        {'id': 'K01', 'beat_ids': ['B01'], 'visual_message': 'The radius stays constant.',
                         'controller_type': 'unknown_motion' if parent.unknown else 'static_canvas',
                         'prompt': 'A colourful circular path with a ball.', 'component_roles': []}]}
                if activity == 'motion':
                    return {'schema_version': 'infofilm.image-animation-cues.v1', 'beat_id': 'B01', 'actions': [
                        {'id': 'label', 'kind': 'label', 'start_word_id': 'w1', 'end_word_id': 'w4',
                         'label': 'Constant radius', 'purpose': 'Show the invariant.'}]}
                raise AssertionError(activity)
        return Facade()


class FakeAudioWorkflow(Workflow):
    def audio(self, directory, script):
        return self.checkpoint(directory, 'audio', {'script': script}, lambda: {
            'duration_seconds': 2, 'audio_sha256': 'test-audio', 'timing_sha256': 'test-timing',
            'words': [{'id': 'w'+str(i+1), 'word': word, 'start': i*.4+.1, 'end': i*.4+.35}
                      for i, word in enumerate(script['points'][0]['narration'].split())]})


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = write(self.root / 'sources.json', {'results': [
            {'url': 'https://institution.test/path', 'raw_content': 'A circle has constant radius from its centre. ' * 20}]})
        FakeModels.calls, FakeModels.chapters, FakeModels.unknown = [], 1, False

    def args(self, *extra):
        p = parser()
        return resolve_args(p.parse_args(['--subject', 'Circular motion', '--sources', str(self.sources),
            '--no-subject-graph', '--output', str(self.root / 'run'), '--canvas-width', '512',
            '--canvas-height', '512', *extra]), p)

    def workflow(self, *extra):
        w = FakeAudioWorkflow(self.args(*extra), models_factory=FakeModels)
        self.addCleanup(w.store.db.close)
        return w

    def fill_canvas(self, directory=None, colour=(35, 56, 89, 255)):
        directory = directory or self.root / 'run/chapters/C01/canvases/K01/canvas-assets'
        template = read(directory / 'asset-import-template.json')
        image = Image.new('RGBA', (512, 512), colour)
        ImageDraw.Draw(image).ellipse((100, 100, 400, 400), outline=(220, 240, 250, 255), width=6)
        image.save(directory / 'canvas.png')
        write(directory / 'asset-import.json', template)

    def test_default_request_mode_reaches_canvas_and_writes_cache_fill_template(self):
        w = self.workflow()
        with patch('infofilm.media.request', side_effect=AssertionError('No provider IO')):
            self.assertEqual(w.run(), 2)
        state = read(w.root / 'state.json')
        self.assertEqual(state['state'], 'waiting')
        self.assertEqual(state['chapters'][0]['state'], 'waiting_execution')
        template = w.root / 'chapters/C01/canvases/K01/canvas-assets/asset-import-template.json'
        self.assertEqual(read(template)['assets'][0]['id'], 'canvas')
        self.assertTrue((w.root / 'chapters/C01/audio.json').exists())
        self.assertTrue((w.root / 'continuity-digest.json').exists())

    def test_cache_fill_then_resume_preserves_accepted_plans_and_routes(self):
        w = self.workflow('--motion-model', 'local')
        self.assertEqual(w.run(), 2)
        before = list(FakeModels.calls)
        self.fill_canvas()
        p = parser()
        args = resolve_args(p.parse_args(['--output', str(w.root), '--resume', '--until', 'motion']), p)
        resumed = FakeAudioWorkflow(args, models_factory=FakeModels)
        self.addCleanup(resumed.store.db.close)
        with patch('infofilm.media.request', side_effect=AssertionError('Cached resume needs no HTTP')):
            self.assertEqual(resumed.run(), 0)
        self.assertEqual(resumed.registry['activities']['motion'], 'local')
        fresh = FakeModels.calls[len(before):]
        self.assertTrue(fresh)
        self.assertTrue(all(a == 'motion' for a, role, name, kwargs in fresh))
        timing = read(w.root / 'chapters/C01/canvases/K01/timing.json')
        self.assertEqual(timing['scenes'][0]['actions'][0]['label'], 'Constant radius')
        self.assertAlmostEqual(timing['duration_seconds'], 2)
        self.assertEqual(read(w.root / 'state.json')['state'], 'stopped')

    def test_pending_route_can_change_without_invalidating_accepted_stages(self):
        w = self.workflow()
        self.assertEqual(w.run(), 2)
        self.fill_canvas()
        resumed = self.workflow('--resume', '--until', 'motion', '--motion-model', 'local-reviewer')
        self.assertEqual(resumed.run(), 0)
        self.assertEqual(resumed.registry['activities']['motion'], 'local-reviewer')

    def test_canvas_prompt_repair_uses_new_assets_and_preserves_frozen_plan_and_old_image(self):
        with patch('infofilm.media.request', side_effect=AssertionError('No provider IO')), \
             patch('httpx.Client', side_effect=AssertionError('No live model or service calls')):
            workflow = self.workflow()
            self.assertEqual(workflow.run(), 2)
            self.fill_canvas()
            resumed = self.workflow('--resume', '--until', 'motion')
            self.assertEqual(resumed.run(), 0)
            chapter = workflow.root / 'chapters/C01'
            target = chapter / 'canvases/K01'
            old_assets = target / 'canvas-assets'
            old_manifest = read(old_assets / 'asset-manifest.json')
            original_canvas = read(chapter / 'canvas.json')['canvases'][0]
            preserved_paths = [chapter / 'canvas.json', chapter / 'checkpoints/canvas.json',
                old_assets / 'asset-requests.json', old_assets / 'asset-manifest.json',
                Path(old_manifest['assets']['canvas']['file'])]
            preserved = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in preserved_paths}
            model_calls = list(FakeModels.calls)
            document = {'schema_version': CANVAS_REPAIR_SCHEMA, 'canvas_id': 'K01',
                'prompt': 'One ball on a clearly separated circular path.',
                'reason': 'Keep the approved constant-radius comparison clear.'}
            write(target / 'canvas-prompt-import.json', document)
            new_assets = target / ('canvas-assets-manual-' + digest(document)[:12])

            repair_wait = self.workflow('--resume', '--until', 'motion')
            self.assertEqual(repair_wait.run(), 2)
            self.assertEqual(repair_wait.status['chapters'][0]['state'], 'waiting_execution')
            requests = read(new_assets / 'asset-requests.json')['requests']
            self.assertEqual(requests[0]['prompt'], document['prompt'])
            self.assertEqual(requests[0]['controller'], {'type': original_canvas['controller_type']})
            self.assertEqual(FakeModels.calls, model_calls)

            self.fill_canvas(new_assets, colour=(93, 65, 20, 255))
            repaired = self.workflow('--resume')
            with patch.object(repaired, 'render_canvas', side_effect=Waiting('needs_review', 'Captured scene before rendering')) as render:
                self.assertEqual(repaired.run(), 2)
            render.assert_called_once()
            scene = render.call_args.args[1]
            manifest = read(new_assets / 'asset-manifest.json')
            self.assertEqual(scene['assets']['background']['path'], manifest['assets']['canvas']['file'])
            self.assertNotEqual(manifest['assets']['canvas']['sha256'], old_manifest['assets']['canvas']['sha256'])
            self.assertEqual(scene['provenance'], {
                'canvas_prompt_repairs': [{'schema_version': CANVAS_REPAIR_SCHEMA, 'canvas_id': 'K01',
                    'reason': document['reason'], 'original_prompt': original_canvas['prompt'],
                    'replacement_prompt': document['prompt']}],
                'canvas_asset_manifest': str(new_assets / 'asset-manifest.json'),
                'visual_message': original_canvas['visual_message'],
                'limitations': original_canvas.get('limitations', [])})
            self.assertEqual(FakeModels.calls, model_calls)
            for path, snapshot in preserved.items():
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), snapshot, str(path))

    def test_changed_sources_block_resume_before_new_model_work(self):
        w = self.workflow()
        w.run()
        count = len(FakeModels.calls)
        write(self.sources, {'results': [{'url': 'https://changed.test', 'raw_content': 'Changed source.'}]})
        resumed = self.workflow('--resume')
        with self.assertRaisesRegex(ValueError, 'Frozen subject/input/settings changed'):
            resumed.run()
        self.assertEqual(len(FakeModels.calls), count)

    def test_waiting_chapter_does_not_prevent_next_and_continuity_contains_frozen_script(self):
        FakeModels.chapters = 2
        w = self.workflow('--mode', 'long')
        self.assertEqual(w.run(), 2)
        self.assertEqual([r['state'] for r in w.status['chapters']], ['waiting_execution', 'waiting_execution'])
        handoff = read(w.root / 'chapters/C02/chapter-handoff.json')
        self.assertEqual(handoff['continuity_digest'][0]['chapter_id'], 'C01')
        self.assertEqual(handoff['continuity_digest'][0]['spoken_points'], ['The radius stays constant.'])

    def test_unknown_controller_emits_request_before_any_image_job(self):
        FakeModels.unknown = True
        w = self.workflow()
        self.assertEqual(w.run(), 2)
        self.assertEqual(w.status['chapters'][0]['state'], 'waiting_controller')
        request = read(w.root / 'chapters/C01/canvases/K01/controller-request.json')
        self.assertEqual(request['canvas']['controller_type'], 'unknown_motion')

    def test_new_parent_plan_can_be_selected_as_existing_chapter_handoff(self):
        w = self.workflow()
        w.run()
        p = parser()
        args = resolve_args(p.parse_args(['--chapter-input', str(w.root / 'parent.json'), '--chapter-id', 'C01',
            '--sources', str(self.sources), '--no-subject-graph', '--output', str(self.root / 'selected')]), p)
        selected = FakeAudioWorkflow(args, models_factory=FakeModels)
        self.addCleanup(selected.store.db.close)
        self.assertEqual(selected.run(), 2)
        self.assertEqual(selected.status['chapters'][0]['id'], 'C01')

    def test_review_activity_is_not_reviewed_again(self):
        w = self.workflow()
        result = w.json_plan(w.root, 'review', 'direct-review', 'Assess the layers.', {}, lambda v: [])
        self.assertTrue(result['pass'])
        self.assertEqual(len(FakeModels.calls), 1)

    def test_nested_model_schema_errors_become_repair_feedback(self):
        self.assertIn('Malformed JSON structure', validation_errors(lambda v: v['x']['y'], {'x': None})[0])

    def test_dry_run_needs_no_keys_network_or_output_directory(self):
        target = self.root / 'dry'
        with patch('sys.stdout', new=io.StringIO()) as captured, patch('httpx.Client', side_effect=AssertionError('No network')):
            self.assertEqual(main(['--subject', 'Gravity', '--output', str(target), '--dry-run',
                                   '--teaching-model', 'local', '--review-model', 'luna']), 0)
        value = json.loads(captured.getvalue())
        self.assertEqual(value['routes']['teaching']['provider'], 'local')
        self.assertEqual(value['routes']['review']['provider'], 'luna')
        self.assertFalse(target.exists())

    def test_resume_adopts_saved_content_arguments(self):
        w = self.workflow('--duration-seconds', '240', '--orientation', 'both')
        w.run()
        p = parser()
        args = resolve_args(p.parse_args(['--output', str(w.root), '--resume']), p)
        self.assertEqual(args.subject, 'Circular motion')
        self.assertEqual(args.duration_seconds, 240)
        self.assertEqual(args.orientation, 'both')
        self.assertEqual(args.sources, self.sources)

    def test_mfa_windows_use_quiet_gaps_and_cover_whole_audio(self):
        beats = [{'id': 'a'}, {'id': 'b'}]
        words = {'a': [{'end': 1.15}], 'b': [{'start': 1.3}]}
        windows = narration_windows(beats, words, 2, 30)
        self.assertEqual(windows['a'][0], 0)
        self.assertEqual(windows['a'][1], windows['b'][0])
        self.assertTrue(1.15 <= windows['a'][1] <= 1.3)
        self.assertEqual(windows['b'][1], 2)


if __name__ == '__main__':
    unittest.main()
