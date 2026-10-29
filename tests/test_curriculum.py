"""Curriculum films (History Studio, 2026-10-15): a textbook concept as a short film. Every coverage point must reach
the evidence and the narration; the textbook source is read whole."""
import unittest

from infofilm.curriculum import evidence_coverage_errors, evidence_task, script_coverage_errors
from infofilm.planning import evidence_context, script_errors

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

    def test_an_objective_no_source_teaches_may_be_declared_a_gap(self):
        # Work Done by a Constant Force (2026-10-30): an objective asked for "energy transfer", which the book never
        # says there; six honest briefs flagged it and each was refused. A declared gap carries it; book points cannot.
        curriculum = {**CURRICULUM, 'coverage': [{'id': 'K1', 'point': 'Objective: why salt'}, {'id': 'K2', 'point': 'Event: Dandi'},
                                                 {'id': 'K3', 'point': 'Objective: energy transfer'}]}
        evidence = {'curriculum': curriculum, 'facts': [{'id': 'F01', 'coverage': ['K1']}, {'id': 'F02', 'coverage': ['K2']}],
                    'source_gaps': [{'coverage': 'K3', 'why': 'the book gives work, not energy transfer'}]}
        self.assertEqual(evidence_coverage_errors(evidence, curriculum), [])
        script = {'points': [{'id': 'P01', 'fact_ids': ['F01']}, {'id': 'P02', 'fact_ids': ['F02']}]}
        self.assertEqual(script_coverage_errors(script, evidence), [], 'a gap is not narrated')
        book = {**evidence, 'source_gaps': [{'coverage': 'K2', 'why': 'no'}]}
        self.assertIn("K2 is the book's own point", ' '.join(evidence_coverage_errors(book, curriculum)))
        both = {**evidence, 'facts': evidence['facts'][1:], 'source_gaps': [{'coverage': 'K1', 'why': 'a'}, {'coverage': 'K3', 'why': 'b'}]}
        self.assertIn('at most 1 of the 2 objectives', ' '.join(evidence_coverage_errors(both, curriculum)))
        self.assertIn('source_gaps', evidence_task(curriculum))

    def test_a_lesson_never_talks_about_its_sources(self):
        # Work-Energy Theorem (2026-10-30): "the sources do not say more about how this resistive force varies"
        evidence = {'curriculum': CURRICULUM, 'facts': [{'id': 'F01'}]}
        point = {'id': 'P01', 'heading': 'Resistive work', 'fact_ids': ['F01'],
                 'narration': 'The resistive work is minus 8.75 joules, and the sources do not say more about it.'}
        self.assertIn('narration speaks about its sources', ' '.join(script_errors({'points': [point]}, evidence)))
        point['narration'] = 'Light from the Sun is the source of energy for plants, as the book says.'
        self.assertNotIn('speaks about its sources', ' '.join(script_errors({'points': [point]}, evidence)))
        point['narration'] = 'Our sources place the fire at the Chinese Museum.'
        self.assertNotIn('speaks about its sources', ' '.join(script_errors({'points': [point]}, {'facts': [{'id': 'F01'}]})),
                         'a documentary may weigh its sources')

    def test_book_source_is_read_whole_and_task_names_the_concept(self):
        text = 'Unrelated opening words. ' * 400 + 'The end of the chapter.'
        context = evidence_context({'results': [{'url': 'https://ncert.nic.in/textbook/pdf/jess302.pdf', 'complete': True,
                                                 'raw_content': text}]}, 'Salt March')
        self.assertEqual(context['sources'][0]['passages'][0]['text'], text)
        self.assertIn('The Salt March (NCERT Class 10 History, Chapter 2 "Nationalism in India")', evidence_task(CURRICULUM))


if __name__ == '__main__':
    unittest.main()


class TopicFilmTests(unittest.TestCase):
    def test_a_topic_keeps_every_cited_source_and_speaks_to_its_audience(self):
        # 2026-10-25: a heart procedure for parents; the NCERT rule "other sources only fill gaps" would drop it all.
        from infofilm.curriculum import evidence_task, script_task
        record = {'kind': 'textbook_concept', 'topic': True, 'audience': 'Parents of a newborn',
                  'concept': 'Balloon aortic valvuloplasty', 'book': 'Beyond the book: heart', 'chapter_no': 1, 'chapter': 'x'}
        self.assertIn('Every source is a cited reference', evidence_task(record))
        self.assertNotIn('NCERT', evidence_task(record))
        self.assertIn('never promise an outcome', script_task(record))
        self.assertIn('Source S1 is the textbook', evidence_task({**record, 'topic': False}))
