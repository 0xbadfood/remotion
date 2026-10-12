import json
import tempfile
import unittest
from pathlib import Path

from infofilm.claims import write_claims
from infofilm.core import read, write


class ClaimsTests(unittest.TestCase):
    def test_every_point_lists_its_facts_sources_and_cited_passages(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'c01'
            write(project / 'film-120/script.json', {'points': [
                {'id': 'P01', 'narration': 'The plan was legal.', 'fact_ids': ['F01']},
                {'id': 'P02', 'narration': 'A bridge sentence.', 'fact_ids': []}]})
            write(project / 'evidence-final.json', {'facts': [{'id': 'F01', 'claim': 'Valkyrie was a legal plan.',
                'source_ids': ['S2', 'S9'], 'evidence_note': 'S1 P03 and S2 (P07, P08) describe it.'}]})
            write(project / 'sources.json', {'results': [
                {'url': 'https://a.example', 'raw_content': 'x'},
                {'url': 'https://b.example', 'title': 'B', 'raw_content': 'y', 'source_digest': {'passages': [
                    {'id': 'P07', 'text': 'Valkyrie was an emergency plan.'}, {'id': 'P09', 'text': 'Unrelated.'}]}}]})
            out = write_claims(Path(directory) / 'claims.json', 'Valkyrie',
                               [{'id': 'C01', 'title': 'Plan', 'project': project, 'duration': 120}], {'label': 'Haiku + Luna'})
            data = read(out)
            first, second = data['chapters'][0]['points']
            source = first['facts'][0]['sources'][0]
            self.assertEqual((source['url'], [p['id'] for p in source['passages']]), ('https://b.example', ['P07']))
            self.assertIsNone(first['facts'][0]['sources'][1]['url'])         # S9 is not in the archive
            self.assertTrue(second['unsourced']); self.assertFalse(first['unsourced'])
            self.assertEqual(data['summary'], {'points': 2, 'unsourced_points': 1, 'sources': 1})
            self.assertEqual(data['llm_configuration']['label'], 'Haiku + Luna')


if __name__ == '__main__':
    unittest.main()
