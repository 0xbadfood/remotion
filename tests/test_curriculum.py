"""Curriculum films (History Studio, 2026-10-15): a textbook concept as a short film. Every coverage point must reach
the evidence and the narration; the textbook source is read whole."""
import unittest

from infofilm.curriculum import evidence_coverage_errors, evidence_task, script_coverage_errors
from infofilm.planning import evidence_context

CURRICULUM = {'concept': 'The Salt March', 'audience': 'Class 10 students', 'book': 'Class 10 History',
              'chapter_no': 2, 'chapter': 'Nationalism in India',
              'coverage': [{'id': 'K1', 'point': 'Objective: why salt'}, {'id': 'K2', 'point': 'Event: Dandi'}]}


class CurriculumTests(unittest.TestCase):
    def test_every_coverage_point_needs_a_fact(self):
        evidence = {'facts': [{'id': 'F01', 'coverage': ['K1']}, {'id': 'F02', 'coverage': ['K9']}]}
        errors = ' '.join(evidence_coverage_errors(evidence, CURRICULUM))
        self.assertIn('F02: "coverage" must list coverage ids', errors)
        self.assertIn('K2 (Event: Dandi)', errors)
        evidence['facts'][1]['coverage'] = ['K2']
        self.assertEqual(evidence_coverage_errors(evidence, CURRICULUM), [])

    def test_narration_must_reach_every_point(self):
        evidence = {'curriculum': CURRICULUM, 'facts': [{'id': 'F01', 'coverage': ['K1']}, {'id': 'F02', 'coverage': ['K2']}]}
        script = {'points': [{'id': 'P01', 'fact_ids': ['F01']}]}
        self.assertIn('K2 (Event: Dandi; facts F02)', ' '.join(script_coverage_errors(script, evidence)))
        script['points'].append({'id': 'P02', 'fact_ids': ['F02']})
        self.assertEqual(script_coverage_errors(script, evidence), [])
        self.assertEqual(script_coverage_errors(script, {'facts': []}), [])          # ordinary films: no check

    def test_book_source_is_read_whole_and_task_names_the_concept(self):
        text = 'Unrelated opening words. ' * 400 + 'The end of the chapter.'
        context = evidence_context({'results': [{'url': 'https://ncert.nic.in/textbook/pdf/jess302.pdf', 'complete': True,
                                                 'raw_content': text}]}, 'Salt March')
        self.assertEqual(context['sources'][0]['passages'][0]['text'], text)
        self.assertIn('The Salt March (NCERT Class 10 History, Chapter 2 "Nationalism in India")', evidence_task(CURRICULUM))


if __name__ == '__main__':
    unittest.main()
