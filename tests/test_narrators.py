import unittest

from infofilm.narrators import select_reference


class NarratorTests(unittest.TestCase):
    def setUp(self):
        self.config = {"reference_audio": "/tmp/default.wav", "reference_transcript": "Default words.",
                       "tts_engine": "omnivoice", "narrators": {"kane": {
                           "label": "Kane", "reference_audio": "/tmp/kane.wav",
                           "reference_transcript": "Kane words."}}}

    def test_named_narrator_selects_matching_audio_and_transcript_without_changing_defaults(self):
        chosen = select_reference(self.config, narrator="kane")
        self.assertEqual(chosen["reference_audio"], "/tmp/kane.wav")
        self.assertEqual(chosen["reference_transcript"], "Kane words.")
        self.assertEqual(self.config["reference_transcript"], "Default words.")

    def test_custom_vibe_reference_never_inherits_another_narrators_transcript(self):
        chosen = select_reference({**self.config, "tts_engine": "vibevoice"}, reference_audio="/tmp/custom.wav")
        self.assertIsNone(chosen["reference_transcript"])

    def test_custom_omni_reference_requires_its_own_transcript(self):
        with self.assertRaisesRegex(ValueError, "exact transcript"):
            select_reference(self.config, reference_audio="/tmp/custom.wav")
        chosen = select_reference(self.config, reference_audio="/tmp/custom.wav", reference_transcript=" Custom   words. ")
        self.assertEqual(chosen["reference_transcript"], "Custom words.")

    def test_unknown_or_ambiguous_selection_fails_before_any_render(self):
        with self.assertRaisesRegex(ValueError, "Unknown narrator"):
            select_reference(self.config, narrator="unknown")
        with self.assertRaisesRegex(ValueError, "Choose"):
            select_reference(self.config, narrator="kane", reference_audio="/tmp/custom.wav")
