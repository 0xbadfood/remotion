"""Pre-voice conditioning: hard tokens get a spoken form for the voice, audit and aligner; captions keep the text."""
import tempfile
import unittest
from unittest.mock import patch

from infofilm.core import Store
from infofilm.media import map_source_words
from infofilm.speech import apply, code_lexicon, lexicon, valid


class SpeechTests(unittest.TestCase):
    def test_code_rules(self):
        lx = code_lexicon([{"narration": "Under NTCA Phase IV rules in World War II, Louis XIV and Malcolm X; "
                                         "UNESCO, Type C, Category V."}])
        self.assertEqual(lx, {"NTCA": "N T C A", "IV": "Four", "II": "Two", "XIV": "the Fourteenth", "V": "Five"})

    def test_apply_keeps_punctuation_and_audit_keeps_letter_tokens(self):
        lx = {"NTCA": "N T C A", "IV": "Four", "St.": "Saint", "%": "percent", "km": "kilometres", "DISHA": "Disha"}
        text = "(NTCA) Phase IV, St. Martin's 40 % within 5 km of DISHA."
        self.assertEqual(apply(text, lx), "(N T C A) Phase Four, Saint Martin's 40 percent within 5 kilometres of Disha.")
        self.assertEqual(apply(text, lx, audit=True), "(NTCA) Phase IV, Saint Martin's 40 percent within 5 kilometres of DISHA.")

    def test_llm_entries_are_validated_and_cannot_override_numerals(self):
        text = "NTCA Phase IV and UNESCO sites, St. Mary's."
        self.assertEqual(valid({"UNESCO": "you nes co", "Ghost": "boo", "St.": "Saint 1", "IV": "Four"}, text),
                         {"UNESCO": "you nes co", "IV": "Four"})
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); self.addCleanup(store.db.close)
            answer = {"entries": [{"written": "UNESCO", "spoken": "UNESCO"}, {"written": "St.", "spoken": "Saint"},
                                  {"written": "IV", "spoken": "I V"}, {"written": "NTCA", "spoken": "N T C A"}]}
            with patch("infofilm.llm.Models.call", return_value=answer) as call:
                lx = lexicon({"planner_url": "p", "reviewer_url": "r"}, store, [{"narration": text}])
                again = lexicon({"planner_url": "p", "reviewer_url": "r"}, store, [{"narration": text}])
            self.assertEqual(lx, {"NTCA": "N T C A", "IV": "Four", "St.": "Saint"})       # code wins on IV
            self.assertEqual(again, lx)
            self.assertEqual(call.call_count, 1)                                            # cached by narration

    def test_captions_keep_written_words_when_speech_is_expanded(self):
        point = {"id": "P06", "narration": "NTCA Phase IV guidelines.", "speech": {"NTCA": "N T C A", "IV": "Four"}}
        tokenize = lambda v: [w for w in "".join(c.lower() if c.isalnum() else " " for c in v).split()]
        aligned = [{"id": f"m{i}", "word": w, "start": i * .2, "end": i * .2 + .1}
                   for i, w in enumerate(["n", "t", "c", "a", "phase", "four", "guidelines"])]
        words, _ = map_source_words([point], aligned, tokenize,
                                    lambda value, p, offset: tokenize(apply(value, p.get("speech"))))
        self.assertEqual([w["word"] for w in words], ["NTCA", "Phase", "IV", "guidelines."])
        self.assertEqual(words[0]["mfa_word_ids"], ["m0", "m1", "m2", "m3"])
