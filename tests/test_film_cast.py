import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from infofilm.core import Store, read, write
from infofilm.film_cast import (SHORT_CHAPTER, cast_errors, cast_short, curriculum_people, make_cast,
                                required_in_chapter)

CORPUS = "Colonel Claus von Stauffenberg carried the bomb. General Ludwig Beck led the plotters. Fromm hesitated."


class FilmCastTests(unittest.TestCase):
    def person(self, name, **extra):
        return {'name': name, 'birth_year': 1907, 'reference_kind': 'photograph', 'appearance_context': '1944, colonel',
                'chapters': ['C01'], 'show_in': ['C01'], **extra}

    def test_names_must_come_from_the_scripts_or_facts(self):
        self.assertEqual(cast_errors({'people': [self.person('Claus von Stauffenberg'), self.person('Ludwig Beck')]},
                                     CORPUS, {'C01'}, 12), [])
        errors = cast_errors({'people': [self.person('Hans von Beck')]}, CORPUS, {'C01'}, 12)
        self.assertIn("'Hans von Beck': not named", errors[0])

    def test_shape_birth_year_chapters_and_budget_are_checked(self):
        errors = cast_errors({'people': [self.person('Ludwig Beck', birth_year='1880', chapters=['C09'])]}, CORPUS, {'C01'}, 12)
        self.assertTrue(any('birth_year' in e for e in errors)); self.assertTrue(any('chapter ids' in e for e in errors))
        self.assertTrue(cast_errors({'people': [self.person('Ludwig Beck')] * 3}, CORPUS, {'C01'}, 2))

    def test_unknown_birth_year_is_null_not_a_deadlock(self):
        # Tiger film: the planner added living researchers, code demanded a birth year nobody knows, the planner
        # dropped them, the reviewer demanded them back (9 rounds).
        self.assertEqual(cast_errors({'people': [self.person('Ludwig Beck', birth_year=None)]}, CORPUS, {'C01'}, 12), [])
        from infofilm.film_cast import TASK
        self.assertIn('private individuals whose stories serve as examples', TASK)

    def test_show_in_names_chapters_where_the_person_is_seen(self):
        self.assertTrue(any('show_in' in e for e in cast_errors({'people': [self.person('Ludwig Beck', show_in=[])]}, CORPUS, {'C01'}, 12)))
        self.assertTrue(any('show_in' in e for e in cast_errors({'people': [self.person('Ludwig Beck', show_in=['C02'])]}, CORPUS, {'C01', 'C02'}, 12)))
        cast = [self.person('Ludwig Beck', chapters=['C01', 'C02'], show_in=['C02']),
                {'name': 'Old Cast', 'chapters': ['C03', 'C04']}]                   # made before show_in: first chapter
        self.assertEqual(required_in_chapter(cast, 'C02'), ['Ludwig Beck'])
        self.assertEqual(required_in_chapter(cast, 'C01'), [])
        self.assertEqual(required_in_chapter(cast, 'C03'), ['Old Cast'])

    def test_cast_reads_every_chapter_script_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = []
            for cid in ('C01', 'C02'):
                project = Path(directory) / cid
                write(project / 'film-120/script.json', {'points': [{'narration': f'{cid}: Stauffenberg acts.'}]})
                write(project / 'evidence-final.json', {'facts': [{'id': 'F01', 'claim': 'Claus von Stauffenberg was a colonel.'}]})
                rows.append({'chapter': {'id': cid, 'title': cid}, 'project': str(project), 'duration': 120})
            models = Mock(); models.loop.return_value = {'people': [self.person('Claus von Stauffenberg', chapters=['C01', 'C02'])]}
            people = make_cast(models, rows, 12)
            self.assertEqual(people[0]['name'], 'Claus von Stauffenberg')
            stage, task, context, validator = models.loop.call_args.args
            self.assertEqual(stage, 'film-cast'); self.assertEqual([c['id'] for c in context['chapters']], ['C01', 'C02'])
            self.assertTrue(validator({'people': [self.person('Hans von Beck')]}))


    def test_short_film_is_cast_as_one_chapter_and_everyone_cast_is_shown(self):
        # Salt March short (2026-10-15): batches named Gandhi two ways and nothing decided who must be seen.
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                write(Path(directory) / 'film-255/script.json', {'points': [{'narration': 'Mahatma Gandhi walked to Dandi.'}]})
                write(Path(directory) / 'evidence-final.json', {'facts': [{'id': 'F01', 'claim': 'Mahatma Gandhi led the Salt March.'}],
                      'curriculum': {'coverage': [{'id': 'K1', 'point': 'Person: Sarojini Naidu - poet and Congress leader'},
                                                  {'id': 'K2', 'point': 'Event: Salt March (1930) - ...'}]}})
                evidence = read(Path(directory) / 'evidence-final.json')
                film = {'requested_seconds': 255, 'script': {'subject': 'The Salt March'}}
                models = Mock(); models.loop.return_value = {'people': [self.person('Mahatma Gandhi', chapters=['F'], show_in=['F'])]}
                config = {'person_references': {'enabled': True}}
                with patch('infofilm.film_cast.research_cast', return_value={'k': {'status': 'ready'}}) as research:
                    people = cast_short(config, store, models, film, evidence)
                    self.assertEqual([p['name'] for p in people], ['Mahatma Gandhi'])
                    stage, task, context, validator = models.loop.call_args.args
                    self.assertEqual([c['id'] for c in context['chapters']], [SHORT_CHAPTER])
                    self.assertEqual(context['planned_people_by_chapter'], {SHORT_CHAPTER: ['Sarojini Naidu']})
                    self.assertEqual(required_in_chapter(people, SHORT_CHAPTER), ['Mahatma Gandhi'])
                    research.assert_called_once()
                    models.loop.reset_mock()
                    self.assertEqual(cast_short(config, store, models, film, evidence), people)   # the saved cast is reused
                    models.loop.assert_not_called()
            finally:
                store.db.close()

    def test_short_film_cast_never_stops_the_film(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                models = Mock(); models.loop.side_effect = RuntimeError('reviewer down')
                write(Path(directory) / 'film-90/script.json', {'points': [{'narration': 'x'}]})
                write(Path(directory) / 'evidence-final.json', {'facts': [{'id': 'F01', 'claim': 'x'}]})
                film = {'requested_seconds': 90, 'script': {}}
                self.assertIsNone(cast_short({'person_references': {'enabled': True}}, store, models, film, {}))
                self.assertIsNone(cast_short({'person_references': {'enabled': False}}, store, models, film, {}))
            finally:
                store.db.close()

    def test_curriculum_people_come_from_person_coverage_points(self):
        self.assertEqual(curriculum_people({'curriculum': {'record': {'coverage': [
            {'point': 'Person: Lord Irwin - Viceroy'}, {'point': 'Key term: Satyagraha - truth'}]}}}), ['Lord Irwin'])
        self.assertEqual(curriculum_people({}), [])


if __name__ == '__main__':
    unittest.main()
