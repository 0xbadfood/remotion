import contextlib
import hashlib
import json
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from infofilm.core import Store, configuration, write
from infofilm.media import audit_audio
from infofilm.omni_media import render_omnivoice


def wav(path, sample=100, frames=80):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack("<h", sample) * frames)
    return path


class NumericAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, configuration()["quiz_root"])

    def score(self, expected, actual):
        from infofilm.audio_audit import score_english_transcript
        return score_english_transcript(expected_text=expected, transcript_text=actual,
                                        audio_duration_seconds=8, speech_span_seconds=8)

    def test_actual_failed_job_transcripts_pass_without_relaxing_thresholds(self):
        cases = [
            ("This transition begins around three weeks, with most puppies fully weaned by seven to ten weeks.",
             "This transition begins around 3 weeks, with most puppies fully weaned by 7-10 weeks."),
            ("Protein is equally critical, with guidelines recommending a minimum of twenty-two point five percent on a dry matter basis.",
             "Protein is equally critical, with guidelines recommending a minimum of 22.5% on a dry matter basis."),
            ("Standard recommendations may overestimate needs for smaller breeds, so aim for a calcium-to-phosphorus ratio near one point four to one.",
             "Standard recommendations may overestimate needs for smaller breeds, so aim for a calcium-to-phosphorus ratio near 1.4 to 1."),
        ]
        for expected, actual in cases:
            with self.subTest(actual=actual):
                result = self.score(expected, actual)
                self.assertEqual(result["status"], "passed")
                self.assertEqual(result["score"], 1)

    def test_changed_values_and_dropped_units_cannot_hide_in_wer_tolerance(self):
        expected = "Guidelines recommend twenty-two point five percent protein on a dry matter basis for healthy puppy growth."
        for actual in [
            "Guidelines recommend 23.5% protein on a dry matter basis for healthy puppy growth.",
            "Guidelines recommend 2.5% protein on a dry matter basis for healthy puppy growth.",
            "Guidelines recommend 22.5 protein on a dry matter basis for healthy puppy growth.",
        ]:
            with self.subTest(actual=actual):
                result = self.score(expected, actual)
                self.assertEqual(result["status"], "failed")
                self.assertIn("numeric_mismatch", result["reasons"])

    def test_equivalent_indefinite_quantities_pass_but_changed_values_fail(self):
        for notation, speech in [("1,000", "a thousand"), ("100", "a hundred"),
                                 ("1,000,000", "a million")]:
            with self.subTest(notation=notation):
                expected = f"The technique helped clear over {notation} cases during the investigation."
                equivalent = f"The technique helped clear over {speech} cases during the investigation."
                self.assertEqual(self.score(expected, equivalent)["status"], "passed")
                changed = "The technique helped clear over two thousand cases during the investigation."
                self.assertIn("numeric_mismatch", self.score(expected, changed)["reasons"])
        # Different coefficients must never become an indefinite singular.
        self.assertIn("numeric_mismatch", self.score("There were 200 cases.", "There were a hundred cases.")["reasons"])

    def test_actual_c06_transcript_preserves_every_claim_without_numeric_false_positive(self):
        expected = ("Since DeAngelo's arrest in 2018, IGG has helped clear over 1,000 cases, though the actual number "
                    "is likely higher due to unreported instances. The technique has become a powerful tool for cold cases.")
        observed = expected.replace("DeAngelo", "D'Angelo").replace("1,000", "a thousand")
        result = self.score(expected, observed)
        self.assertEqual(result["status"], "passed")
        self.assertNotIn("numeric_mismatch", result["reasons"])
        from infofilm.audio_audit import point_coverage, spoken_numeric_text
        self.assertGreater(point_coverage([{"id": "P09", "narration": expected}], observed)[0]["coverage"], .9)
        self.assertEqual(spoken_numeric_text("a thousand"), "a thousand")

    def test_new_omni_manifest_uses_the_same_numeric_wording_as_mfa(self):
        from infofilm.omni_media import _batch
        from infofilm.media import mfa_numeric_text
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            reference = wav(store.root / "ref.wav")
            narration = "In 1970, it reached 22.5%."
            client = Mock()
            client.post.return_value.json.return_value = {"id": "test"}
            client.get.return_value.json.return_value = {"status": "failed", "error": "Test terminal result"}
            with patch("infofilm.omni_media.httpx.Client") as http:
                http.return_value.__enter__.return_value = client
                with self.assertRaisesRegex(RuntimeError, "Test terminal result"):
                    _batch({}, store, "local", store.root, [{"id": "P01", "narration": narration}],
                           reference, "Exact reference words.", 0, "test")
            manifest = json.loads(client.post.call_args.kwargs["data"]["manifest"])
            self.assertEqual(manifest["tasks"][0]["text"], mfa_numeric_text(narration))
            self.assertNotIn("1970", manifest["tasks"][0]["text"])
            self.assertEqual(manifest["ref_text"], "Exact reference words.")
            store.db.close()

    def test_ranges_ratios_and_negative_values_preserve_meaning(self):
        self.assertEqual(self.score("Use a ratio of one point four to one.", "Use a ratio of 1.4:1.")["status"], "passed")
        self.assertEqual(self.score("It falls to minus five degrees.", "It falls to -5 degrees.")["status"], "passed")
        for actual in ["Weaning is usually at 7-11 weeks.", "Weaning is usually at 10-7 weeks."]:
            self.assertIn("numeric_mismatch", self.score("Weaning is usually at seven to ten weeks.", actual)["reasons"])

    def test_real_missing_narration_still_fails(self):
        result = self.score("The first point explains growth and the second explains why balanced nutrition protects the developing skeleton.",
                            "The first point explains growth.")
        self.assertEqual(result["status"], "failed")

        self.assertIn("low_coverage", result["reasons"])

    def test_missing_conclusion_cannot_hide_inside_good_whole_film_score(self):
        prefix = "The evidence connects the causes with their consequences. " * 30
        ending = ("Historians still debate the relative weight of natural causes versus colonial policy. "
                  "While drought was the trigger, administrative failures transformed a crop failure into a demographic collapse.")
        result = self.score(prefix+ending, prefix+"his story")
        self.assertGreater(result["score"], .875)
        self.assertEqual(result["status"], "failed")
        self.assertIn("incomplete_ending", result["reasons"])

    def test_missing_internal_sentence_is_not_diluted_by_long_narration(self):
        before = "The evidence connects the causes with their consequences. " * 20
        after = "The account continues with a carefully explained outcome. " * 20
        result = self.score(before+"Officials hoarded grain and failed to provide relief. "+after, before+after)
        self.assertGreater(result["score"], .875)
        self.assertIn("missing_passage", result["reasons"])

    def test_point_coverage_identifies_the_omitted_final_point(self):
        from infofilm.audio_audit import point_coverage
        points = [{"id": "P01", "narration": "The drought caused a disastrous failure of the rice crop."},
                  {"id": "P02", "narration": "Administrative failures made the crisis worse and prevented effective relief."}]
        coverage = point_coverage(points, points[0]["narration"])
        self.assertEqual([p["coverage"] for p in coverage], [1, 0])

    def test_minor_proper_name_asr_error_remains_tolerated(self):
        result = self.score("The social breakdown also increased piracy in the Hooghly delta.",
                            "The social breakdown also increased piracy in the Hoogley delta.")
        self.assertEqual(result["status"], "passed")

    def test_cached_raw_transcript_is_rescored_and_original_metrics_preserved(self):
        from quiz_harness.audio_audit import DEFAULT_WHISPER_MODEL
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            audio = wav(Path(directory) / "point.wav")
            key = hashlib.sha256(DEFAULT_WHISPER_MODEL.encode() + b"\0en" + audio.read_bytes()).hexdigest()
            write(store.root / "audio_audit_cache" / f"{key}.json", {
                "text": "Protein needs are at least 22.5% on a dry matter basis.",
                "audio_duration_seconds": 8, "speech_span_seconds": 8,
            })
            with patch("quiz_harness.audio_audit.WhisperAuditClient._start", side_effect=AssertionError("ASR cache must be reused")):
                result = audit_audio(configuration(), store, audio,
                                     "Protein needs are at least twenty-two point five percent on a dry matter basis.")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["verbatim_audit"]["status"], "failed")
            self.assertIn("22.5%", result["raw_transcript"])
            self.assertEqual(json.loads((audio.parent / "audit.json").read_text()), result)
            store.db.close()


class AuditEscalationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, configuration()["quiz_root"])

    def run_audit(self, directory, transcripts):
        from infofilm.audio_audit import audit_with_escalation, score_english_transcript
        store = Store(directory)
        audio = wav(store.root / "point.wav")
        expected = "His rise exploited a conflict between the Pandyas and Pallavas to establish Thanjavur as his capital."
        seen = []

        def recognize(auditor, path, text, points=None):
            seen.append(auditor.model)
            result = score_english_transcript(expected_text=text, transcript_text=transcripts[len(seen)-1],
                                             audio_duration_seconds=10, speech_span_seconds=10)
            return {**result, "model": auditor.model, "raw_transcript": transcripts[len(seen)-1]}

        try:
            with patch("infofilm.audio_audit.audit_english_audio", side_effect=recognize):
                result = audit_with_escalation({}, store, audio, expected)
            self.assertEqual(read_json(audio.parent / "audit.json"), result)
            return result, seen
        finally:
            store.db.close()

    def test_primary_pass_does_not_start_extra_recognizers(self):
        with tempfile.TemporaryDirectory() as directory:
            result, seen = self.run_audit(directory, ["His rise exploited a conflict between the Pandyas and Pallavas to establish Thanjavur as his capital."])
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(seen), 1)

    def test_crosscheck_pass_preserves_failed_primary_without_lowering_thresholds(self):
        with tempfile.TemporaryDirectory() as directory:
            result, seen = self.run_audit(directory, ["His rise exploited the conflict.",
                "His rise exploited a conflict between the Pandyas and Pallavas to establish Thanjavur as his capital."])
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(seen), 2)
            self.assertEqual(result["recognizer_attempts"][0]["status"], "failed")
            self.assertFalse(result["expected_prompt_supplied"])

    def test_missing_passage_still_fails_all_recognizers(self):
        with tempfile.TemporaryDirectory() as directory:
            result, seen = self.run_audit(directory, ["His rise exploited the conflict."]*3)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(seen), 3)
            self.assertIn("incomplete_ending", result["reasons"])
            self.assertEqual(seen[-1], "openai/whisper-large-v3-turbo")

    def test_fallback_error_cannot_approve_primary_failure(self):
        from infofilm.audio_audit import audit_with_escalation
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                failure = {"status": "failed", "score": .5, "reasons": ["text_mismatch"], "model": "base"}
                with patch("infofilm.audio_audit.audit_english_audio", side_effect=[failure, RuntimeError("offline model unavailable"), RuntimeError("offline model unavailable")]):
                    result = audit_with_escalation({}, store, wav(store.root / "point.wav"), "Expected narration.")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(result["recognizer_attempts"]), 3)
            finally:
                store.db.close()

    def test_whisper_pass_never_calls_llm(self):
        from infofilm.audio_audit import audit_with_escalation
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                with patch("infofilm.audio_audit.audit_english_audio", return_value={"status": "passed", "score": 1, "reasons": []}), \
                     patch("infofilm.llm.Models.call_json", side_effect=AssertionError("Passing audio needs no LLM")):
                    self.assertEqual(audit_with_escalation({"reviewer_url": "local"}, store, wav(store.root / "point.wav"), "Words.")["status"], "passed")
            finally:
                store.db.close()

    def test_llm_accepts_name_variant_keeps_original_failure_and_avoids_extra_asr(self):
        from infofilm.audio_audit import audit_with_escalation
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                failed = {"status": "failed", "score": .75, "reasons": ["text_mismatch"],
                          "expected_text": "the cholas ruled thanjavur", "transcript_text": "the cholas ruled tanjavur"}
                verdict = {"decision": "accept", "complete": True, "meaning_preserved": True,
                           "differences": [{"id": "D01", "kind": "non_english_name_variant", "explanation": "Same Tamil place name."}], "reason": "ASR spelling."}
                with patch("infofilm.audio_audit.audit_english_audio", side_effect=[failed]) as recognize, \
                     patch("infofilm.llm.Models.call_json", return_value=verdict):
                    result = audit_with_escalation({"reviewer_url": "local"}, store, wav(store.root / "point.wav"), "The Cholas ruled Thanjavur.")
                self.assertEqual(result["status"], "passed")
                self.assertEqual(result["score"], .75)
                self.assertEqual(result["asr_audit"]["status"], "failed")
                self.assertEqual(recognize.call_count, 1)
                self.assertEqual(result["acceptance_method"], "llm_transcript_adjudication")
            finally:
                store.db.close()

    def test_changed_numbers_bypass_llm_acceptance(self):
        from infofilm.audio_audit import adjudicate_transcript
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                with patch("infofilm.llm.Models.call_json", side_effect=AssertionError("Numeric guard cannot be waived")):
                    result = adjudicate_transcript({"reviewer_url": "local"}, store, wav(store.root / "point.wav"), "It happened in 848.", {"reasons": ["numeric_mismatch"]})
                self.assertIsNone(result)
            finally:
                store.db.close()

    def test_ignored_difference_or_changed_entity_cannot_receive_pass(self):
        from infofilm.audio_audit import adjudicate_transcript
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                audit = {"status": "failed", "score": .75, "reasons": ["text_mismatch"],
                         "expected_text": "they ruled thanjavur", "transcript_text": "they ruled delhi"}
                for differences in [[], [{"id": "D01", "kind": "meaning_change"}]]:
                    with patch("infofilm.llm.Models.call_json", return_value={"decision": "accept", "complete": True, "meaning_preserved": True, "differences": differences}):
                        result = adjudicate_transcript({"reviewer_url": "local"}, store, wav(store.root / "point.wav"), "They ruled Thanjavur.", audit)
                    self.assertEqual(result["status"], "failed")
            finally:
                store.db.close()

    def test_empty_transcription_skips_llm_and_extra_asr(self):
        from infofilm.audio_audit import audit_with_escalation
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                empty = {"status": "failed", "score": 0, "reasons": ["short_transcript"], "transcript_words": 0}
                with patch("infofilm.audio_audit.audit_english_audio", side_effect=[empty]) as recognize, \
                     patch("infofilm.llm.Models.call_json", side_effect=AssertionError("No text to adjudicate")):
                    result = audit_with_escalation({"reviewer_url": "local"}, store, wav(store.root / "point.wav"), "Expected speech.")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(recognize.call_count, 1)
            finally:
                store.db.close()

    def test_large_worker_module_launch_avoids_stdlib_queue_shadowing(self):
        from infofilm.audio_audit import _large_audit_client
        from quiz_harness.audio_audit import WhisperAuditClient
        with tempfile.TemporaryDirectory() as directory:
            client = _large_audit_client(WhisperAuditClient)(cache_root=Path(directory), model="openai/whisper-large-v3-turbo")
            with patch("subprocess.Popen") as launch:
                client._start()
            self.assertEqual(launch.call_args.args[0][1:3], ["-m", "infofilm.large_audio_audit_worker"])
            self.assertEqual(launch.call_args.kwargs["cwd"], Path(__file__).resolve().parents[1])


def read_json(path):
    return json.loads(path.read_text())


class OmniRetryTests(unittest.TestCase):
    def setup_run(self, directory):
        store = Store(directory)
        reference = wav(Path(directory) / "reference.wav")
        config = {"reference_audio": str(reference), "reference_transcript": "Exact reference words.", "omni_model": "model"}
        points = [{"id": "P01", "narration": "The first point is already good."},
                  {"id": "P02", "narration": "The second point needs another take."}]
        return store, config, points

    def batch(self, calls):
        def submit(config, store, base, root, tasks, reference, reference_text, attempt, name):
            calls.append((attempt, [p["id"] for p in tasks]))
            for p in tasks:
                wav(root / "points" / p["id"] / f"take-{attempt}" / "narration.wav", sample=100 + attempt)
            job_id = f"batch-{attempt}"
            write(root / f"provider-{attempt}.json", {"job_id": job_id,
                  "manifest": {"tasks": [{"id": p["id"], "text": p["narration"]} for p in tasks]}})
            return job_id
        return submit

    def audit(self, config, store, audio, text):
        passed = audio.parent.parent.name == "P01" or audio.parent.name == "take-4"
        return {"status": "passed" if passed else "failed", "score": 1 if passed else .5,
                "reasons": [] if passed else ["text_mismatch"]}

    def test_failed_point_can_pass_on_fifth_take_and_passed_point_is_never_regenerated(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory)
            calls = []
            with patch("infofilm.omni_media.omni_service", return_value=contextlib.nullcontext("local")) as service, \
                 patch("infofilm.omni_media._batch", side_effect=self.batch(calls)), \
                 patch("infofilm.omni_media.audit_audio", side_effect=self.audit):
                audio, info = render_omnivoice(config, store, points, "test")
            self.assertEqual(calls, [(0, ["P01", "P02"]), (1, ["P02"]), (2, ["P02"]), (3, ["P02"]), (4, ["P02"])])
            service.assert_called_once()
            self.assertEqual(info["point_attempts"], {"P01": 1, "P02": 5})
            self.assertEqual(info["speed"], .8)
            with wave.open(str(audio)) as joined:
                self.assertEqual(joined.readframes(joined.getnframes()), struct.pack("<h", 100) * 80 + struct.pack("<h", 104) * 80)
            store.db.close()

    def test_exhaustion_is_bounded_and_resume_reuses_clips_without_gpu_or_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory)
            calls = []
            failed = {"status": "failed", "score": .5, "reasons": ["text_mismatch"]}
            with patch("infofilm.omni_media.omni_service", return_value=contextlib.nullcontext("local")), \
                 patch("infofilm.omni_media._batch", side_effect=self.batch(calls)), \
                 patch("infofilm.omni_media.audit_audio", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "after 5 attempts: P01, P02"):
                    render_omnivoice(config, store, points, "test")
            self.assertEqual(len(calls), 5)
            root = next((store.root / "audio" / "omnivoice").iterdir())
            self.assertFalse((root / "narration.wav").exists())
            self.assertFalse((root / "result.json").exists())
            passed = {"status": "passed", "score": 1, "reasons": []}
            with patch("infofilm.omni_media.omni_service", side_effect=AssertionError("Saved clips need no GPU")), \
                 patch("infofilm.omni_media._batch", side_effect=AssertionError("Saved clips need no new job")), \
                 patch("infofilm.omni_media.audit_audio", return_value=passed):
                audio, info = render_omnivoice(config, store, points, "resume")
            self.assertTrue(audio.is_file())
            self.assertEqual(info["point_attempts"], {"P01": 1, "P02": 1})
            self.assertTrue(all((root / f"provider-{i}.json").is_file() for i in range(5)))
            store.db.close()


if __name__ == "__main__":
    unittest.main()
