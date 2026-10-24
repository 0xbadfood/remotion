import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from infofilm.core import Store
from infofilm.lux_media import lux_settings, render_luxtts
from infofilm.media import mfa_numeric_text


def wav(path, frames=4800):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(struct.pack("<h", 200) * frames)
    return path


class LuxMediaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = Store(root / "project")
        (root / "lux" / "zipvoice").mkdir(parents=True)
        (root / "lux" / "zipvoice" / "luxvoice.py").write_text("")
        python = root / "python"
        python.write_text("")
        self.config = {"lux_python": str(python), "lux_root": str(root / "lux"),
                       "reference_audio": str(wav(root / "kane.wav")), "reference_transcript": "Exact  reference words."}
        self.points = [{"id": "P01", "narration": "In 1970, it reached 22.5%."}, {"id": "P02", "narration": "The heart has four chambers."}]
        self.runs = []

    def tearDown(self):
        self.store.db.close()
        self.directory.cleanup()

    def fake_worker(self, command, **kwargs):
        manifest = json.loads(Path(command[-1]).read_text())
        self.assertEqual(command[1], "-I")                                       # isolated: infofilm/queue.py must not shadow queue
        self.runs.append(manifest)
        for task in manifest["tasks"]:
            wav(Path(task["out"]))
        return type("Done", (), {"returncode": 0, "poll": lambda self: 0, "kill": lambda self: None})()

    def test_each_point_is_voiced_audited_and_failed_points_retaken_with_a_new_seed(self):
        def audit(config, store, clip, text, **kwargs):
            first_take_of_p02 = "P02" in str(clip) and "take-0" in str(clip)
            return {"status": "failed" if first_take_of_p02 else "passed", "score": .5, "reasons": ["low_coverage"]}
        with patch("infofilm.lux_media.subprocess.Popen", side_effect=self.fake_worker), \
             patch("infofilm.lux_media.audit_audio", side_effect=audit):
            final, info = render_luxtts(self.config, self.store, self.points, "narration-test")
        self.assertEqual([[t["id"] for t in run["tasks"]] for run in self.runs], [["P01", "P02"], ["P02"]])
        self.assertEqual([run["seed"] for run in self.runs], [0, 1])
        first = self.runs[0]
        self.assertEqual(first["speed"], .75)                                     # LuxTTS speeds up 1.3x inside
        self.assertEqual(first["reference_text"], "Exact reference words.")
        self.assertEqual(first["tasks"][0]["text"], mfa_numeric_text(self.points[0]["narration"]))
        self.assertEqual(info["provider"], "LuxTTS")
        self.assertEqual(info["point_attempts"], {"P01": 1, "P02": 2})
        # The studio shows progress from these: each clip as it is recorded, each point once it passes its check.
        states = [(state, json.loads(detail).get("point")) for state, detail in self.store.db.execute(
            "select state, detail from events where stage = 'narration-test' order by id")]
        self.assertEqual([p for s, p in states if s == "point_recorded"], ["P01", "P02", "P02"])
        self.assertEqual([p for s, p in states if s == "point_accepted"], ["P01", "P02"])
        self.assertEqual(info["sample_rate"], 48000)
        with wave.open(str(final)) as joined:
            self.assertEqual(joined.getnframes(), 2 * 4800)
        # A second call is served from the audited cache, with no worker run.
        with patch("infofilm.lux_media.subprocess.Popen", side_effect=AssertionError("no rerun")):
            again, _ = render_luxtts(self.config, self.store, self.points, "narration-test")
        self.assertEqual(again, final)

    def test_points_that_never_pass_stop_the_narration(self):
        failed = {"status": "failed", "score": .2, "reasons": ["low_coverage"]}
        with patch("infofilm.lux_media.subprocess.Popen", side_effect=self.fake_worker), \
             patch("infofilm.lux_media.audit_audio", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "still needs review after 2 attempts: P01, P02"):
                render_luxtts({**self.config, "lux_max_attempts": 2}, self.store, self.points, "narration-test")

    def test_settings_and_runtime_are_checked(self):
        with self.assertRaisesRegex(ValueError, "lux_speed"):
            lux_settings({"lux_speed": 2.5})
        with self.assertRaisesRegex(RuntimeError, "LuxTTS runtime is missing"):
            render_luxtts({**self.config, "lux_python": "/missing"}, self.store, self.points, "x")


if __name__ == "__main__":
    unittest.main()


class DefaultEngineTests(unittest.TestCase):
    def test_luxtts_is_the_default_and_yields_to_vibevoice_for_animation_chapters(self):
        from infofilm.web import FilmRequest
        self.assertEqual(FilmRequest(subject='Why volcanoes erupt', submission_id='submission-test-01').tts_engine, 'luxtts')
        animated = FilmRequest(subject='Why volcanoes erupt', submission_id='submission-test-01', generation_mode='long', durations=[600],
                               animation_chapters=True)
        self.assertEqual(animated.tts_engine, 'vibevoice')
        with self.assertRaisesRegex(ValueError, 'VibeVoice'):
            FilmRequest(subject='Why volcanoes erupt', submission_id='submission-test-01', generation_mode='long', durations=[600],
                        animation_chapters=True, tts_engine='luxtts')
        self.assertEqual(lux_settings({})['speed'], .75)


class RemoteFallbackTests(unittest.TestCase):
    def test_unreachable_media_factory_falls_back_to_local(self):
        from infofilm.lux_media import _run_remote
        from infofilm.media import remote_mms_align
        with tempfile.TemporaryDirectory() as folder:
            store, reference = Store(Path(folder)), Path(folder) / "ref.wav"
            reference.write_bytes(b"RIFF")
            config = {"lux_url": "http://127.0.0.1:9", "aligner_url": "http://127.0.0.1:9"}
            manifest = {"model": "m", "speed": .75, "num_steps": 4, "t_shift": .5, "seed": 0, "reference_text": None,
                        "reference": str(reference),
                        "tasks": [{"id": "P1", "index": 0, "text": "Hello.", "out": str(Path(folder) / "P1.wav")}]}
            self.assertFalse(_run_remote(config, store, manifest, "narration"))
            self.assertEqual(remote_mms_align(config, store, "alignment", reference, ["hello"]), (None, None))
