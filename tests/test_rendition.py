import json
import math
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from infofilm import rendition
from infofilm.core import digest


def tone(path, seconds, voice=0.3, room=0.0003):
    """Half a second of room tone, then alternating 'speech' and room, like a read passage."""
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(48000)
        frames = bytearray()
        for i in range(int(seconds * 48000)):
            speaking = int(i / 24000) % 2 == 1
            value = voice * math.sin(i * .05) if speaking else room * math.sin(i * .9)
            frames += struct.pack("<h", int(value * 32767))
        f.writeframes(bytes(frames))


def make_run(root):
    run = root / "run"
    (run / "film-60").mkdir(parents=True)
    (run / "images").mkdir()
    (run / "linkwork").mkdir()
    (run / "images" / "a.png").write_bytes(b"picture")
    (run / "images" / "unused.png").write_bytes(b"not in this film")
    (run / "linkwork" / "s2.json").write_text('{"duration": 3}')
    video = run / "videos" / "timeline.json"
    video.parent.mkdir()
    video.write_text(json.dumps({"data": {"width": 1920, "height": 1080, "fps": 30}}))
    sha = digest(b"picture")
    (run / "film-60" / "script.json").write_text(json.dumps({"title": "T", "requested_seconds": 60, "points": [{"id": "P01", "narration": "x", "fact_ids": []}]}))
    (run / "film-60" / "storyboard.json").write_text(json.dumps({"scenes": [
        {"id": "S01", "kind": "image", "asset_id": "A001"},
        {"id": "S02", "kind": "linkwork", "linkwork": {"verb": "x"}, "linkwork_file": str(run / "linkwork" / "s2.json"), "room_seconds": 2.0, "linkwork_restart": 4.0}]}))
    (run / "image_manifest.json").write_text(json.dumps({"A001": {"file": str(run / "images" / "a.png"), "sha256": sha, "prompt": "long"},
                                                        "A002": {"file": str(run / "images" / "unused.png"), "sha256": "x"}}))
    (run / "evidence-final.json").write_text('{"facts": []}')
    (run / "creative-brief.json").write_text('{"resolved_mood": "curious"}')
    (run / "sources.json").write_text('{"results": [{"url": "u", "title": "t", "raw": "long page"}]}')
    (run / "delivery.json").write_text(json.dumps({"subject": "S", "music_engine": "none", "outputs": [{"actual_seconds": 58.0, "timeline": str(video)}]}))
    return run


class Bundle(unittest.TestCase):
    def test_a_bundle_carries_its_images_and_no_absolute_path_or_voice_timing(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            out = rendition.export(make_run(root), root / "b" / "C1", chapter="c", concept="C1")
            manifest = rendition.verify(out)
            self.assertEqual(manifest["concept"], "C1")
            images = json.loads((out / "images.json").read_text())
            self.assertEqual(list(images), ["A001"])
            self.assertEqual((out / images["A001"]["file"]).read_bytes(), b"picture")
            board = (out / "storyboard.json").read_text()
            self.assertNotIn(str(root), board)
            scene = json.loads(board)["scenes"][1]
            self.assertTrue((out / scene["linkwork_file"]).is_file())
            self.assertNotIn("room_seconds", scene)
            self.assertNotIn("linkwork_restart", scene)
            self.assertEqual(rendition._resolve(scene, out)["linkwork_file"], str(out / scene["linkwork_file"]))

    def test_a_changed_file_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            out = rendition.export(make_run(root), root / "b")
            next((out / "assets").iterdir()).write_bytes(b"swapped")
            with self.assertRaises(ValueError):
                rendition.verify(out)

    def test_a_bundle_is_never_written_over(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            run = make_run(root)
            rendition.export(run, root / "b")
            with self.assertRaises(ValueError):
                rendition.export(run, root / "b")


class Voice(unittest.TestCase):
    def measures(self, **k):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "x.wav"
            tone(path, k.pop("seconds", 12), **k)
            return rendition.voice_measures(path)

    def test_a_quiet_room_passes_and_a_noisy_one_is_asked_to_record_again(self):
        self.assertEqual(rendition.voice_problems(self.measures()), [])
        problems = rendition.voice_problems(self.measures(room=.1))
        self.assertTrue(any("background noise" in p for p in problems), problems)

    def test_short_quiet_and_clipped_takes_are_named(self):
        self.assertTrue(any("short" in p for p in rendition.voice_problems(self.measures(seconds=3))))
        self.assertTrue(any("quiet" in p for p in rendition.voice_problems(self.measures(voice=.002, room=.00001))))
        self.assertTrue(any("distorted" in p for p in rendition.voice_problems({"seconds": 12, "speech_db": -15, "snr_db": 40, "clipped": .01})))

    def test_the_passage_must_be_the_one_read(self):
        self.assertEqual(rendition.passage_match(rendition.PASSAGE), 1.0)
        self.assertLess(rendition.passage_match("good morning class today we study motion"), rendition.MIN_MATCH)
        self.assertTrue(any("passage" in p for p in rendition.voice_problems({"seconds": 12, "speech_db": -15, "snr_db": 40, "clipped": 0}, .2)))


class Look(unittest.TestCase):
    def test_every_template_passes_its_contrast_check(self):
        for name, template in rendition.TEMPLATES.items():
            self.assertEqual(rendition.template_errors(template), [], name)

    def test_templates_are_dark_because_book_figures_are_drawn_white(self):
        for name, template in rendition.TEMPLATES.items():
            self.assertLess(rendition.luminance(template["background"]), .1, name)

    def test_an_unreadable_template_is_refused(self):
        bad = {**rendition.TEMPLATES["midnight"], "text": "#101a34"}
        with self.assertRaises(ValueError):
            rendition.dress({"scenes": []}, bad)

    def test_dressing_recolours_linkwork_drops_a_dark_backdrop_and_adds_the_disclosure(self):
        data = {"scenes": [{"id": "a", "linkwork": {"duration": 3, "theme": "dark"}, "backdrop": "x.png"}], "creativeBrief": {"resolved_mood": "m"}}
        out = rendition.dress(data, rendition.TEMPLATES["midnight"], name="Asha  Rao")
        self.assertEqual(out["scenes"][0]["linkwork"]["theme"]["paper"], "#101a33")
        self.assertNotIn("backdrop", out["scenes"][0])
        self.assertEqual(out["creativeBrief"]["theme"]["background"], "#101a33")
        self.assertEqual(out["creativeBrief"]["resolved_mood"], "m")
        self.assertEqual(out["school"]["disclosure"], "Narration by AI, in Asha Rao's voice, used with consent.")
        self.assertEqual(data["scenes"][0]["linkwork"]["theme"], "dark")

    def test_a_logo_is_reencoded_small(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "logo.png"
            Image.new("RGB", (2000, 1000), "red").save(path)
            uri = rendition.logo_uri(path)
        self.assertTrue(uri.startswith("data:image/png;base64,"))
        self.assertLess(len(uri), 20000)

    def test_lost_anchors_are_named(self):
        self.assertEqual(rendition.anchor_problems({"scenes": [{"id": "S1", "start_word_id": "w9"}, {"id": "S2", "start_word_id": "w1"}]}, {"words": [{"id": "w1"}]}), ["S1"])


class Sealed(unittest.TestCase):
    def test_a_rendition_may_not_plan_or_read_the_digest_but_may_call_the_arbiter(self):
        from unittest import mock
        from infofilm.llm import Models
        reached = []
        with mock.patch.object(Models, "call", lambda self, role, name, *a, **k: reached.append(name) or '{"ok": true}'):
            with rendition.sealed():
                models = Models.__new__(Models)
                with self.assertRaises(PermissionError):
                    models.call("planner", "visual-sheet", "p")
                with self.assertRaises(PermissionError):
                    models.call_json("planner", "script-review", "p", max_tokens=10)
                with self.assertRaises(PermissionError):
                    open("/tmp/textbook-digest/x.json")
                models.call("reviewer", "audio-semantic-abc", "p")
                try:
                    models.call_json("reviewer", "audio-semantic-def", "p", max_tokens=10)
                except PermissionError:
                    self.fail("the audio arbiter must stay allowed through call_json")
                except Exception:
                    pass
        self.assertEqual(reached[:2], ["audio-semantic-abc", "audio-semantic-def"])

    def test_a_rendition_reaches_the_reviewer_a_queued_film_would(self):
        from unittest import mock
        with mock.patch("infofilm.llm_settings.Registry.freeze", lambda self, config: {**config, "reviewer_url": "frozen"}) as _:
            self.assertEqual(rendition.film_config()["reviewer_url"], "frozen")

    def test_a_vibevoice_rendition_turns_off_the_lexicon_that_asks_an_llm(self):
        source = Path(rendition.__file__).read_text()
        self.assertIn('"speech_conditioning": False', source)
        self.assertEqual(rendition.ENGINES, ("luxtts", "vibevoice"))

    def test_the_arbiters_name_is_the_one_the_audit_uses(self):
        source = Path(rendition.__file__).with_name("audio_audit.py").read_text()
        self.assertIn('"audio-semantic-" + identity', source)

    def test_a_bundles_evidence_names_no_path(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            run = make_run(root)
            (run / "evidence-final.json").write_text(json.dumps({"root": "/home/x/textbook-digest", "facts": [{"id": "F1", "source_ids": ["S1"], "svg": "/home/x/textbook-digest/a.svg"}]}))
            out = rendition.export(run, root / "b")
            self.assertEqual(json.loads((out / "evidence-final.json").read_text()), {"facts": [{"id": "F1", "source_ids": ["S1"]}]})


if __name__ == "__main__":
    unittest.main()
