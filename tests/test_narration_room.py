import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from infofilm import narration_room
from infofilm.core import Store, digest

RATE = 16000


def wav(path, seconds, level=1000):
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes(int(level).to_bytes(2, "little", signed=True) * round(seconds * RATE))
    return path


class NarrationRoomTests(unittest.TestCase):
    """2026-10-25: the narration waits for the picture: silence, a fixed phrase or a replay after a beat's last word."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        audio = wav(self.root / "narration.wav", 10)
        words = [{"id": f"w{i}", "word": f"word{i}", "start": s, "end": s + 0.8} for i, s in enumerate([0.5, 2.0, 5.0, 7.0], 1)]
        self.timing = {"audio": str(audio), "audio_sha256": digest(audio.read_bytes()), "words": words,
                       "points": [{"point_id": "P1", "start": 0.0, "end": 4.0, "word_ids": ["w1", "w2"]},
                                  {"point_id": "P2", "start": 4.5, "end": 9.0, "word_ids": ["w3", "w4"]}],
                       "audio_info": {"duration_seconds": 10.0, "sample_rate": RATE}}
        self.script = {"points": [{"id": "P1", "fact_ids": ["F1"]}, {"id": "P2", "fact_ids": ["F2"]}]}
        scene = self.root / "scene.json"
        scene.write_text(json.dumps({"duration": 9.0}))
        self.storyboard = {"scenes": [
            {"id": "S01", "point_id": "P1", "start_word_id": "w1", "kind": "linkwork", "linkwork_file": str(scene)},
            {"id": "S02", "point_id": "P2", "start_word_id": "w3", "kind": "image"}]}
        self.phrases = {"replay": {"path": str(wav(self.root / "replay.wav", 1.0, 3000)), "seconds": 1.0,
                                   "words": [{"word": "Watch", "start": 0.1, "end": 0.4}, {"word": "again", "start": 0.5, "end": 0.9}]},
                        "look": {"path": str(wav(self.root / "look.wav", 0.8, 3000)), "seconds": 0.8,
                                 "words": [{"word": "Look", "start": 0.1, "end": 0.7}]}}

    def tearDown(self):
        self.dir.cleanup()

    def film(self):
        return {"requested_seconds": 60, "script": self.script, "timing": self.timing, "storyboard": self.storyboard,
                "audio": self.timing["audio"], "audio_info": dict(self.timing["audio_info"]), "actual_seconds": 10.0}

    def resolved(self):
        from infofilm.creative import resolved_scenes
        return resolved_scenes(self.storyboard, self.script, self.timing)

    def test_a_scene_longer_than_its_beat_gets_the_seconds_it_needs_after_its_last_word(self):
        # S01's beat runs 0 to 5 s; its scene needs 9 - 1.5 (the held last frame) = 7.5 s: 2.5 s of room after w2.
        inserts = narration_room.plan(self.resolved(), self.timing["words"], 10.0, narration_room.settings(), {})
        self.assertEqual(len(inserts), 1)
        self.assertEqual((inserts[0]["scene"], inserts[0]["after_word"], inserts[0]["seconds"]), ("S01", "w2", 2.5))
        self.assertAlmostEqual(inserts[0]["at"], 2.95)

    def test_the_extended_narration_shifts_only_what_follows(self):
        store = Store(self.root / "project")
        inserts = narration_room.plan(self.resolved(), self.timing["words"], 10.0, narration_room.settings(), {})
        audio, info, timing = narration_room.extend(store, self.film(), inserts, {})
        self.assertAlmostEqual(info["duration_seconds"], 12.5)
        words = {w["id"]: w for w in timing["words"]}
        self.assertEqual((words["w2"]["start"], words["w3"]["start"]), (2.0, 7.5))
        self.assertEqual([(p["start"], p["end"]) for p in timing["points"]], [(0.0, 6.5), (7.0, 11.5)])   # P1 runs on through its room
        self.assertEqual(timing["room"]["added_seconds"], 2.5)
        with wave.open(str(audio)) as out:                      # the inserted seconds are silence
            out.setpos(round(3.5 * RATE))
            self.assertEqual(out.readframes(10), b"\0" * 20)
        self.assertEqual(narration_room.extend(store, self.film(), inserts, {})[0], audio)   # cached

    def test_a_replay_speaks_its_phrase_then_plays_the_scene_again(self):
        self.storyboard["scenes"][0].update(room="replay", importance="core")
        config = {"room_max_beat_seconds": 20, "room_max_share": 2}            # a long room in this 10 s test film
        inserts = narration_room.plan(self.resolved(), self.timing["words"], 10.0, narration_room.settings(config), self.phrases)
        replay = inserts[0]
        # The words end at 2.95 s; the first play ends at 7.5 s; the phrase, then the scene plays from the start again.
        self.assertAlmostEqual(replay["phrase_at"], 4.55 + narration_room.GAP)
        self.assertAlmostEqual(replay["restart"], 2.95 + 4.55 + 2 * narration_room.GAP + 1.0)
        film = self.film()
        with mock.patch.object(narration_room, "phrase_clips", return_value=self.phrases):
            narration_room.apply(config, Store(self.root / "project"), [film])
        self.assertEqual(film["storyboard"]["scenes"][0]["linkwork_restart"], replay["restart"])
        heard = [w for w in film["timing"]["words"] if w.get("room") == "S01"]
        self.assertEqual([w["word"] for w in heard], ["Watch", "again"])
        self.assertIn(heard[0]["id"], film["timing"]["points"][0]["word_ids"])          # captions show the phrase
        self.assertAlmostEqual(heard[0]["start"], 2.95 + replay["phrase_at"] + 0.1, places=2)
        self.assertGreater(film["actual_seconds"], 10.0 + replay["restart"] - 2.95)
        self.assertTrue(Path(film["audio"]).is_file())

    def test_rooms_stay_inside_their_caps_and_core_beats_come_first(self):
        room = {**narration_room.settings(), "max_share": 0.2}                   # 2 s of room in a 10 s film
        inserts = narration_room.plan(self.resolved(), self.timing["words"], 10.0, room, {})
        self.assertEqual(inserts, [])                                             # 2.5 s would not fit
        room = {**narration_room.settings(), "max_beat_seconds": 1.0}
        self.assertEqual(narration_room.plan(self.resolved(), self.timing["words"], 10.0, room, {})[0]["seconds"], 1.0)

    def test_a_voice_without_phrases_gets_a_plain_pause(self):
        self.storyboard["scenes"][1]["room"] = "look"
        inserts = narration_room.plan(self.resolved(), self.timing["words"], 100.0, narration_room.settings(), {})
        self.assertEqual([(i["scene"], i["seconds"], i.get("phrase")) for i in inserts][1], ("S02", 2.5, None))

    def test_off_means_off(self):
        film = self.film()
        narration_room.apply({"narration_room": False}, Store(self.root / "project"), [film])
        self.assertIs(film["timing"], self.timing)


class SpanningBeatTests(unittest.TestCase):
    """A Linkwork or figure beat may carry one explanation across narration points (through_point_id)."""

    def test_a_scene_can_carry_its_explanation_through_later_points(self):
        from infofilm.creative import beat_errors, resolved_scenes
        from test_creative import example
        script, timing, storyboard, _ = example()
        scenes = storyboard["scenes"]
        scenes[1].update(kind="compare", labels=["a", "b"], details=["c", "d"], through_point_id="P2")
        del scenes[2]
        self.assertIn("only linkwork or figure beats carry", " ".join(beat_errors(storyboard, script, timing)))
        scenes[1].update(kind="figure", through_point_id="P2")
        errors = beat_errors(storyboard, script, timing, figures=None)
        self.assertFalse(any("narration point" in e or "cover every" in e for e in errors), errors)
        resolved = resolved_scenes(storyboard, script, timing)
        self.assertEqual(resolved[1]["end"], 18)
        scenes.append({**scenes[0], "id": "S9", "point_id": "P2", "start_word_id": "w6"})
        self.assertIn("P2 is carried by S2", " ".join(beat_errors(storyboard, script, timing)))

    def test_through_must_point_forward(self):
        from infofilm.creative import beat_errors
        from test_creative import example
        script, timing, storyboard, _ = example()
        storyboard["scenes"][2]["through_point_id"] = "P1"
        self.assertIn("through_point_id must be a later narration point", " ".join(beat_errors(storyboard, script, timing)))


if __name__ == "__main__":
    unittest.main()


class StagedPlanningTests(unittest.TestCase):
    """2026-10-25: concept films plan a beat sheet first, then the full beats a batch at a time from it."""

    def sheet(self):
        from test_creative import example
        script, timing, storyboard, _ = example()
        rows = [{"id": f"S0{i}", "point_id": s["point_id"], "start_word_id": s["start_word_id"], "kind": "image",
                 "intent": "The puppy eats from its bowl", "importance": "core" if i == 1 else "support"}
                for i, s in enumerate(storyboard["scenes"], 1)]
        return script, timing, storyboard, rows

    def test_the_sheet_is_checked_by_code(self):
        from infofilm.creative import sheet_errors
        script, timing, _, rows = self.sheet()
        self.assertEqual(sheet_errors({"beats": rows}, script, timing), [])
        rows[0]["intent"] = " ".join(["word"] * 40)                 # the free-fall sheet's intents ran to 31-39 words
        self.assertEqual(sheet_errors({"beats": rows}, script, timing), [])
        rows[0]["room"] = "hold"
        rows[1].update(room="look")
        rows[2].update(kind="linkwork", verb="stage")
        problems = " ".join(sheet_errors({"beats": rows}, script, timing))
        self.assertIn("S02: only a core beat gets room", problems)
        self.assertIn("unsupported visual beat kind 'linkwork'", problems)
        linkwork = {"config": {}, "cards": [{"name": "stage"}]}
        rows[1].pop("room")
        rows[2].update(importance="core", room="replay", point_id="P2")
        self.assertEqual(sheet_errors({"beats": rows}, script, timing, linkwork=linkwork), [])
        # S03's 2 s beat (ending at 18 s) cannot hold a scene that runs at least 30 s, even with 12 s of room.
        problems = sheet_errors({"beats": rows}, script, timing, linkwork=linkwork, typical={"stage": 31.5})
        self.assertIn("S03: a stage scene needs at least 30 s", " ".join(problems))

    def test_a_late_start_says_how_to_show_something_from_a_later_sentence(self):
        # 2026-10-29, the scattering film: the reviewer wanted an equation beat to start at the sentence stating the
        # law, code wanted the point's first beat at its first word, and the sheet ran out of tries
        from infofilm.creative import sheet_errors
        script, timing, _, rows = self.sheet()
        rows[0]["start_word_id"] = "w002"
        problems = " ".join(sheet_errors({"beats": rows}, script, timing))
        self.assertIn("open P1 with another beat and start that one at the sentence that states it", problems)

    def test_an_object_where_a_name_belongs_is_reported_not_a_crash(self):
        # 2026-10-27, the PDA film: the planner put a whole figure spec in a beat's figure field
        from infofilm.creative import sheet_errors
        script, timing, _, rows = self.sheet()
        rows[0].update(kind="figure", figure={"id": "pda-heart-front", "stage": "stage-duct-open"})
        rows[1].update(kind={"name": "image"})
        figures = {"library": [{"id": "pda-heart-front"}]}
        problems = " ".join(sheet_errors({"beats": rows}, script, timing, figures))
        self.assertIn("S01: figure is the id alone (a string)", problems)
        self.assertIn("S02", problems)

    def test_batches_cut_only_where_a_new_point_starts(self):
        from infofilm import creative
        _, _, _, rows = self.sheet()
        with mock.patch.object(creative, "STAGED_BEATS", 1):
            self.assertEqual([[r["id"] for r in b] for b in creative.sheet_batches(rows)], [["S01", "S02"], ["S03"]])

    def test_pass_two_writes_each_batch_from_its_rows(self):
        from infofilm import creative
        script, timing, storyboard, rows = self.sheet()
        rows[0]["room"] = "hold"
        film = {"script": script, "timing": timing, "requested_seconds": 60, "actual_seconds": 18}
        context = {"narration_points": [{"point_id": "P1"}, {"point_id": "P2"}], "script": script}
        seen = []

        class Models:
            config = {}

            def loop(self, stage, task, context, validator, **kwargs):
                seen.append((stage, context))
                if stage.startswith("visual-sheet"):
                    assert "PASS 1 OF 2" in task
                    # A repair that added a beat after S01 named it S01b: valid, and renumbered before pass 2.
                    repaired = [rows[0], dict(rows[1], id="S01b"), dict(rows[2], id="S02")]
                    assert not validator({"beats": repaired}), validator({"beats": repaired})
                    return {"beats": repaired}
                assert "PASS 2" in task and "never report the choice of kind, verb or figure, or where a beat starts" in task
                ids = [r["id"] for r in context["beat_sheet"]]
                scenes = [dict(s, id=f"S0{n}", camera="still") for n, s in enumerate(storyboard["scenes"], 1) if f"S0{n}" in ids]
                assert not validator({"scenes": scenes}), validator({"scenes": scenes})
                wrong = [dict(scenes[0], kind="compare", labels=["a", "b"], details=["c", "d"])] + scenes[1:]
                assert f"{ids[0]}: keep the sheet's kind 'image'" in " ".join(validator({"scenes": wrong}))
                return {"scenes": scenes}
        with mock.patch.object(creative, "STAGED_BEATS", 1):
            board = creative.staged_beats(Models(), film, {}, "TASK", context, None, True)
        self.assertEqual([s for s, _ in seen], ["visual-sheet-60", "visual-beats-60-p1", "visual-beats-60-p2"])
        self.assertEqual([r["id"] for r in seen[2][1]["beat_sheet"]], ["S03"])
        self.assertEqual([s["id"] for s in seen[2][1]["previous_scenes"]], ["S01", "S02"])
        self.assertEqual([r["id"] for r in seen[1][1]["next_beats"]], ["S03"])
        self.assertEqual([(s["id"], s["importance"], s.get("room")) for s in board["scenes"]],
                         [("S01", "core", "hold"), ("S02", "support", None), ("S03", "support", None)])

    def test_the_sheet_sees_verbs_without_their_specs(self):
        from infofilm.creative import sheet_options
        context = {"linkwork_options": {"verbs": [{"name": "stage", "about": "props", "fields": {"a": 1}, "example": {}}],
                                        "props": {"car": "a car"}}}
        verbs = sheet_options(context, {"stage": 9.5})["linkwork_options"]["verbs"]
        self.assertEqual(verbs, [{"name": "stage", "about": "props", "options": ["a"], "typical_seconds": 9.5}])
        # A verb whose card names a shorter use shows both; its shortest spec stays out of pass 1 like the example.
        context["linkwork_options"]["verbs"][0]["shortest"] = {"verb": "stage"}
        verbs = sheet_options(context, {"stage": 25.0}, {"stage": 8.3})["linkwork_options"]["verbs"]
        self.assertEqual(verbs, [{"name": "stage", "about": "props", "options": ["a"], "typical_seconds": 25.0, "shortest_seconds": 8.3}])
        self.assertIn("fields", context["linkwork_options"]["verbs"][0])                # pass 2 keeps them

    def test_the_sheet_judges_a_verb_by_its_shortest_use(self):
        # 2026-10-28, Class 10 Light C2: a four-position example kept ray_diagram out of every short beat
        from infofilm import linkwork_beats
        cards = [{"name": "ray_diagram", "example": {"verb": "ray_diagram", "n": 4}, "shortest": {"verb": "ray_diagram", "n": 1}},
                 {"name": "stage", "example": {"verb": "stage"}}]
        runs = {linkwork_beats._key(c["example"]): 25.0 for c in cards}
        runs[linkwork_beats._key(cards[0]["shortest"])] = 8.3
        fake = lambda config, specs: {linkwork_beats._key(s): {"errors": [], "duration": runs[linkwork_beats._key(s)]} for s in specs}
        with mock.patch.object(linkwork_beats, "check_specs", fake):
            self.assertEqual(linkwork_beats.typical_seconds({}, cards), {"ray_diagram": 25.0, "stage": 25.0})
            self.assertEqual(linkwork_beats.typical_seconds({}, cards, "shortest"), {"ray_diagram": 8.3, "stage": 25.0})
