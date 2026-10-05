import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from infofilm.core import digest, read, write
from infofilm.media import audio_info
from infofilm.pipeline import create
from infofilm.resume import narration_checkpoint
from test_resume_music import checkpoint
from test_audio import wav


def narration_fixture(project):
    delivery = checkpoint(project)
    image = project/"images/approved.png"
    image.parent.mkdir()
    image.write_bytes(b"approved picture")
    write(project/"image_manifest.json", {"A1": {"file": str(image), "sha256": digest(image.read_bytes())}})
    write(project/"evidence-final.json", {"facts": [{"id": "F1", "source_ids": ["S1"]}]})
    write(project/"sources.json", {"results": [{"url": "https://example.org/source"}]})
    write(project/"config-snapshot.json", {"reference_audio": str(project/"audio/60/voice.wav"),
        "language": "en_indian", "tts_url": "local"})
    for duration in [60, 120]:
        selected = read(project/f"film-{duration}/audio-selection.json")
        selected["script"]["points"][0]["fact_ids"] = ["F1"]
        write(project/f"film-{duration}/audio-selection.json", selected)
        write(project/f"film-{duration}/script.json", selected["script"])
        write(project/f"film-{duration}/storyboard.json", {"scenes": [{"id": "S01", "point_id": "P1",
            "start_word_id": "w0001", "kind": "image", "asset_id": "A1", "image_brief": "Subject context",
            "heading": "Explanation", "summary": "A clear explanation", "motion": "hold",
            "copy_density": "minimal", "rationale": "Clarify the subject"}]})
    return delivery


class NarrationResumeTests(unittest.TestCase):
    def settings(self, project):
        return SimpleNamespace(project=project, resume_from="narration", tts_engine="vibevoice",
            subject=None, reference_audio=project/"audio/60/voice.wav", reference_transcript="An explanation.")

    def voice(self, config, store, points, name):
        audio = wav(store.root/"repaired"/name/"narration.wav", frames=16000)
        return audio, {**audio_info(audio), "point_clips": [{"point_id": p["id"]} for p in points]}

    def timing(self, config, store, audio, script, info, name):
        return {"audio": str(audio), "audio_sha256": info["audio_sha256"], "audio_info": info,
            "text_sha256": digest("An explanation."),
            "words": [{"id": "w0001", "word": "An", "start": .1, "end": .3},
                      {"id": "w0002", "word": "explanation.", "start": .3, "end": .8}],
            "points": [{"point_id": "P1", "start": .1, "end": .8, "word_ids": ["w0001", "w0002"]}]}

    def test_repair_reuses_script_and_images_retimes_both_orientations_and_preserves_original_video(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            original = narration_fixture(project)
            image_before = (project/"image_manifest.json").read_bytes()
            def compile(config, store, *args, **kwargs):
                root = store.root/"new-video"/f"{config['width']}-{args[6]}"
                root.mkdir(parents=True, exist_ok=True)
                return root, root/"timeline.json"
            with patch("infofilm.vibe_media.render_vibevoice", side_effect=self.voice) as voice, \
                 patch("infofilm.vibe_media.align_vibevoice", side_effect=self.timing) as mfa, \
                 patch("infofilm.media.audit_audio", return_value={"status": "passed", "score": 1}), \
                 patch("infofilm.resume.prepare_music", side_effect=lambda config, store, film, brief, engine: {"engine": "none"}), \
                 patch("infofilm.video.compile_timeline", side_effect=compile) as compiler, \
                 patch("infofilm.video.render", side_effect=lambda config, store, root, timeline: root/"video.mp4") as render, \
                 patch("infofilm.delivery.preview", return_value=project), \
                 patch("infofilm.pipeline.research", side_effect=AssertionError("No research")), \
                 patch("infofilm.pipeline.generate_images", side_effect=AssertionError("No image generation")):
                create(self.settings(project))
            self.assertEqual(voice.call_count, 2)
            self.assertEqual(mfa.call_count, 2)
            self.assertEqual(compiler.call_count, 4)
            self.assertEqual(render.call_count, 4)
            self.assertEqual((project/"image_manifest.json").read_bytes(), image_before)
            self.assertEqual(read(next(project.glob("delivery-history/*/delivery.json"))), original)
            updated = read(project/"delivery.json")
            self.assertTrue(all(o["narration_repaired"] and o["images_reused"] for o in updated["outputs"]))
            self.assertTrue(all(Path(o["video"]).exists() for o in original["outputs"]))

    def test_changed_picture_fails_before_gpu_work(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            narration_fixture(project)
            (project/"images/approved.png").write_bytes(b"changed")
            with patch("infofilm.vibe_media.render_vibevoice", side_effect=AssertionError("No GPU")):
                with self.assertRaisesRegex(ValueError, "image changed"):
                    create(self.settings(project))

    def test_failed_completeness_check_preserves_previous_delivery_and_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            original = narration_fixture(project)
            previous = (project/"film-60/audio-selection.json").read_bytes()
            with patch("infofilm.vibe_media.render_vibevoice", side_effect=self.voice), \
                 patch("infofilm.media.audit_audio", return_value={"status": "failed", "reasons": ["incomplete_ending"]}), \
                 patch("infofilm.video.render", side_effect=AssertionError("No delivery of incomplete speech")):
                with self.assertRaisesRegex(RuntimeError, "completeness"):
                    create(self.settings(project))
            self.assertEqual(read(project/"delivery.json"), original)
            self.assertEqual((project/"film-60/audio-selection.json").read_bytes(), previous)
            self.assertEqual(narration_checkpoint(project)[0], original)


if __name__ == "__main__":
    unittest.main()
