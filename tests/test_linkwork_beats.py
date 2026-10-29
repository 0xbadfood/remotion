import unittest

from infofilm import linkwork_beats


class LinkworkBeatTests(unittest.TestCase):
    """Linkwork beats are compiled and checked by Linkwork itself; a spec that fails or outlasts its beat is an error."""

    cards = [{"name": "motion_on_line"}]
    spec = {"verb": "motion_on_line", "axis": {"range": [-4, 8], "unit": "m"}, "object": "car",
            "moves": [{"t": 0, "x": 0}, {"t": 3, "x": 6}]}

    def beat(self, spec, start=0.0, end=6.0):
        return {"id": "S03", "kind": "linkwork", "linkwork": spec, "start": start, "end": end}

    def test_a_good_spec_passes_and_reports_its_facts(self):
        self.assertEqual(linkwork_beats.beat_errors({}, [self.beat(self.spec)], self.cards), [])
        result = linkwork_beats.check_specs({}, [self.spec])
        facts = next(iter(result.values()))["facts"]
        self.assertTrue(any("velocity +2 m/s" in f for f in facts), facts)

    def test_errors_come_back_in_plain_words(self):
        off = {**self.spec, "moves": [{"t": 0, "x": 0}, {"t": 2, "x": 20}]}
        self.assertTrue(any("off the axis" in e for e in linkwork_beats.beat_errors({}, [self.beat(off)], self.cards)))
        self.assertTrue(any("not one of" in e for e in linkwork_beats.beat_errors({}, [self.beat({"verb": "pendulum"})], self.cards)))

    def test_a_blank_option_is_an_option_not_taken(self):
        # 2026-10-29, the force-on-a-conductor film: reverse: "" on five beats, each a code error
        spec = {**self.spec, "title": "", "object": "car", "illustrative": None}
        self.assertEqual(linkwork_beats.beat_errors({}, [self.beat(spec)], self.cards), [])
        self.assertNotIn("title", spec)

    def test_errors_offer_illustrative_for_values_the_narration_never_gives(self):
        # 2026-10-29: the reviewer struck a circuit's invented values and the verb asked for them back, round and round
        off = {**self.spec, "moves": [{"t": 0, "x": 0}, {"t": 2, "x": 20}]}
        problems = " ".join(linkwork_beats.beat_errors({}, [self.beat(off)], self.cards))
        self.assertIn("set illustrative:true", problems)
        problems = " ".join(linkwork_beats.beat_errors({}, [self.beat({**off, "illustrative": True})], self.cards))
        self.assertNotIn("set illustrative:true", problems)

    def test_two_cases_side_by_side_are_one_compound_beat(self):
        # 2026-10-28, Class 10 Light C2: the sheet asked for a concave and a convex mirror side by side; one
        # ray_diagram draws one mirror, and the beat failed every round until the plan ran out of tries.
        cards = [{"name": "ray_diagram"}]
        part = lambda device, x: {"id": device.split("_")[0], "box": [x, 0, 960, 1080], "spec": {
            "verb": "ray_diagram", "device": device, "focal_length": 10, "objects": ["infinity"], "panel": False}}
        pair = {"compound": True, "parts": [part("concave_mirror", 0), part("convex_mirror", 960)]}
        self.assertEqual(linkwork_beats.spec_verbs(pair), ["ray_diagram", "ray_diagram"])
        self.assertEqual(linkwork_beats.beat_errors({}, [self.beat(pair, 0.0, 9.0)], cards), [])
        stray = {"compound": True, "parts": [part("concave_mirror", 0), {**part("convex_mirror", 960), "spec": {"verb": "pendulum"}}]}
        self.assertTrue(any("'pendulum' is not one of" in e for e in linkwork_beats.beat_errors({}, [self.beat(stray)], cards)))
        self.assertIsNone(linkwork_beats.spec_verbs({"compound": True, "parts": []}))
        self.assertTrue(any("compound:true" in e for e in linkwork_beats.beat_errors({}, [self.beat({"device": "x"})], cards)))
        from infofilm.creative import match_errors
        row = {"id": "S06", "point_id": "P07", "kind": "linkwork", "verb": "ray_diagram"}
        scene = {"id": "S06", "point_id": "P07", "kind": "linkwork", "linkwork": pair}
        self.assertFalse([e for e in match_errors({"scenes": [scene]}, [row]) if "the sheet chose the verb" in e])

    def test_motion_must_fit_inside_its_beat(self):
        errors = linkwork_beats.beat_errors({"narration_room": False}, [self.beat(self.spec, 10.0, 11.0)], self.cards)
        self.assertTrue(any("but the beat is only 1.0 s" in e and "join this passage" in e for e in errors), errors)

    def test_the_narration_waits_for_a_scene_up_to_its_room(self):
        # 2026-10-25: the narration waits (narration_room) instead of the scene playing faster or going back.
        self.assertEqual(linkwork_beats.beat_errors({}, [self.beat(self.spec, 10.0, 11.0)], self.cards), [])
        slow = {**self.spec, "moves": [{"t": 0, "x": 0}, {"t": 30, "x": 6}]}
        errors = linkwork_beats.beat_errors({}, [self.beat(slow, 10.0, 11.0)], self.cards)
        self.assertTrue(any("the narration can wait 12 s more" in e for e in errors), errors)

    def test_a_scene_a_little_long_plays_faster_instead_of_failing(self):
        # A graph's own drawing pace (6.8 s) in a 4.8 s beat failed nine repairs: nothing in its spec could shorten it.
        self.assertEqual(linkwork_beats.beat_errors({}, [self.beat(self.spec, 0.0, 2.5)], self.cards), [])
        self.assertEqual(linkwork_beats.speed_to_fit(6.8, 4.8), 1.104)
        self.assertEqual(linkwork_beats.speed_to_fit(4.0, 4.8), 1.0)
        self.assertEqual(linkwork_beats.speed_to_fit(30.0, 4.8), linkwork_beats.FIT)


if __name__ == "__main__":
    unittest.main()


class VectorFirstTests(unittest.TestCase):
    """2026-10-24: a concept film painted a car on a road, a speedometer and a notebook where props could draw them."""

    def test_every_physics_film_is_offered_the_general_props_and_told_vector_first(self):
        evidence = {"curriculum": {"concept": "Instantaneous velocity and speed", "linkwork": {"subject": "physics"}}}
        names = [c["name"] for c in linkwork_beats.shortlist({}, evidence, {"subject": "physics"})]
        self.assertIn("stage", names)
        options = linkwork_beats.planner_options([{"name": "stage", "about": "a world of props"}], {})
        for prop in ("figure", "bulb", "stopwatch", "speedometer", "car"):
            self.assertIn(prop, options["props"])
        self.assertIn("VECTOR FIRST", linkwork_beats.TASK)
        self.assertIn("a car on a road is a prop doing physics", linkwork_beats.TASK)


class IllustrativeTests(unittest.TestCase):
    def test_a_passage_with_no_numbers_is_drawn_illustrative_not_painted(self):
        self.assertIn("set illustrative:true", linkwork_beats.TASK)
        self.assertIn("never report them", linkwork_beats.TASK)
        spec = {"verb": "motion_on_line", "axis": {"range": [0, 20]}, "object": "car", "illustrative": True,
                "formula": {"expr": "0.5*t^2", "t": [0, 6]}, "readouts": ["t", "x", "v"]}
        result = linkwork_beats.check_specs({}, [spec])[linkwork_beats._key(spec)]
        self.assertEqual((result["errors"], result["facts"]), ([], []))


class LinkworkCrashTests(unittest.TestCase):
    def test_a_crash_is_reported_with_its_message_not_as_bad_json(self):
        import tempfile, os
        from infofilm import linkwork_beats
        with tempfile.TemporaryDirectory() as d:
            node = os.path.join(d, "node")
            with open(node, "w") as f:
                f.write("#!/bin/sh\necho 'TypeError: entries is not a function' >&2\nexit 1\n")
            os.chmod(node, 0o755)
            linkwork_beats._CHECKED.clear()
            with self.assertRaisesRegex(RuntimeError, "linkwork batch failed: TypeError: entries is not a function"):
                linkwork_beats.check_specs({"node": node}, [{"verb": "stage"}])

