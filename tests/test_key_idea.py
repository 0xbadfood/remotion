"""Key-idea films (user, 2026-10-20): after a concept film, one of its key ideas as a short simplified film.
Simplification first, no coverage demands; a learner check says whether the idea landed."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from infofilm import planning
from infofilm.curriculum import evidence_coverage_errors, evidence_task, is_key_idea, script_coverage_errors, script_task
from infofilm.core import read

IDEA = {'kind': 'key_idea', 'concept': "Bohr's Model of the Hydrogen Atom", 'audience': 'Class 11 students in India',
        'book': 'Class 11 Chemistry', 'chapter_no': 2, 'chapter': 'Structure of Atom', 'coverage': [],
        'idea': {'title': 'Why hydrogen gives sharp coloured lines',
                 'understanding': 'Electrons only have certain energies, so only certain jumps and colours exist.',
                 'questions': ['Why are there gaps between the lines?']}}


class Events:
    def __init__(self, root):
        self.root, self.events = Path(root), []

    def event(self, name, state, detail=None):
        self.events.append((name, state, detail))


class FakeModels:
    """Scripted learner and judge verdicts; every loop returns a new script version."""

    def __init__(self, root, verdicts):
        self.store, self.verdicts, self.loops, self.calls = Events(root), list(verdicts), [], []

    def loop(self, stage, task, context, validator, **kwargs):
        self.loops.append((stage, task, context))
        return {'title': 'Lines', 'points': [{'id': 'P01', 'heading': 'Ladder', 'fact_ids': ['F01'],
                                              'narration': f'Version {len(self.loops)}: a ladder of energies.'}]}

    def call(self, role, name, prompt, **kwargs):
        self.calls.append((role, name, prompt))
        if role == 'planner':
            return {'explanation': 'Electrons sit on rungs.', 'answers': [], 'lost_at': []}
        return self.verdicts.pop(0)


class KeyIdeaTests(unittest.TestCase):
    def test_prompts_put_simplification_first_and_ask_for_no_coverage(self):
        self.assertTrue(is_key_idea(IDEA))
        self.assertIn('Simplification comes before everything else', script_task(IDEA))
        self.assertIn('Why hydrogen gives sharp coloured lines', evidence_task(IDEA))
        self.assertIn('completeness is not wanted', evidence_task(IDEA))
        facts = {'facts': [{'id': 'F01', 'claim': 'Analogy: a ladder'}]}
        self.assertEqual(evidence_coverage_errors(facts, IDEA), [])
        self.assertEqual(script_coverage_errors({'points': []}, {**facts, 'curriculum': IDEA}), [])

    def test_a_script_is_rewritten_until_the_idea_lands(self):
        with tempfile.TemporaryDirectory() as root:
            models = FakeModels(root, [{'landed': False, 'score': 40, 'wrong_beliefs': ['electrons are planets'],
                                        'fixes': [{'point_id': 'P01', 'problem': 'x', 'suggestion': 'y'}]},
                                       {'landed': True, 'score': 85, 'wrong_beliefs': [], 'fixes': []}])
            evidence = {'title': 'Lines', 'facts': [{'id': 'F01'}], 'curriculum': IDEA}
            script = planning.make_script(models, evidence, 240, 120)
            self.assertIn('Version 2', script['points'][0]['narration'])
            stage, task, context = models.loops[1]
            self.assertEqual(stage, 'script-240-land1')
            self.assertEqual(context['learner_feedback']['wrong_beliefs'], ['electrons are planets'])
            self.assertIn('a short simplified explainer', models.loops[0][1])
            record = read(Path(root) / 'learner-check-240.json')
            self.assertTrue(record['landed'])
            self.assertEqual(len(record['rounds']), 2)
            # the learner sees only the narration, never the evidence or the idea's answer
            learner_prompt = models.calls[0][2]
            self.assertNotIn('Electrons only have certain energies', learner_prompt)
            self.assertIn('Why are there gaps between the lines?', learner_prompt)

    def test_a_wrong_belief_means_not_landed_and_rounds_are_bounded(self):
        miss = {'landed': True, 'score': 80, 'wrong_beliefs': ['light is made in the nucleus'], 'fixes': []}
        with tempfile.TemporaryDirectory() as root:
            models = FakeModels(root, [miss] * 3)
            planning.make_script(models, {'title': 'L', 'facts': [{'id': 'F01'}], 'curriculum': IDEA}, 240, 120)
            self.assertEqual(len(models.loops), 1 + planning.LANDING_ROUNDS)
            self.assertFalse(read(Path(root) / 'learner-check-240.json')['landed'])

    def test_concept_films_get_no_learner_check(self):
        with tempfile.TemporaryDirectory() as root:
            models = FakeModels(root, [])
            curriculum = {**IDEA, 'kind': 'textbook_concept', 'coverage': []}
            planning.make_script(models, {'title': 'L', 'facts': [{'id': 'F01'}], 'curriculum': curriculum}, 240, 120)
            self.assertEqual((len(models.loops), models.calls), (1, []))


if __name__ == '__main__':
    unittest.main()
