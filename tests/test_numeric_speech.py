import re
import sys
import unittest

from infofilm.audio_audit import canonical_numeric_text, point_coverage, score_english_transcript
from infofilm.core import configuration
from infofilm.media import map_source_words, mfa_numeric_text


class NumericSpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, configuration()["quiz_root"])

    def test_year_styles_follow_date_context(self):
        for notation, speech in [("1970", "nineteen seventy"), ("1900", "nineteen hundred"),
                                 ("1905", "nineteen oh five"), ("2000", "two thousand"),
                                 ("2005", "two thousand five"), ("2009", "two thousand nine"),
                                 ("2010", "twenty ten"), ("2026", "twenty twenty six")]:
            with self.subTest(year=notation):
                self.assertEqual(mfa_numeric_text(f"In {notation}, it opened."), f"In {speech}, it opened.")
        self.assertEqual(mfa_numeric_text("January 1, 1970"), "January one, nineteen seventy")
        # Month directly before the year, including day-first dates (Apollo 13 narration said 'one thousand ...').
        self.assertEqual(mfa_numeric_text("Apollo 13 launched on 11 April 1970 from Florida."),
                         "Apollo thirteen launched on eleven April nineteen seventy from Florida.")
        self.assertEqual(mfa_numeric_text("It was shipped in May 1967."), "It was shipped in May nineteen sixty seven.")
        self.assertEqual(mfa_numeric_text("By April, 1970 it was ready."), "By April, nineteen seventy it was ready.")
        # Years in free phrasing (Neptune Spear narration).
        for text, speech in [("A 2004 account says so.", "A two thousand four account says so."),
                             ("Aid was suspended in mid-2011.", "Aid was suspended in mid-twenty eleven."),
                             ("Letters from late 2010 and early 2011.", "Letters from late twenty ten and early twenty eleven."),
                             ("Its report gives 2005 as the point.", "Its report gives two thousand five as the point.")]:
            self.assertEqual(mfa_numeric_text(text), speech)
        self.assertEqual(mfa_numeric_text("It was -1970 degrees."), "It was minus one thousand, nine hundred and seventy degrees.")
        self.assertEqual(mfa_numeric_text("848 CE"), "eight hundred and forty-eight CE")

    def test_quantities_are_not_guessed_as_years(self):
        for text in ["In 1970 houses", "It cost ₹1970.", "It cost $1970.", "It cost 1970 rupees.",
                     "There were 1,970 soldiers.", "Profit was 1970 dollars.",
                     "It took 1970 years.", "From 1200 to 1500 calories.", "In 1970 km."]:
            with self.subTest(text=text):
                self.assertNotIn("nineteen seventy", mfa_numeric_text(text))
        self.assertEqual(mfa_numeric_text("Between 1970 and 1980."),
                         "Between nineteen seventy and nineteen eighty.")
        self.assertEqual(mfa_numeric_text("1970–1980"), "nineteen seventy to nineteen eighty")

    def test_audit_accepts_equivalent_date_styles_but_rejects_changed_year(self):
        for year, alternatives in [
                ("1970", ["nineteen seventy", "nineteen hundred seventy", "nineteen hundred and seventy",
                          "one thousand nine hundred and seventy"]),
                ("1905", ["nineteen oh five", "nineteen oh-five"]),
                ("2005", ["two thousand five", "two thousand and five"]),
                ("2026", ["twenty twenty-six", "twenty twenty six"])]:
            expected = f"The institution opened in {year}, changing the lives of its students."
            for words in alternatives:
                with self.subTest(year=year, words=words):
                    actual = expected.replace(year, words)
                    audit = score_english_transcript(expected_text=expected, transcript_text=actual,
                                                    audio_duration_seconds=8, speech_span_seconds=8)
                    self.assertEqual(audit["status"], "passed")
                    self.assertEqual(audit["score"], 1)
                    self.assertEqual(point_coverage([{"id": "P01", "narration": expected}], actual)[0]["coverage"], 1)
        bad = score_english_transcript(expected_text="It opened in 1970 and served the entire community.",
                                       transcript_text="It opened in nineteen seventy one and served the entire community.",
                                       audio_duration_seconds=8, speech_span_seconds=8)
        self.assertIn("numeric_mismatch", bad["reasons"])
        self.assertEqual(canonical_numeric_text("twenty one people"), "twenty one people")

    def test_audit_accepts_joined_designations_but_rejects_a_changed_number(self):
        # Manhattan C06: narration said 'P nine' correctly; every Whisper model wrote 'P9'.
        for expected, heard in [("It was one of three plants in the P-9 Project.", "It was one of three plants in the P9 project."),
                                ("They separated U-235 at Oak Ridge.", "They separated U235 at Oak Ridge."),
                                ("A B-29 bomber flew the mission.", "A B29 bomber flew the mission.")]:
            with self.subTest(expected=expected):
                audit = score_english_transcript(expected_text=mfa_numeric_text(expected), transcript_text=heard,
                                                 audio_duration_seconds=4, speech_span_seconds=3.8)
                self.assertNotIn("numeric_mismatch", audit["reasons"])
        changed = score_english_transcript(expected_text=mfa_numeric_text("They separated U-235 at Oak Ridge."),
                                           transcript_text="They separated U238 at Oak Ridge.",
                                           audio_duration_seconds=4, speech_span_seconds=3.8)
        self.assertIn("numeric_mismatch", changed["reasons"])
        self.assertEqual(canonical_numeric_text("the 21st of May in the 1940s"), canonical_numeric_text("the 21st of May in the 1940s"))

    def test_caption_mapping_preserves_year_and_quantity_in_same_point(self):
        text = "In 1970, 1970 soldiers arrived."
        tokenize = lambda value: re.findall(r"[a-z0-9]+", value.lower())
        expanded = tokenize(mfa_numeric_text(text))
        aligned = [{"id": str(i), "word": value, "start": i * .1, "end": (i + 1) * .1}
                   for i, value in enumerate(expanded)]
        source_tokens = lambda value, point, offset: tokenize(mfa_numeric_text(value, context=point["narration"], offset=offset))
        words, _ = map_source_words([{"id": "P01", "narration": text}], aligned, tokenize, source_tokens)
        self.assertEqual([word["word"] for word in words], text.split())
        self.assertEqual(len(words[1]["mfa_word_ids"]), 2)
        self.assertEqual(len(words[2]["mfa_word_ids"]), 6)


if __name__ == "__main__":
    unittest.main()
