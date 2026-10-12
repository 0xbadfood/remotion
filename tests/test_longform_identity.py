"""Resume compatibility keeps execution mode operational and content frozen."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from infofilm.core import ROOT, configuration, digest, read, write
from infofilm.longform import create_long, planner_settings, project_identities, year_bounds
from infofilm.narrators import select_reference


def provider(mode=None):
    result = {
        'schema_version': 'infofilm.animation-provider.v1',
        'model': 'gpt-6-luna', 'reviewer_model': 'gpt-6-luna',
        'reasoning_effort': 'high', 'max_output_tokens': 24000,
        'max_attempts': 3, 'max_render_repairs': 2,
        'continuity_model': 'local', 'render_concurrency': 6,
        'library': 'animation-library',
    }
    if mode is not None:
        result['execution_mode'] = mode
    return result


def identity_inputs(mode=None):
    return {
        'subject': 'Circular motion', 'duration': 720, 'orientation': 'landscape',
        'years': [None, None], 'settings': {'planner_role': 'planner'},
        'config': {'planner_url': 'http://planner', 'reviewer_url': 'http://reviewer',
                   'animation_provider': provider(mode)},
        'supplied_inputs': {'sources': 'source-sha'}, 'reference_sha': 'reference-sha',
        'controls': {'mood': 'auto', 'tts_engine': 'vibevoice',
                     'voice_id': 'kane', 'reference_transcript': 'Reference speech'},
    }


def historical_identity(inputs, *, graph=False, animation_provider=None):
    """The pre-refactor hash order, independently from the compatibility helper."""
    result = digest(inputs)
    if graph:
        result = digest({'legacy_identity': result, 'subject_graph': True})
    if animation_provider is not None:
        result = digest({'legacy_identity': result, 'animation_chapters': True,
                         'provider': animation_provider})
    return result


class LongformIdentityTests(unittest.TestCase):
    def test_original_absent_mode_hash_remains_compatible(self):
        inputs = identity_inputs()
        canonical, compatible = project_identities(inputs, animation_provider=provider('beats'))
        old = historical_identity(inputs, animation_provider=provider())
        self.assertIn(old, compatible)
        self.assertIn(canonical, compatible)

    def test_explicit_modes_and_normalized_legacy_wrapper_share_identity(self):
        canonical_hashes = set()
        for raw_mode in (None, 'chapter', 'beats'):
            for effective_mode in (None, 'chapter', 'beats'):
                with self.subTest(raw_mode=raw_mode, effective_mode=effective_mode):
                    inputs = identity_inputs(raw_mode)
                    effective = provider(effective_mode)
                    canonical, compatible = project_identities(inputs, animation_provider=effective)
                    canonical_hashes.add(canonical)
                    self.assertIn(historical_identity(inputs, animation_provider=effective), compatible)
        self.assertEqual(len(canonical_hashes), 1)

    def test_graph_wrapping_order_and_legacy_modes_remain_compatible(self):
        inputs = identity_inputs('chapter')
        canonical, compatible = project_identities(inputs, subject_graph=True,
                                                   animation_provider=provider('beats'))
        self.assertIn(historical_identity(inputs, graph=True, animation_provider=provider('chapter')), compatible)
        stripped = identity_inputs()
        self.assertEqual(canonical, historical_identity(stripped, graph=True, animation_provider=provider()))
        no_graph, _ = project_identities(inputs, animation_provider=provider('beats'))
        self.assertNotEqual(canonical, no_graph)

    def test_identity_calculation_does_not_mutate_frozen_inputs(self):
        inputs = identity_inputs('chapter')
        effective = provider('beats')
        originals = copy.deepcopy((inputs, effective))
        project_identities(inputs, subject_graph=True, animation_provider=effective)
        self.assertEqual((inputs, effective), originals)

    def test_non_animation_identity_is_unchanged_including_nested_settings(self):
        inputs = identity_inputs('chapter')
        for graph in (False, True):
            with self.subTest(graph=graph):
                canonical, compatible = project_identities(inputs, subject_graph=graph)
                self.assertEqual(canonical, historical_identity(inputs, graph=graph))
                self.assertEqual(compatible, {canonical})

    def test_real_frozen_setting_changes_are_not_compatible(self):
        original = identity_inputs('chapter')
        frozen_hash = historical_identity(original, graph=True, animation_provider=provider('chapter'))
        changes = {
            'model': lambda i, p: p.update(model='another-model'),
            'reviewer_model': lambda i, p: p.update(reviewer_model='another-reviewer'),
            'library': lambda i, p: p.update(library='another-library'),
            'config_model': lambda i, p: i['config']['animation_provider'].update(model='another-model'),
            'config_reviewer': lambda i, p: i['config']['animation_provider'].update(reviewer_model='another-reviewer'),
            'config_library': lambda i, p: i['config']['animation_provider'].update(library='another-library'),
            'voice': lambda i, p: i['controls'].update(voice_id='another-speaker'),
            'reference_audio': lambda i, p: i.update(reference_sha='changed-reference-sha'),
            'reference_text': lambda i, p: i['controls'].update(reference_transcript='Different reference speech'),
            'subject': lambda i, p: i.update(subject='Gravity'),
            'mood': lambda i, p: i['controls'].update(mood='funny'),
            'duration': lambda i, p: i.update(duration=900),
            'orientation': lambda i, p: i.update(orientation='portrait'),
            'source': lambda i, p: i['supplied_inputs'].update(sources='changed-source-sha'),
        }
        for label, mutate in changes.items():
            with self.subTest(setting=label):
                inputs, effective = copy.deepcopy(original), provider('beats')
                mutate(inputs, effective)
                _, compatible = project_identities(inputs, subject_graph=True, animation_provider=effective)
                self.assertNotIn(frozen_hash, compatible)

    def test_resume_accepts_historical_identity_without_replanning_saved_outline(self):
        plan = read(ROOT / 'pilots/longform/mughal-chapter-plan.json')
        brief = read(ROOT / 'pilots/longform/mughal-orientation.json')
        for chapter in plan['chapters']:
            chapter['production'] = {'kind': 'illustrated', 'rationale': 'Explain this historical chapter',
                                     'animation_brief': None}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ref = root / 'reference.wav'
            ref.write_bytes(b'unchanged reference audio')
            snapshot = write(root / 'config.json', {'reference_audio': str(ref),
                             'animation_provider': provider('chapter')})
            args = SimpleNamespace(config=snapshot, project=root / 'parent', subject=plan['film']['subject'],
                durations=[900], orientation='landscape', reference_audio=None, sources=None,
                orientation_brief=write(root / 'brief.json', brief), chapter_plan=None, until='plan',
                animation_chapters=True)
            write(args.project / 'chapter-plan.json', plan)
            narrative = write(args.project / 'chapters/c01/animation/narration-script-plan.json',
                              {'points': [{'id': 'P01', 'text': 'Already approved narration.'}]})
            before = {p: p.read_bytes() for p in (snapshot, narrative, args.project / 'chapter-plan.json')}
            config = select_reference(configuration(snapshot), reference_audio=None)
            start, end = year_bounds(args.subject)
            inputs = {'subject': args.subject, 'duration': 900, 'orientation': args.orientation,
                'years': [start, end], 'settings': planner_settings(config), 'config': config,
                'supplied_inputs': {'orientation_brief': digest(args.orientation_brief.read_bytes())},
                'reference_sha': digest(ref.read_bytes()),
                'controls': {k: getattr(args, k, None) for k in
                    ['mood', 'mood_intensity', 'visual_balance', 'music_engine', 'tts_engine', 'omni_speed',
                     'reference_transcript', 'narrator', 'voice_id', 'language']}}
            old_hash = historical_identity(inputs, animation_provider=provider('chapter'))
            write(args.project / 'longform-project.json', {'identity': old_hash})
            with patch('infofilm.longform.plan_film') as planner:
                create_long(args)
                planner.assert_not_called()
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content, str(path))


if __name__ == '__main__':
    unittest.main()
