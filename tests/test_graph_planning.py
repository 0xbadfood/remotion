import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from infofilm.core import ROOT, Store, digest, read, write
from infofilm.graph_planning import classify_subject, freeze_graph, make_handoff
from infofilm.longform import create_long, make_orientation
from infofilm.pipeline import research
from infofilm.llm import ModelOutputError
from infofilm.subject_taxonomy import model_classify


CLASSIFICATION = {'primary': 'history.era', 'secondary': [], 'lens': 'history',
                  'scope_extent': 'broad', 'ambiguity': 'clear', 'rationale': 'A historical era overview.',
                  'model_classification_accepted': True, 'needs_classification_review': False}


class GraphPlanningTests(unittest.TestCase):
    def test_research_guidance_is_separate_from_title_and_has_its_own_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            config = {'reviewer_url': 'reviewer', 'research_python': sys.executable, 'kbagent_root': directory}
            def fetched(command, **kwargs):
                request = read(command[-1]); target = Path(request['directory'])
                write(target / 'extracted.json', {'results': [{'url': 'https://example.org', 'raw_content': 'Evidence'}]})
                (target / 'answer.txt').write_text('Source-backed explanation')
            try:
                with patch('infofilm.pipeline.subprocess.run', side_effect=fetched) as worker:
                    research(config, store, 'Anatomy')
                    research(config, store, 'Anatomy', subject_guidance={'profile': 'structures'})
                    research(config, store, 'Anatomy', subject_guidance={'profile': 'structures'})
                    research(config, store, 'Anatomy')
                self.assertEqual(worker.call_count, 2)
                requests = [read(call.args[0][-1]) for call in worker.call_args_list]
                self.assertEqual([r['subject'] for r in requests], ['Anatomy', 'Anatomy'])
                self.assertNotIn('subject_guidance', requests[0])
                self.assertEqual(requests[1]['subject_guidance'], {'profile': 'structures'})
                self.assertNotEqual(requests[0]['directory'], requests[1]['directory'])
            finally:
                store.db.close()

    def test_frozen_graph_survives_catalog_changes_and_rejects_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = freeze_graph({}, root)
            with patch('infofilm.graph_planning.ROOT', root / 'missing'):
                self.assertEqual(freeze_graph({}, root), config)
            changed = copy.deepcopy(config)
            changed['subject_graph_snapshot']['nodes']['history']['label'] += ' changed'
            with self.assertRaisesRegex(ValueError, 'different frozen'):
                freeze_graph(changed, root)

    def test_selection_is_cached_and_uncertainty_uses_generic_planning(self):
        config = freeze_graph({'planner_url': 'planner', 'reviewer_url': 'reviewer'})
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                with patch('infofilm.graph_planning.model_classify', return_value=CLASSIFICATION) as classify:
                    seeds, guidance = classify_subject(config, store, Mock(), 'An era')
                    self.assertIsNotNone(guidance)
                    self.assertEqual(classify_subject(config, store, Mock(), 'An era'), (seeds, guidance))
                    self.assertEqual(classify.call_count, 1)
                self.assertNotIn('planner_prompt_shape', guidance['profile'])
                with self.assertRaisesRegex(ValueError, 'inputs changed'):
                    classify_subject(config, store, Mock(), 'Another era')
            finally:
                store.db.close()
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                uncertain = {**CLASSIFICATION, 'ambiguity': 'ambiguous', 'needs_classification_review': True}
                with patch('infofilm.graph_planning.model_classify', return_value=uncertain):
                    seeds, guidance = classify_subject(config, store, Mock(), 'Mercury')
                self.assertIsNone(guidance)
                self.assertIsNone(make_handoff(config, store, seeds, guidance, {}, 900, {}))
            finally:
                store.db.close()

    def test_malformed_classification_has_bounded_repair_and_fallback(self):
        config = freeze_graph({})
        models = Mock()
        models.call.side_effect = [ModelOutputError('truncated')] * 2
        with tempfile.TemporaryDirectory() as directory:
            result = model_classify('Unknown subject', config['subject_graph_snapshot'], models, Path(directory))
        self.assertEqual(models.call.call_count, 2)
        self.assertFalse(result['model_classification_accepted'])
        self.assertTrue(result['needs_classification_review'])

    def test_switch_guides_all_parent_stages_and_off_preserves_legacy_resume(self):
        plan = read(ROOT / 'pilots/longform/mughal-chapter-plan.json')
        brief = read(ROOT / 'pilots/longform/mughal-orientation.json')
        for enabled in [False, True]:
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                reference = root / 'ref.wav'; reference.write_bytes(b'reference')
                args = SimpleNamespace(config=write(root / 'config.json', {'reference_audio': str(reference),
                    'planner_url': 'planner', 'reviewer_url': 'reviewer'}),
                    project=root / 'parent', subject=plan['film']['subject'], durations=[900], orientation='landscape',
                    reference_audio=None, sources=None, orientation_brief=None, chapter_plan=None, until='plan',
                    subject_graph=enabled)
                extraction = {'results': [{'url': 'https://example.org', 'raw_content': 'Source evidence'}]}
                with patch('infofilm.graph_planning.model_classify', return_value=CLASSIFICATION) as classify, \
                     patch('infofilm.pipeline.research', return_value=extraction) as research, \
                     patch('infofilm.longform.make_orientation', return_value=brief) as orientation, \
                     patch('infofilm.longform.plan_film', return_value=(plan, {})) as planner:
                    create_long(args)
                    record = read(args.project / 'longform-project.json')
                    if not enabled:
                        # Old web requests lack the flag; they must still resume.
                        del args.subject_graph
                    create_long(args)
                    self.assertEqual(read(args.project / 'longform-project.json')['identity'], record['identity'])
                    self.assertEqual(planner.call_count, 1)
                    self.assertEqual(classify.call_count, int(enabled))
                    self.assertEqual('subject_guidance' in orientation.call_args.kwargs, enabled)
                    self.assertEqual('subject_guidance' in research.call_args.kwargs, enabled)
                    self.assertEqual('subject_handoff' in planner.call_args.kwargs, enabled)
                    if enabled:
                        self.assertEqual(research.call_args.args[2], args.subject)
                        self.assertTrue((args.project / 'subject-graph/planner-request.json').is_file())
                    else:
                        self.assertEqual(research.call_args.args[2], args.subject)
                        self.assertFalse((args.project / 'subject-graph').exists())
                        # Check the actual pre-switch fingerprint rather than just repeatability.
                        config = planner.call_args.args[0]; settings = planner.call_args.args[4]
                        old_identity = digest({'subject': args.subject, 'duration': 900, 'orientation': 'landscape',
                            'years': [record['start_year'], record['end_year']], 'settings': settings, 'config': config,
                            'supplied_inputs': {}, 'reference_sha': digest(reference.read_bytes()),
                            'controls': {k: getattr(args, k, None) for k in ['mood', 'mood_intensity', 'visual_balance',
                                'music_engine', 'tts_engine', 'omni_speed', 'reference_transcript', 'narrator', 'voice_id', 'language']}})
                        self.assertEqual(record['identity'], old_identity)
                    args.subject_graph = not enabled
                    with self.assertRaisesRegex(ValueError, 'different frozen settings'):
                        create_long(args)

    def test_orientation_uses_profile_questions_only_when_supplied(self):
        sources = {'results': [{'url': 'https://example.org', 'raw_content': 'Anatomy explains structures and their functions. ' * 20}]}
        models = Mock(); models.loop.return_value = {'anchors': []}
        settings = read(ROOT / 'external-planner.json')
        make_orientation(models, sources, 'Anatomy', settings)
        self.assertNotIn('subject_guidance', models.loop.call_args.args[2])
        make_orientation(models, sources, 'Anatomy', settings, subject_guidance={'profile': {'attributes': ['structures']}})
        self.assertIn('subject_guidance', models.loop.call_args.args[2])
        self.assertIn('not as evidence', models.loop.call_args.args[1])
