"""Animation resumes preserve pinned speech configuration across mode changes."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infofilm.animation_workflow import resume_speech_configuration, run_once
from infofilm.core import digest, read, write
import test_animation_workflow as workflow_fixtures


def speech_config(mode=None):
    value = {
        'reviewer_url': 'http://local.invalid/v1', 'tts_url': 'http://voice.invalid',
        'reference_audio': '/references/kane.wav', 'reference_transcript': 'Exact reference text.',
        'browser': '/bin/unused',
        'animation_provider': {'model': 'gpt-6-luna', 'reviewer_model': 'gpt-6-luna',
                               'library': 'animation-library', 'max_attempts': 3},
    }
    if mode is not None:
        value['animation_provider']['execution_mode'] = mode
    return value


class AnimationResumeConfigTests(unittest.TestCase):
    def archive(self, root, mode=None):
        archived = speech_config(mode)
        write(root / 'speech-config.json', archived)
        return archived, {'speech_config_sha256': digest(archived)}

    def test_new_workflow_uses_candidate_without_requiring_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = speech_config('beats')
            result = resume_speech_configuration(Path(directory), candidate, None)
            self.assertEqual(result, candidate)
            result['animation_provider']['model'] = 'changed-return-value'
            self.assertEqual(candidate, speech_config('beats'))

    def test_absent_mode_can_resume_through_either_execution_mode(self):
        for mode in ('chapter', 'beats'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archived, previous = self.archive(root)
                candidate = speech_config(mode)
                result = resume_speech_configuration(root, candidate, previous)
                self.assertEqual(result, archived)
                self.assertEqual(digest(result), previous['speech_config_sha256'])
                self.assertNotIn('execution_mode', result['animation_provider'])

    def test_explicit_mode_change_preserves_original_downstream_pin(self):
        for original, current in (('chapter', 'beats'), ('beats', 'chapter')):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archived, previous = self.archive(root, original)
                result = resume_speech_configuration(root, speech_config(current), previous)
                self.assertEqual(result, archived)
                self.assertEqual(result['animation_provider']['execution_mode'], original)

    def test_original_exact_configuration_remains_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archived, previous = self.archive(root, 'beats')
            self.assertEqual(resume_speech_configuration(root, archived, previous), archived)

    def test_inputs_and_archived_config_are_not_mutated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archived, previous = self.archive(root, 'chapter')
            candidate = speech_config('beats')
            originals = copy.deepcopy((candidate, previous))
            original_bytes = (root / 'speech-config.json').read_bytes()
            result = resume_speech_configuration(root, candidate, previous)
            result['animation_provider']['library'] = 'mutated-return-value'
            self.assertEqual((candidate, previous), originals)
            self.assertEqual((root / 'speech-config.json').read_bytes(), original_bytes)
            self.assertEqual(read(root / 'speech-config.json'), archived)

    def test_missing_or_tampered_archive_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archived, previous = self.archive(root)
            (root / 'speech-config.json').unlink()
            with self.assertRaises(ValueError):
                resume_speech_configuration(root, speech_config('beats'), previous)
            tampered = copy.deepcopy(archived)
            tampered['reference_transcript'] = 'Tampered archived transcript.'
            write(root / 'speech-config.json', tampered)
            with self.assertRaises(ValueError):
                resume_speech_configuration(root, speech_config('beats'), previous)

    def test_meaningful_changes_remain_visible_to_the_frozen_input_guard(self):
        changes = {
            'voice_service': lambda c: c.update(tts_url='http://other-voice.invalid'),
            'local_review_endpoint': lambda c: c.update(reviewer_url='http://other-review.invalid'),
            'reference_audio': lambda c: c.update(reference_audio='/references/other.wav'),
            'reference_text': lambda c: c.update(reference_transcript='Different reference text.'),
            'model': lambda c: c['animation_provider'].update(model='another-model'),
            'reviewer_model': lambda c: c['animation_provider'].update(reviewer_model='another-reviewer'),
            'library': lambda c: c['animation_provider'].update(library='other-library'),
            'attempts': lambda c: c['animation_provider'].update(max_attempts=4),
            'browser': lambda c: c.update(browser='/bin/other'),
            'unrelated_mode_field': lambda c: c.update(execution_mode='beats'),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, previous = self.archive(root)
            for label, mutate in changes.items():
                with self.subTest(setting=label):
                    candidate = speech_config('beats')
                    mutate(candidate)
                    result = resume_speech_configuration(root, candidate, previous)
                    self.assertEqual(result, candidate)
                    self.assertNotEqual(digest(result), previous['speech_config_sha256'])

    def test_existing_workflow_reuses_accepted_phase_with_new_operational_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            fixtures = workflow_fixtures.WorkflowTests()
            args = fixtures.fixture(directory, 'teaching')
            original = read(args.config)
            original['animation_provider'] = speech_config()['animation_provider']
            write(args.config, original)
            model = fixtures.fake([workflow_fixtures.teaching()])
            with patch('infofilm.animation_workflow.WorkflowModels', return_value=model), \
                 patch('infofilm.animation_workflow.publish_report'):
                self.assertEqual(run_once(args), 0)
                frozen = (args.output / 'inputs.json').read_bytes()
                speech = (args.output / 'speech-config.json').read_bytes()
                teaching = (args.output / 'teaching-plan.json').read_bytes()
                candidate = copy.deepcopy(original)
                candidate['animation_provider']['execution_mode'] = 'beats'
                write(args.config, candidate)
                args.resume = True
                args.execution_mode = 'beats'
                self.assertEqual(run_once(args), 0)
            self.assertEqual(len(model.calls), 1)
            self.assertEqual(len(model.review_calls), 1)
            self.assertEqual((args.output / 'inputs.json').read_bytes(), frozen)
            self.assertEqual((args.output / 'speech-config.json').read_bytes(), speech)
            self.assertEqual((args.output / 'teaching-plan.json').read_bytes(), teaching)


if __name__ == '__main__':
    unittest.main()
