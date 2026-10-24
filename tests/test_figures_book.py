import tempfile
import unittest
from pathlib import Path

from infofilm import figures as F

FIG = '/home/nitin/manim/textbook-digest/keph1-02/figures/Fig._2.1.svg'


@unittest.skipUnless(Path(FIG).exists(), 'needs the digest index of Class 11 Physics')
class BookFigureTests(unittest.TestCase):
    """Physics and Maths films (2026-10-24): LaTeX equations and the book's own figures, nothing else."""
    def setUp(self):
        self.fig = {'subject': 'physics', 'book': True, 'code_root': '/home/nitin/manim',
                    'library': [{'id': 'Fig-2-1', 'name': 'Fig. 2.1', 'description': 'x-t graph', 'file': FIG, 'credit': 'NCERT Class 11 Physics, Fig. 2.1'}]}

    def test_only_equations_and_book_figures_and_latex_errors_come_back_while_planning(self):
        ok = {'id': 'S1', 'figure': {'type': 'equation', 'latex': r'v = \lim_{\Delta t \to 0} \frac{\Delta x}{\Delta t}'}}
        self.assertEqual(F.scene_errors(ok, self.fig), [])
        self.assertEqual(F.scene_errors({'id': 'S2', 'figure': {'type': 'library', 'id': 'Fig-2-1'}}, self.fig), [])
        self.assertIn('equation or library', F.scene_errors({'id': 'S3', 'figure': {'type': 'bohr', 'element': 'Na'}}, self.fig)[0])
        self.assertIn('does not compile', F.scene_errors({'id': 'S4', 'figure': {'type': 'equation', 'latex': r'\frac{dx}{dt'}}, self.fig)[0])
        self.assertIs(F.task_for(self.fig), F.BOOK_TASK)

    def test_a_book_figure_is_drawn_from_its_svg_with_its_credit(self):
        with tempfile.TemporaryDirectory() as folder:
            films = [{'storyboard': {'scenes': [{'id': 'S1', 'kind': 'figure', 'figure': {'type': 'library', 'id': 'Fig-2-1'}}]}}]
            F.render(type('Store', (), {'root': Path(folder)}), films, self.fig)
            scene = films[0]['storyboard']['scenes'][0]
            self.assertTrue(Path(scene['figure_file']).exists())
            self.assertEqual(scene['credit'], 'NCERT Class 11 Physics, Fig. 2.1')
