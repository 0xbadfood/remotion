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


class PaintedWordsTests(unittest.TestCase):
    """2026-10-29: 18 of 70 backdrops carried painted titles ("Dimeinnal Aayiss Retiontions") though the prompt asked
    for a wordless image; a take OCR reads words in is drawn again with another seed, and a film whose every take has
    them keeps the plain gradient."""

    def store(self, directory):
        events = []
        return SimpleNamespace(root=Path(directory), event=lambda *a: events.append(a), events=events)

    def test_ocr_reads_painted_words(self):
        from PIL import Image, ImageDraw, ImageFont
        from infofilm.backdrop import painted_words
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b.png"
            image = Image.new("RGB", (900, 300), "black")
            ImageDraw.Draw(image).text((40, 100), "Merror Fumule Magnification", fill="white",
                                       font=ImageFont.load_default(size=60))
            image.save(path)
            words = painted_words(path)
            if words is None:
                self.skipTest("tesseract is not installed")
            self.assertGreater(len(words), 1)
            Image.new("RGB", (900, 300), (20, 30, 40)).save(path)
            self.assertEqual(painted_words(path), [])

    def test_faint_letters_on_a_textured_backdrop_are_read_and_texture_alone_is_not(self):
        """2026-10-29: "Unli·om Cirrcoual Mountrein", faint grey on a cloudy dark image, passed plain OCR."""
        import random
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
        from infofilm.backdrop import painted_words
        rng = random.Random(7)
        cloud = Image.new("L", (1536, 864))
        cloud.putdata([rng.randint(30, 140) for _ in range(1536 * 864)])
        cloud = cloud.filter(ImageFilter.GaussianBlur(3))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b.png"
            cloud.save(path)
            words = painted_words(path)
            if words is None:
                self.skipTest("tesseract is not installed")
            self.assertEqual(words, [])
            ImageDraw.Draw(cloud).text((420, 150), "Unliom Cirrcoual Mountrein", fill=120,
                                       font=ImageFont.load_default(size=44))
            cloud.save(path)
            self.assertGreater(len(painted_words(path)), 0)

    def test_a_large_light_title_on_a_plain_dark_backdrop_is_read(self):
        """2026-10-30: "Slalar Pnioct", crisp light grey on a dark gradient, passed every view of the gate."""
        from PIL import Image, ImageDraw, ImageFont
        from infofilm.backdrop import painted_words
        dark = Image.new("L", (1536, 864))
        dark.putdata([max(10, 70 - y // 14) for y in range(864) for _ in range(1536)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "b.png"
            dark.save(path)
            words = painted_words(path)
            if words is None:
                self.skipTest("tesseract is not installed")
            self.assertEqual(words, [])
            ImageDraw.Draw(dark).text((556, 320), "Slalar Pnioct", fill=200, font=ImageFont.load_default(size=72))
            dark.save(path)
            self.assertGreater(len(painted_words(path)), 0)

    def test_a_take_with_words_is_drawn_again_and_all_failing_keeps_the_gradient(self):
        from unittest import mock
        from infofilm import backdrop
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            write(Path(directory) / "backdrop" / "current.json", {"file": "old"})
            seeds = []

            def take(config, store, folder, params, label):
                folder.mkdir(parents=True, exist_ok=True)
                seeds.append(params["seed"])
                (folder / "candidate-0.png").write_bytes(params["seed"].encode())
                return folder / "candidate-0.png"
            reads = iter([["Merror", "Fumule"], []])
            with mock.patch("infofilm.image_recovery.render_take", take), \
                 mock.patch("infofilm.image_recovery.provider_session", mock.MagicMock()), \
                 mock.patch.object(backdrop, "painted_words", lambda path: next(reads)):
                value = backdrop.draw(store, {}, "qwen_local", "p", {"prompt": "p"}, 100, Path(directory) / "backdrop" / "x")
            self.assertEqual(seeds, ["100", "101"])
            self.assertEqual(value["painted_words"], [])
            self.assertTrue(value["file"].endswith("take-1/candidate-0.png"))
            with mock.patch("infofilm.image_recovery.render_take", take), \
                 mock.patch("infofilm.image_recovery.provider_session", mock.MagicMock()), \
                 mock.patch.object(backdrop, "painted_words", lambda path: ["Graph", "Analysic"]):
                self.assertIsNone(backdrop.draw(store, {}, "qwen_local", "p", {"prompt": "p"}, 200, Path(directory) / "backdrop" / "y"))
            self.assertFalse((Path(directory) / "backdrop" / "current.json").exists())
            self.assertTrue(list((Path(directory) / "backdrop").glob("current.json.old-*")))

    def test_numbers_stay_out_of_the_prompt(self):
        self.assertEqual(clean("A pendulum in a quiet hall. The 10 g steel and 100 g lead bob compared.", 200),
                         "A pendulum in a quiet hall")
