import re
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from infofilm.audio_audit import spoken_numeric_text
from infofilm.core import Store, digest, read, write
from infofilm.media import AlignmentError, align_audio, map_source_words, mfa_numeric_text


def tokenize(text):
    return re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))


def payload(words):
    return {"tiers": {"words": {"entries": [[i * .1, (i + 1) * .1, word] for i, word in enumerate(words)]}}}


class AlignmentRecoveryTests(unittest.TestCase):
    def setup_run(self, directory):
        store = Store(directory)
        audio = store.root / "narration.wav"
        with wave.open(str(audio), "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            output.writeframes(struct.pack("<h", 100) * 24000)
        script = {"points": [{"id": "P05", "narration": "The minimum is 22.5% protein."}]}
        module = SimpleNamespace(**{f"DEFAULT_{key}": Path("test") for key in [
            "MFA_BIN", "MAMBA_BIN", "MFA_ROOT", "ACOUSTIC_MODEL", "DICTIONARY_MODEL", "G2P_MODEL"]})
        module.tokenize = tokenize
        def run(*args, **kwargs):
            return payload(tokenize(args[4])), Path("test-output"), {"had_retry": False}
        module.run_mfa_alignment = Mock(side_effect=run)
        legacy = store.root / "alignment" / digest({"audio": digest(audio.read_bytes()), "text": script["points"][0]["narration"]})[:20]
        return store, audio, script, module, legacy

    def test_saved_decimal_failure_retries_for_both_engines_without_changing_audio(self):
        for engine in ["omnivoice", "vibevoice"]:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as directory:
                store, audio, script, module, legacy = self.setup_run(directory)
                bad = payload(["the", "minimum", "is", "<unk>", "<unk>", "protein"])
                write(legacy / "mfa-raw.json", bad)
                before = digest(audio.read_bytes())
                with patch("infofilm.media._mfa_adapter", return_value=(module, Path("adapter.py"))):
                    result = align_audio({"tts_engine": engine}, store, audio, script, "alignment")
                self.assertEqual(module.run_mfa_alignment.call_count, 1)
                self.assertIn("twenty-two point five percent", module.run_mfa_alignment.call_args.args[4])
                self.assertEqual(read(legacy / "mfa-raw.json"), bad)
                self.assertEqual(digest(audio.read_bytes()), before)
                words = result["words"]
                self.assertEqual([w["word"] for w in words], ["The", "minimum", "is", "22.5%", "protein."])
                self.assertEqual(words[3]["mfa_word_ids"], ["mfa0004", "mfa0005", "mfa0006", "mfa0007", "mfa0008"])
                self.assertAlmostEqual(words[3]["start"], .3)
                self.assertAlmostEqual(words[3]["end"], .8)
                self.assertTrue(result["provenance"]["had_retry"])
                self.assertEqual([a["status"] for a in result["provenance"]["alignment_attempts"]], ["failed", "passed"])
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events WHERE state='retrying'").fetchone()[0], 1)
                # A successful recovery is reusable without any MFA invocation.
                with patch("infofilm.media._mfa_adapter", side_effect=AssertionError("Reuse timing cache")):
                    self.assertEqual(align_audio({"tts_engine": engine}, store, audio, script, "resume"), result)
                store.db.close()

    def test_fresh_numeric_input_is_normalized_on_first_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            store, audio, script, module, _ = self.setup_run(directory)
            with patch("infofilm.media._mfa_adapter", return_value=(module, Path("adapter.py"))):
                result = align_audio({}, store, audio, script, "alignment")
            self.assertEqual(module.run_mfa_alignment.call_count, 1)
            self.assertEqual(result["provenance"]["orthographic_input"], "The minimum is twenty-two point five percent protein.")
            self.assertFalse(result["provenance"]["had_retry"])
            store.db.close()

    def test_decade_compound_recovers_saved_unknown_without_rebuilding_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            store, audio, script, module, _ = self.setup_run(directory)
            script["points"][0]["narration"] = "In the mid-1970s, a shadow moved."
            legacy = store.root / "alignment" / digest({"audio": digest(audio.read_bytes()),
                "text": script["points"][0]["narration"]})[:20]
            bad = payload(["in", "the", "mid", "<unk>", "a", "shadow", "moved"])
            write(legacy / "mfa-raw.json", bad)
            before = audio.read_bytes()
            with patch("infofilm.media._mfa_adapter", return_value=(module, Path("adapter.py"))):
                result = align_audio({}, store, audio, script, "alignment")
            self.assertEqual(module.run_mfa_alignment.call_count, 1)
            self.assertEqual(module.run_mfa_alignment.call_args.args[4], "In the mid nineteen seventies, a shadow moved.")
            self.assertEqual(result["words"][2]["word"], "mid-1970s,")
            self.assertEqual(result["words"][2]["mfa_word_ids"], ["mfa0003", "mfa0004", "mfa0005"])
            self.assertEqual(read(legacy / "mfa-raw.json"), bad)
            self.assertEqual(audio.read_bytes(), before)
            self.assertTrue(result["provenance"]["had_retry"])
            store.db.close()

    def test_decade_normalization_is_specific_and_leaves_asr_scoring_unchanged(self):
        for text, expected in [("late-1980s", "late nineteen eighties"),
                ("1990s", "nineteen nineties"), ("1900s", "nineteen hundreds"),
                ("2000s", "two thousands"), ("2010s", "twenty tens"), ("2020s", "twenty twenties")]:
            with self.subTest(text=text):
                self.assertEqual(mfa_numeric_text(text), expected)
        self.assertEqual(mfa_numeric_text("123s"), "123s")
        self.assertEqual(spoken_numeric_text("mid-1970s"), "mid-1970s")
        self.assertEqual(mfa_numeric_text("-22.5%"), spoken_numeric_text("-22.5%"))

    def test_transcript_validation_failure_retries_wider_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            store, audio, script, module, legacy = self.setup_run(directory)
            script["points"][0]["narration"] = "The protein matters."
            module.run_mfa_alignment.side_effect = lambda *args, **kwargs: (
                payload(["the", "<unk>", "matters"]), Path("test"), {})
            with patch("infofilm.media._mfa_adapter", return_value=(module, Path("adapter.py"))):
                with self.assertRaisesRegex(AlignmentError, "after 2 attempts"):
                    align_audio({}, store, audio, script, "alignment")
            self.assertEqual([c.kwargs["beam"] for c in module.run_mfa_alignment.call_args_list], [160, 320])
            self.assertFalse(list((store.root / "alignment").glob("*/timing.json")))
            self.assertEqual(len(list((store.root / "alignment").glob("*/mfa-raw.json"))), 2)
            store.db.close()

    def test_decoder_failure_also_retries_before_accepting_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            store, audio, script, module, _ = self.setup_run(directory)
            module.run_mfa_alignment.side_effect = [RuntimeError("decoder failed"),
                (payload(tokenize(spoken_numeric_text(script["points"][0]["narration"]))), Path("test"), {})]
            with patch("infofilm.media._mfa_adapter", return_value=(module, Path("adapter.py"))):
                result = align_audio({}, store, audio, script, "alignment")
            self.assertEqual(module.run_mfa_alignment.call_count, 2)
            self.assertTrue(result["provenance"]["had_retry"])
            store.db.close()

    def test_ratio_captions_use_expanded_mfa_spans_and_unknown_words_still_fail(self):
        points = [{"id": "P10", "narration": "A ratio of 1.2 to 1."}]
        lexemes = ["a", "ratio", "of", "one", "point", "two", "to", "one"]
        aligned = [{"id": str(i), "word": word, "start": i * .1, "end": (i + 1) * .1} for i, word in enumerate(lexemes)]
        normalizer = lambda value: tokenize(spoken_numeric_text(value))
        words, _ = map_source_words(points, aligned, normalizer)
        self.assertEqual([w["word"] for w in words], ["A", "ratio", "of", "1.2", "to", "1."])
        self.assertEqual(words[3]["mfa_word_ids"], ["3", "4", "5"])
        aligned[4]["word"] = "<unk>"
        with self.assertRaises(ValueError):
            map_source_words(points, aligned, normalizer)


if __name__ == "__main__":
    unittest.main()
