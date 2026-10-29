import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from infofilm.backdrop import backdrop_context, clean, prompt
from infofilm.core import write
from infofilm.planning import RASTER_LABEL


class BackdropPromptTests(unittest.TestCase):
    def test_prompt_comes_from_the_lesson_and_drops_written_elements(self):
        context = {"chapter": {"objective": "Explain thrust as a reaction to exhaust pushed backward.",
                               "visual_direction": "Establishing illustration of a jet airliner at rest. Use native "
                                                   "overlays for the labels 'action' and 'reaction' and for the 1686 date."},
                   "film": {"shared_visual_style": "Clean cutaway illustrations; reconstructed scenes are identified as illustration."}}
        text = prompt(context)
        self.assertIn("thrust as a reaction", text)
        self.assertIn("jet airliner at rest", text)
        for gone in ("overlay", "label", "1686", "'action'", "identified as illustration"):
            self.assertNotIn(gone, text)
        self.assertIsNone(RASTER_LABEL.search(text))

    def test_equations_and_diagrams_stay_out_and_the_image_is_wordless(self):
        # 2026-10-29, Resistors in Parallel: the style asked for equations and tables, and the backdrop came back a
        # chalkboard of garbled writing ("Resistors in Parrald") behind every equation slide
        context = {"chapter": {"objective": "Resistors in Parallel"},
                   "film": {"shared_visual_style": "Clean editorial explanatory illustration on a dark field. Use native vector "
                                                   "graphics for all circuit diagrams (Figures 11.7). Show equations, derivation "
                                                   "steps and the Example 11.8 tab"}}
        text = prompt(context)
        self.assertIn("Clean editorial explanatory illustration", text)
        for gone in ("vector graphics", "derivation", "Example 11.8"):
            self.assertNotIn(gone, text)
        self.assertIn("A wordless image", text)
        self.assertIsNone(RASTER_LABEL.search(text))

    def test_clean_drops_label_sentences_and_quotes(self):
        self.assertEqual(clean('Mangroves at dawn. Add a banner with the date. "Royal" tigers swim.', 200),
                         "Mangroves at dawn. tigers swim")


class ShortFilmBackdropTests(unittest.TestCase):
    def test_short_film_backdrop_comes_from_subject_and_creative_brief(self):
        # Shorts had no chapter-context.json, so their flow and compare cards sat on the flat gradient (2026-10-15).
        with tempfile.TemporaryDirectory() as directory:
            store = SimpleNamespace(root=Path(directory))
            self.assertIsNone(backdrop_context(store))
            write(Path(directory) / "project.json", {"subject": "The Salt March (NCERT Class 10 History, Nationalism in India)"})
            write(Path(directory) / "creative-brief.json", {"visual_style": "Painterly editorial illustration, earth tones. "
                                                                            "Dark gradient overlays behind any text."})
            text = prompt(backdrop_context(store))
            self.assertIn("The Salt March", text); self.assertIn("Painterly editorial illustration", text)
            self.assertNotIn("NCERT", text); self.assertIsNone(RASTER_LABEL.search(text))
            write(Path(directory) / "chapter-context.json", {"chapter": {"objective": "Long-film chapter"}})
            self.assertEqual(backdrop_context(store)["chapter"]["objective"], "Long-film chapter")
