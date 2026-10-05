import contextlib
import tempfile
import struct
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from infofilm.core import Store, digest, read, write
from infofilm.media import AlignmentError, audio_info
from infofilm.vibe_media import render_vibevoice, align_vibevoice, _batch, _check_clip


def wav(path, sample=1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack("<h", sample)*24000*4)
    return path


class VibePointTests(unittest.TestCase):
    def setup_run(self, directory, count=9):
        store = Store(directory)
        ref = wav(store.root/"reference.wav")
        config = {"reference_audio": str(ref), "tts_url": "local", "language": "en_indian"}
        points = [{"id": f"P{i:02d}", "narration": f"The subject has a clear explanation for point {i}."} for i in range(1, count+1)]
        return store, config, points

    def batch(self, calls):
        def generate(config, store, root, tasks, reference, attempt, index, name):
            calls.append((attempt, [p["id"] for p in tasks]))
            for p in tasks:
                clip = root/"points"/p["id"]/f"take-{attempt}"/"narration.wav"
                if not clip.exists(): wav(clip, sample=int(p["id"][1:])*1000+attempt)
            job = {"job_id": f"batch-{attempt}-{index}", "status": "success"}
            write(root/"batches"/f"attempt-{attempt}-batch-{index}"/"provider.json",
                  {"points": [{"point_id": p["id"]} for p in tasks], **job})
            return job
        return generate

    def check(self, config, project, point, clip, name):
        info = audio_info(clip)
        words = [{"id": f"w{i+1:04d}", "word": w, "start": .2+i*.2, "end": .4+i*.2,
                  "mfa_word_ids": [f"mfa{i+1:04d}"]} for i, w in enumerate(point["narration"].split())]
        path = write(clip.parent/"timing.json", {"audio_sha256": info["audio_sha256"],
            "text_sha256": digest(point["narration"]), "words": words})
        return {"audit": {"status": "passed", "score": 1, "reasons": []}, "timing": str(path)}

    def test_batches_are_bounded_and_assembly_preserves_order_exact_pauses_and_global_word_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory)
            calls = []
            with patch("infofilm.vibe_media.vibe_service", return_value=contextlib.nullcontext()), \
                 patch("infofilm.vibe_media._batch", side_effect=self.batch(calls)), \
                 patch("infofilm.vibe_media._check_clip", side_effect=self.check):
                audio, info = render_vibevoice(config, store, points, "test")
            self.assertEqual(calls, [(0, ["P01", "P02", "P03", "P04"]),
                                     (0, ["P05", "P06", "P07", "P08"]), (0, ["P09"])])
            pause = round(.18*24000)
            self.assertEqual(info["sample_count"], 9*96000+8*pause)
            self.assertEqual([c["offset_samples"] for c in info["point_clips"]], [i*(96000+pause) for i in range(9)])
            with wave.open(str(audio)) as output:
                pcm = output.readframes(output.getnframes())
            for i in range(9):
                start = i*(96000+pause)*2
                self.assertEqual(pcm[start:start+2], struct.pack("<h", (i+1)*1000))
            timing = align_vibevoice(config, store, audio, {"points": points}, info, "alignment")
            self.assertEqual([w["word"] for w in timing["words"]], " ".join(p["narration"] for p in points).split())
            self.assertEqual([w["id"] for w in timing["words"]], [f"w{i+1:04d}" for i in range(len(timing["words"]))])
            self.assertAlmostEqual(timing["points"][1]["start"], 4.38)
            self.assertLess(timing["points"][-1]["end"], info["duration_seconds"])
            bad = dict(info)
            bad["point_clips"] = [dict(c) for c in info["point_clips"]]
            bad["point_clips"][1]["offset_samples"] += 24000
            with self.assertRaisesRegex(ValueError, "offsets"):
                align_vibevoice(config, store, audio, {"points": points}, bad, "invalid")
            # A completed run rechecks cached clips without native GPU work.
            historical = wav(audio.parent/'narration.wav', sample=2500)
            historical_bytes = historical.read_bytes()
            with patch("infofilm.vibe_media.vibe_service", side_effect=AssertionError("No GPU")), \
                 patch("infofilm.vibe_media._batch", side_effect=AssertionError("No new batches")), \
                 patch("infofilm.vibe_media._check_clip", side_effect=self.check):
                cached, reused = render_vibevoice(config, store, points, "resume")
            self.assertEqual(cached, audio)
            self.assertEqual(reused["audio_sha256"], info["audio_sha256"])
            self.assertEqual(historical.read_bytes(), historical_bytes)
            store.db.close()

    def test_only_failed_point_retries_and_failure_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory, 3)
            calls = []
            def check(*args):
                if args[2]["id"] == "P02" and args[3].parent.name != "take-2":
                    return {"audit": {"status": "failed", "score": .5, "reasons": ["incomplete_ending"]}}
                return self.check(*args)
            with patch("infofilm.vibe_media.vibe_service", return_value=contextlib.nullcontext()), \
                 patch("infofilm.vibe_media._batch", side_effect=self.batch(calls)), \
                 patch("infofilm.vibe_media._check_clip", side_effect=check):
                _, info = render_vibevoice(config, store, points, "test")
            self.assertEqual(calls, [(0, ["P01", "P02", "P03"]), (1, ["P02"]), (2, ["P02"])])
            self.assertEqual(info["point_attempts"], {"P01": 1, "P02": 3, "P03": 1})
            store.db.close()

    def test_exhausted_points_cannot_publish_a_joined_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory, 1)
            calls = []
            with patch("infofilm.vibe_media.vibe_service", return_value=contextlib.nullcontext()), \
                 patch("infofilm.vibe_media._batch", side_effect=self.batch(calls)), \
                 patch("infofilm.vibe_media._check_clip", return_value={"audit": {"status": "failed", "score": .5, "reasons": ["incomplete_ending"]}}):
                with self.assertRaisesRegex(RuntimeError, "after 3 attempts: P01"):
                    render_vibevoice(config, store, points, "test")
            self.assertEqual(len(calls), 3)
            self.assertFalse(list((store.root/"audio/vibevoice").glob("*/result.json")))
            store.db.close()

    def test_exhausted_mfa_rebuilds_only_failed_chunk_then_accepts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory, 3)
            calls = []
            def check(*args):
                if args[2]["id"] == "P02" and args[3].parent.name == "take-0":
                    return {"audit": {"status": "passed", "score": 1, "reasons": []},
                            "alignment_error": "MFA alignment still needs review after 2 attempts"}
                return self.check(*args)
            with patch("infofilm.vibe_media.vibe_service", return_value=contextlib.nullcontext()), \
                 patch("infofilm.vibe_media._batch", side_effect=self.batch(calls)), \
                 patch("infofilm.vibe_media._check_clip", side_effect=check):
                _, info = render_vibevoice(config, store, points, "test")
            self.assertEqual(calls, [(0, ["P01", "P02", "P03"]), (1, ["P02"])])
            self.assertEqual(info["point_attempts"], {"P01": 1, "P02": 2, "P03": 1})
            events = store.db.execute("SELECT detail FROM events WHERE state='point_alignment_failed'").fetchall()
            self.assertEqual(len(events), 1)
            self.assertIn('"chunk_rebuild_pending": true', events[0][0])
            store.db.close()

    def test_mfa_failure_is_preserved_separately_from_passed_whisper_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory, 1)
            clip = wav(store.root / "point/narration.wav")
            audit = {"status": "passed", "score": 1, "reasons": []}
            with patch("infofilm.silence.cleanup", return_value=(clip, {})), \
                 patch("infofilm.vibe_media.audit_audio", return_value=audit), \
                 patch("infofilm.vibe_media.align_audio", side_effect=AlignmentError("MFA exhausted")):
                result = _check_clip(config, store.root, points[0], clip, "test")
            self.assertEqual(result["audit"], audit)
            self.assertEqual(result["alignment_error"], "MFA exhausted")
            self.assertEqual(read(clip.parent / "alignment-failure.json"), result)
            self.assertNotIn("timing", result)
            # An infrastructure error must surface, rather than trigger new speech.
            with patch("infofilm.silence.cleanup", return_value=(clip, {})), \
                 patch("infofilm.vibe_media.audit_audio", return_value=audit), \
                 patch("infofilm.vibe_media.align_audio", side_effect=PermissionError("MFA cannot start")):
                with self.assertRaises(PermissionError):
                    _check_clip(config, store.root, points[0], clip, "test")
            store.db.close()

    def test_mfa_exhaustion_also_obeys_chunk_attempt_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory, 1)
            calls = []
            with patch("infofilm.vibe_media.vibe_service", return_value=contextlib.nullcontext()), \
                 patch("infofilm.vibe_media._batch", side_effect=self.batch(calls)), \
                 patch("infofilm.vibe_media._check_clip", return_value={"audit": {"status": "passed", "score": 1},
                         "alignment_error": "MFA exhausted"}):
                with self.assertRaisesRegex(RuntimeError, "after 3 attempts: P01"):
                    render_vibevoice(config, store, points, "test")
            self.assertEqual(calls, [(0, ["P01"]), (1, ["P01"]), (2, ["P01"])])
            self.assertFalse(list((store.root / "audio/vibevoice").glob("*/result.json")))
            store.db.close()

    def test_partial_download_resumes_the_recorded_batch_without_losing_saved_clip(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory, 4)
            native = self.batch([])
            def interrupted(*args):
                native(*args)
                root = args[2]
                for p in points[1:]: (root/"points"/p["id"]/"take-0/narration.wav").unlink()
                raise TimeoutError("Interrupted download")
            with patch("infofilm.vibe_media.vibe_service", return_value=contextlib.nullcontext()), \
                 patch("infofilm.vibe_media._batch", side_effect=interrupted):
                with self.assertRaises(TimeoutError): render_vibevoice(config, store, points, "test")
            root = next((store.root/"audio/vibevoice").iterdir())
            preserved = (root/"points/P01/take-0/narration.wav").read_bytes()
            calls = []
            with patch("infofilm.vibe_media.vibe_service", return_value=contextlib.nullcontext()), \
                 patch("infofilm.vibe_media._batch", side_effect=self.batch(calls)), \
                 patch("infofilm.vibe_media._check_clip", side_effect=self.check):
                render_vibevoice(config, store, points, "resume")
            self.assertEqual(calls, [(0, ["P01", "P02", "P03", "P04"])])
            self.assertEqual((root/"points/P01/take-0/narration.wav").read_bytes(), preserved)
            store.db.close()

    def test_unknown_submission_is_never_blindly_resubmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory, 1)
            root = store.root/"audio/vibevoice/test"
            write(root/"batches/attempt-0-batch-0/provider.json", {"state": "submission_started", "job_id": "existing",
                "body": {}, "points": [{"point_id": "P01"}]})
            with patch("infofilm.vibe_media.request", side_effect=AssertionError("No new POST")), \
                 patch("infofilm.vibe_media.wait_job", side_effect=RuntimeError("Provider job needs reconciliation")):
                with self.assertRaisesRegex(RuntimeError, "reconciliation"):
                    _batch(config, store, root, points, Path(config["reference_audio"]), 0, 0, "test")
            store.db.close()

    def test_new_batch_sends_explicit_numbers_and_saved_result_survives_service_recycle(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config, points = self.setup_run(directory, 1)
            points[0]["narration"] = "In 1970, growth reached 22.5%. During the mid-1970s, it changed."
            root = store.root / "audio/vibevoice/test"
            def terminal(*args, **kwargs):
                return {"job_id": args[2].split("/")[-1], "status": "failed", "error": "Test terminal record"}
            with patch("infofilm.vibe_media.request", return_value={"voice_ref": "speaker:test"}), \
                 patch("infofilm.vibe_media.wait_job", side_effect=terminal):
                _batch(config, store, root, points, Path(config["reference_audio"]), 0, 0, "test")
            record = read(root / "batches/attempt-0-batch-0/provider.json")
            spoken = record["body"]["items"][0]["script"]
            self.assertIn("one thousand, nine hundred and seventy", spoken)
            self.assertIn("twenty-two point five percent", spoken)
            self.assertIn("mid nineteen seventies", spoken)
            self.assertNotIn("1970", spoken)
            self.assertIn("1970", points[0]["narration"])
            # Completed native metadata is durable even if the service forgot its jobs.
            with patch("infofilm.vibe_media.request", side_effect=AssertionError("No new submit")), \
                 patch("infofilm.vibe_media.wait_job", side_effect=AssertionError("Use saved terminal result")):
                self.assertEqual(_batch(config, store, root, points, Path(config["reference_audio"]), 0, 0, "resume")["status"], "failed")
            store.db.close()


if __name__ == "__main__":
    unittest.main()
