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

    def test_motion_must_fit_inside_its_beat(self):
        errors = linkwork_beats.beat_errors({}, [self.beat(self.spec, 10.0, 12.0)], self.cards)
        self.assertTrue(any("but the beat lasts 2.0 s" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
