import re
import sys
import unittest
from pathlib import Path

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

    def test_subscript_zero_spoken_naught_matches_asr_digits(self):
        # Projectile film, 2026-10-26: 'v naught', 'theta naught' and 'two v naught' were said right; Whisper wrote
        # v0, theta0, 2v0 (and 'theta not'), and every take failed as a numeric mismatch.
        for expected, heard in [
                ("The total time of flight, T f, is two v naught sin theta naught divided by g.",
                 "The total time of flight, Tf is 2v0 sin theta0 divided by g."),
                ("The maximum height is h m equals v naught sin theta naught, squared, divided by two g.",
                 "The maximum height is hm equals v0 sin theta 0, squared, divided by 2g."),
                ("The range is v naught squared sin two theta naught divided by g.",
                 "The range is v0 squared sin 2 theta not divided by g.")]:
            with self.subTest(heard=heard):
                audit = score_english_transcript(expected_text=mfa_numeric_text(expected), transcript_text=heard,
                                                 audio_duration_seconds=6, speech_span_seconds=5.8)
                self.assertNotIn("numeric_mismatch", audit["reasons"])
        changed = score_english_transcript(expected_text=mfa_numeric_text("It is two v naught sin theta naught divided by g."),
                                           transcript_text="It is 3v0 sin theta0 divided by g.", audio_duration_seconds=4, speech_span_seconds=3.8)
        self.assertIn("numeric_mismatch", changed["reasons"])
        self.assertNotIn("1990 s", canonical_numeric_text("the 1990s"))          # a decade is not split

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


class TenPowerTests(unittest.TestCase):
    def test_scientific_notation_is_read_and_scored_one_way(self):
        from infofilm.lux_media import speakable
        self.assertEqual(speakable("about 4.6 × 10⁻¹⁵ metres"), "about 4.6 times 10 to the power minus 15 metres")
        self.assertIn("10 to the power 17", speakable("2.29 × 10¹⁷ kg"))
        script = canonical_numeric_text("about 4.6 × 10⁻¹⁵ metres")
        for heard in ("about 4.6 x 10-15 metres", "about 4.6 times 10 minus 15 metres",
                      "about 4.6 times 10 to the minus 15 metres", "about 4.6 times 10 to the power of minus 15 metres"):
            self.assertEqual(canonical_numeric_text(heard), script, heard)
        # the sign still counts, and a range is not a power
        self.assertNotEqual(canonical_numeric_text("about 4.6 times 10 to the 15 metres"), script)
        self.assertEqual(canonical_numeric_text("pages 10-15"), "pages ten to fifteen")

    def test_alignment_reads_a_power_of_ten_as_the_voice_did(self):
        # leph2-05 C3, 2026-11-04: '10¹⁴' reached int() as a year and failed both alignment attempts
        context = 'Nuclear matter is about 10¹⁴ times denser than water, as found in 1932.'
        self.assertEqual(mfa_numeric_text('10¹⁴', context=context, offset=context.index('10')), 'ten to the power fourteen')
        self.assertEqual(mfa_numeric_text('1932.', context=context, offset=context.index('1932')), 'nineteen thirty two.')
        self.assertIn('two point two nine times ten to the power minus fifteen', mfa_numeric_text('2.29 × 10⁻¹⁵ m'))
        # word by word, as the aligner maps the script, the words are those of the whole point
        import re as _re
        point_text = 'The 4.0 × 10⁻¹⁴ metre figure applies, and 2.29 × 10¹⁷ kg.'
        parts = [mfa_numeric_text(m.group(0), context=point_text, offset=m.start()) for m in _re.finditer(r'\S+', point_text)]
        self.assertEqual(' '.join(parts).split(), mfa_numeric_text(point_text).split())
        # wording saved with a recording made before still names its point
        from infofilm.media import align_audio
        from unittest.mock import patch
        point = {'id': 'P01', 'narration': 'Water is about 10³ kilograms per cubic metre.'}
        saved = mfa_numeric_text(point['narration'], ten_powers=False)
        self.assertNotEqual(saved, mfa_numeric_text(point['narration']))
        import tempfile, types
        def error(recorded):
            with tempfile.TemporaryDirectory() as root, patch('infofilm.media.digest', return_value='0' * 40):
                try:
                    align_audio({}, types.SimpleNamespace(root=Path(root)), __file__, {'points': [point]}, 'x',
                                spoken_points={'P01': recorded})
                except Exception as exc:
                    return str(exc)
        self.assertIn('does not match its source point', error('something else entirely'))
        self.assertNotIn('does not match its source point', str(error(saved)))

    def test_whisper_joining_a_power_of_ten_is_not_a_changed_number(self):
        # leph2-05 C3, 2026-11-04: "10³" heard as 103 and "2.29 × 10¹⁷" as "2.29 times 1017", in every take
        for script, heard in [("Water is about 10³ kilograms per cubic metre.", "Water is about 103 kilograms per cubic meter."),
                              ("A density of about 2.29 × 10¹⁷ kilograms.", "A density of about 2.29 times 1017 kilograms."),
                              ("About 4.6 × 10⁻¹⁵ metres.", "About 4.6 times 10-15 metres.")]:
            result = score_english_transcript(expected_text=script, transcript_text=heard, audio_duration_seconds=5,
                                              speech_span_seconds=5)
            self.assertNotIn('numeric_mismatch', result['reasons'], heard)
        # a number that is no power in the script stays a number
        result = score_english_transcript(expected_text="About 10³ kilograms.", transcript_text="About 1017 kilograms.",
                                          audio_duration_seconds=3, speech_span_seconds=3)
        self.assertIn('numeric_mismatch', result['reasons'])

    def test_whisper_splitting_a_long_decimal_at_a_pause_is_not_a_changed_number(self):
        # leph2-05 C1, 2026-11-04: "1.0078 u" heard as "1.007, 8u" and "99.985%" as "99.9, 85%", in every take
        script = "Masses of about 1.0078 u and 2.0141 u, with an abundance of 99.985 percent."
        heard = "Masses of about 1.007, 8u and 2.014, 1u, with an abundance of 99.9, 85 percent."
        result = score_english_transcript(expected_text=script, transcript_text=heard, audio_duration_seconds=6,
                                          speech_span_seconds=6)
        self.assertNotIn('numeric_mismatch', result['reasons'])
        # other digits are still a changed number
        result = score_english_transcript(expected_text=script, transcript_text=heard.replace("99.9, 85", "99.9, 58"),
                                          audio_duration_seconds=6, speech_span_seconds=6)
        self.assertIn('numeric_mismatch', result['reasons'])
