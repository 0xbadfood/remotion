import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from infofilm import figures
from infofilm.creative import beat_errors
from test_creative import example

LIBRARY_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' width='200' height='100'><rect x='10' y='10' width='180' "
               "height='80' fill='none' stroke='black'/></svg>")


def chemistry(root=None):
    return {'code_root': '/home/nitin/manim', 'library_root': str(root or '/nowhere'),
            'library': [{'id': 'crt-1', 'name': 'Cathode ray tube', 'description': 'A tube', 'labels': [],
                         'credit': 'Cathode ray tube.svg by Kd, CC BY 4.0, via Wikimedia Commons'}]}


def with_figure(spec):
    script, timing, storyboard, _ = example()
    storyboard['scenes'][1].update(kind='figure', figure=spec, image_brief='', copy_density='standard',
                                   heading='Nitrogen', summary='Three unpaired 2p electrons')
    return script, timing, storyboard


class FigureBeatTests(unittest.TestCase):
    def test_films_without_figure_options_cannot_use_them(self):
        script, timing, storyboard = with_figure({'type': 'orbital_boxes', 'element': 'N'})
        self.assertIn('S2: unsupported visual beat kind', beat_errors(storyboard, script, timing))

    def test_valid_figures_pass(self):
        for spec in ({'type': 'orbital_boxes', 'element': 'N'}, {'type': 'library', 'id': 'crt-1'},
                     {'type': 'molecule', 'smiles': 'O', 'formula': 'H2O'}):
            script, timing, storyboard = with_figure(spec)
            self.assertEqual(beat_errors(storyboard, script, timing, chemistry()), [], spec)

    def test_bad_figures_are_explained(self):
        for spec, words in (({'type': 'molecule', 'smiles': 'CCO', 'formula': 'H2O'}, 'fix the SMILES'),
                            ({'type': 'library', 'id': 'made-up'}, 'figure_options.library'),
                            (None, 'need a "figure" object')):
            script, timing, storyboard = with_figure(spec)
            self.assertTrue(any(words in e for e in beat_errors(storyboard, script, timing, chemistry())), spec)

    def test_options_come_from_the_curriculum(self):
        self.assertIsNone(figures.options({'curriculum': {'concept': 'Salt March'}}))
        self.assertEqual(figures.options({'curriculum': {'figures': chemistry()}})['library'][0]['id'], 'crt-1')
        self.assertNotIn('credit', figures.planner_options(chemistry())['library'][0])


class FigureRenderTests(unittest.TestCase):
    def test_figures_are_drawn_and_library_ones_credited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'crt-1.svg').write_text(LIBRARY_SVG)
            films = []
            for spec in ({'type': 'orbital_boxes', 'element': 'N'}, {'type': 'library', 'id': 'crt-1'}):
                _, _, storyboard = with_figure(spec)
                films.append({'storyboard': storyboard})
            figures.render(SimpleNamespace(root=root), films, chemistry(root))
            drawn, library = films[0]['storyboard']['scenes'][1], films[1]['storyboard']['scenes'][1]
            self.assertTrue(Path(drawn['figure_file']).exists())
            self.assertNotIn('credit', drawn)
            self.assertEqual(library['credit'], chemistry()['library'][0]['credit'])

    def test_a_bad_figure_fails_the_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, storyboard = with_figure({'type': 'equation', 'latex': r'\frac{1}{'})
            with self.assertRaises(ValueError):
                figures.render(SimpleNamespace(root=Path(directory)), [{'storyboard': storyboard}], chemistry())



class BiologyFigureTests(unittest.TestCase):
    """Biology Studio (2026-10-22): diagrams from the ~/biology library, at the film's class level."""
    HEART = {'type': 'biology', 'id': 'heart-section', 'highlight': 'part-left-ventricle',
             'labels': ['part-aorta', 'part-left-ventricle']}

    def options(self):
        return {'subject': 'biology', 'code_root': '/home/nitin/manim', 'level': 3,
                'library': [{'id': 'heart-section', 'name': 'Section of the human heart', 'parts': {}, 'stages': []}]}

    def test_biology_films_get_diagram_beats_checked_by_code(self):
        script, timing, storyboard = with_figure(self.HEART)
        self.assertEqual(beat_errors(storyboard, script, timing, self.options()), [])
        for spec, words in (({**self.HEART, 'labels': ['part-nope']}, 'not labelled parts'),
                            ({'type': 'molecule', 'smiles': 'O', 'formula': 'H2O'}, 'type biology or equation')):
            script, timing, storyboard = with_figure(spec)
            self.assertTrue(any(words in e for e in beat_errors(storyboard, script, timing, self.options())), spec)
        self.assertIn("type:'biology'", figures.task_for(self.options()))
        self.assertNotIn("type:'biology'", figures.task_for(chemistry()))

    def test_a_biology_diagram_is_drawn(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, storyboard = with_figure(self.HEART)
            figures.render(SimpleNamespace(root=Path(directory)), [{'storyboard': storyboard}], self.options())
            scene = storyboard['scenes'][1]
            self.assertTrue(Path(scene['figure_file']).exists())
            self.assertNotIn('credit', scene)                    # original art: no credit line
            layers = scene['figure_layers']                      # labels timed to the narration, the lit part pulsing
            self.assertTrue(Path(layers['base']['file']).exists() and Path(layers['highlight']['file']).exists())
            self.assertEqual([(l['text'], l['lit']) for l in layers['labels']], [('Aorta', False), ('Left ventricle', True)])

    def test_labels_appear_when_the_narration_names_them(self):
        from infofilm.video import figure_reveals
        words = [{'word': w, 'start': t} for w, t in (('The', 10.0), ('sino-atrial', 10.4), ('node', 10.9),
                                                     ('fires', 11.2), ('then', 12.0), ('the', 12.2),
                                                     ('atrio-ventricular', 12.5), ('node', 13.1))]
        labels = [{'text': 'Sino-atrial node'}, {'text': 'Atrio-ventricular node'}, {'text': 'Purkinje fibres'}]
        fps, start = 30, 300                                     # the scene starts at 10 s
        sa, av, pk = figure_reveals(labels, words, start, start + 150, fps)
        self.assertEqual((sa, av), (12 - 8, 75 - 8))            # each at its own name, not at the shared "node"
        self.assertEqual(pk, 10)                                 # never said: at the cut


if __name__ == '__main__':
    unittest.main()
