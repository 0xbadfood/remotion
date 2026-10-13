import unittest

from infofilm.backdrop import clean, prompt
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

    def test_clean_drops_label_sentences_and_quotes(self):
        self.assertEqual(clean('Mangroves at dawn. Add a banner with the date. "Royal" tigers swim.', 200),
                         "Mangroves at dawn. tigers swim")
