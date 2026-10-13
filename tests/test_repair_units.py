import unittest

from infofilm.repair_units import flag, merge, pointer_errors, repair_request, resolve, units

SCENES = {"lists": {"scenes": "id"}, "anchors": {"scenes": ["point_id", "start_word_id"]}, "operations": ["scenes"]}
ASSETS = {"lists": {"assets": "id", "banners": "scene_key"}}
EVIDENCE = {"lists": {"facts": "id", "uncertainties": "@U"}}
BRIEF = {"lists": {}, "fields": True}


def scene(i, word, brief="ok"):
    return {"id": f"S{i:02d}", "point_id": "P01", "start_word_id": word, "kind": "image", "image_brief": brief}


class PointerTests(unittest.TestCase):
    def setUp(self):
        self.beats = {"scenes": [scene(i, f"w{i:04d}") for i in range(1, 26)]}
        self.prompts = {"assets": [{"id": f"A{i:03d}", "prompt": "x"} for i in range(1, 10)],
                        "banners": [{"scene_key": "110:C03-scene-02", "text": ""}]}

    def test_every_pointer_form_seen_in_the_runs_resolves(self):
        self.assertEqual(resolve("scenes[S17]", self.beats, SCENES), ["S17"])                 # Manhattan C03
        self.assertEqual(resolve("S20 (P10, start_word_id w0339)", self.beats, SCENES), ["S20"])
        self.assertEqual(resolve("scenes[3]", self.beats, SCENES), ["S04"])                   # positional
        self.assertEqual(resolve("assets[A009].prompt", self.prompts, ASSETS), ["A009"])      # Manhattan C08
        self.assertEqual(resolve("assets[id=A013].prompt", {"assets": [{"id": "A013"}]}, ASSETS), ["A013"])
        self.assertEqual(resolve("assets[0].prompt; assets[3].prompt", self.prompts, ASSETS), ["A001", "A004"])  # C07
        self.assertEqual(resolve("assets[].people_in_frame (A001–A004)", self.prompts, ASSETS), ["A001", "A002", "A003", "A004"])
        self.assertEqual(resolve("110:C03-scene-02: banner too long", self.prompts, ASSETS), ["110:C03-scene-02"])
        self.assertEqual(resolve("stage", self.beats, SCENES), [])
        self.assertEqual(resolve("S2", self.beats, SCENES), [])                               # no partial id matches

    def test_string_lists_and_fields_get_code_ids(self):
        evidence = {"facts": [{"id": "F01"}, {"id": "F02"}], "uncertainties": ["Date disputed", "Count unclear"]}
        self.assertEqual(list(units(evidence, EVIDENCE)), ["F01", "F02", "U01", "U02"])
        self.assertEqual(resolve("U02", evidence, EVIDENCE), ["U02"])
        brief = {"resolved_mood": "serious", "theme": {"accent": "#a00"}, "music": {}}
        self.assertEqual(resolve("theme.accent", brief, BRIEF), ["theme"])

    def test_unresolvable_blocking_pointer_is_reported_for_a_review_repair(self):
        review = {"issues": [{"field": "the montage", "severity": "major"}, {"field": "stage", "severity": "major"},
                             {"field": "whatever", "severity": "minor"}]}
        errors = pointer_errors(review, self.beats, SCENES)
        self.assertEqual(len(errors), 1); self.assertIn("'the montage'", errors[0])

    def test_findings_and_validator_errors_map_to_units_and_the_rest_is_stage_level(self):
        review = {"issues": [{"field": "scenes[S17]", "severity": "major", "problem": "reads as a command chain"},
                             {"field": "stage", "severity": "major", "problem": "repeats chapter 2"}]}
        flagged, stage = flag(self.beats, ["S21: diagram labels/details are too long", "Need 1-120 beats"], review, SCENES)
        self.assertEqual(sorted(flagged), ["S17", "S21"])
        self.assertEqual([s.get("problem") or s.get("validation_error") for s in stage], ["repeats chapter 2", "Need 1-120 beats"])


class MergeTests(unittest.TestCase):
    def test_partial_response_replaces_only_flagged_scenes_and_keeps_anchors(self):
        prior = {"scenes": [scene(1, "w0001"), scene(2, "w0010", "too long"), scene(3, "w0020")]}
        response = {"units": {"scenes": [{**scene(2, "w0012", "short fixed")}, scene(3, "w0099", "unasked change")]}}
        merged = merge(prior, response, {"S02": [{}]}, SCENES)
        self.assertEqual([s["image_brief"] for s in merged["scenes"]], ["ok", "short fixed", "ok"])
        self.assertEqual(merged["scenes"][1]["start_word_id"], "w0010")

    def test_full_candidate_response_is_also_merged_by_id(self):
        prior = {"scenes": [scene(1, "w0001"), scene(2, "w0010", "bad"), scene(3, "w0020")]}
        renumbered = {"scenes": [scene(1, "w0001", "rewritten"), scene(2, "w0012", "fixed"), scene(3, "w0016"), scene(4, "w0020")]}
        merged = merge(prior, renumbered, {"S02": [{}]}, SCENES)
        self.assertEqual([s["id"] for s in merged["scenes"]], ["S01", "S02", "S03"])
        self.assertEqual(merged["scenes"][0], prior["scenes"][0]); self.assertEqual(merged["scenes"][1]["image_brief"], "fixed")

    def test_string_units_fields_and_operations(self):
        evidence = {"facts": [{"id": "F01", "claim": "a"}], "uncertainties": ["old", "keep"]}
        merged = merge(evidence, {"units": {"uncertainties": [{"id": "U01", "text": "new"}]}}, {"U01": [{}]}, EVIDENCE)
        self.assertEqual(merged["uncertainties"], ["new", "keep"])
        brief = {"resolved_mood": "serious", "theme": {"accent": "#a00"}}
        self.assertEqual(merge(brief, {"fields": {"theme": {"accent": "#036"}, "resolved_mood": "fun"}}, {"theme": [{}]}, BRIEF),
                         {"resolved_mood": "serious", "theme": {"accent": "#036"}})
        prior = {"scenes": [scene(1, "w0001"), scene(2, "w0010"), scene(3, "w0020")]}
        ops = {"units": {"scenes": []}, "operations": [{"op": "insert_after", "target": "S02",
                                                         "items": [{"point_id": "P01", "start_word_id": "w0015", "kind": "image"}]},
                                                        {"op": "remove", "target": "S03"}]}
        merged = merge(prior, ops, {"S02": [{}]}, SCENES)
        self.assertEqual([s["id"] for s in merged["scenes"]], ["S01", "S02", "S02b", "S03"])  # S03 not flagged: stays
        merged = merge(prior, {"operations": [{"op": "remove", "target": "S03"}]}, {"S03": [{}]}, SCENES)
        self.assertEqual([s["id"] for s in merged["scenes"]], ["S01", "S02"])

    def test_repair_request_lists_only_flagged_units_and_the_partial_shape(self):
        prior = {"scenes": [scene(1, "w0001"), scene(2, "w0010", "bad")]}
        text = repair_request(prior, {"S02": [{"validation_error": "S02: labels too long"}]}, SCENES)
        self.assertIn('"units": {"scenes"', text); self.assertIn("FLAGGED UNITS", text)
        self.assertIn('"S02"', text.split("FLAGGED UNITS")[1]); self.assertNotIn('"S01"', text.split("FLAGGED UNITS")[1])
        self.assertIn('"operations"', text)


class WordCountTests(unittest.TestCase):
    def test_designations_are_one_word_and_times_still_count_each_part(self):
        from infofilm.planning import word_count
        self.assertEqual(word_count("Oak Ridge plants: Y-12, K-25, S-50"), 6)      # Manhattan C04 banner
        self.assertEqual(word_count("At 10:30 p.m. on May 7"), 8)


class RasterLabelTests(unittest.TestCase):
    def test_prompts_asking_the_generator_to_write_a_label_are_a_unit_error(self):
        from infofilm.planning import RASTER_LABEL
        self.assertTrue(RASTER_LABEL.search("Illustrated, clearly labelled as an illustration, not archival footage."))  # C07
        self.assertTrue(RASTER_LABEL.search('A sign with the words "Oak Ridge"'))
        self.assertFalse(RASTER_LABEL.search("A dial with plain unlabeled tick marks."))
        self.assertFalse(RASTER_LABEL.search("An editorial illustration of a laboratory."))


if __name__ == "__main__":
    unittest.main()


class LinkedPointerTests(unittest.TestCase):
    """Cuba C04 image prompts: 'A017.banner' flagged the asset, so the banner unit (160:S18) never changed in 6 rounds."""

    def test_asset_banner_pointer_repairs_the_scene_banner(self):
        from infofilm import repair_units as ru
        from infofilm.planning import image_prompt_units
        candidate = {"assets": [{"id": "A017", "prompt": "Two letters on a desk."}, {"id": "A018", "prompt": "Sea."}],
                     "banners": [{"scene_key": "160:S18", "text": "Two Soviet messages, October 26 and 27, 1962"},
                                 {"scene_key": "160:S19", "text": "Quarantine line"}]}
        spec = image_prompt_units({"A017": [{"scene_key": "160:S18"}], "A018": [{"scene_key": "160:S19"}]})
        self.assertEqual(ru.resolve("A017.banner", candidate, spec), ["160:S18"])
        self.assertEqual(ru.resolve("A017.prompt", candidate, spec), ["A017"])
        self.assertEqual(ru.resolve("A017", candidate, spec), ["A017"])
        review = {"issues": [{"severity": "major", "field": "A017.banner", "problem": "Dates both letters"}]}
        flagged, stage = ru.flag(candidate, [], review, spec)
        self.assertEqual((list(flagged), stage), (["160:S18"], []))
        self.assertIn("A banner is its own unit: point at its id (160:S18, 160:S19)", ru.pointer_guide(candidate, spec))
        # Without links the old behaviour stands.
        self.assertEqual(ru.resolve("A017.banner", candidate, {"lists": {"assets": "id", "banners": "scene_key"}}), ["A017"])


class ScreenUnchangedTests(unittest.TestCase):
    """Dandi C07 image prompts: each round blocked on units nobody had changed or asked about."""

    def test_blocking_findings_may_only_name_changed_or_asked_units(self):
        from infofilm import repair_units as ru
        spec = {"lists": {"assets": "id"}}
        before = {"assets": [{"id": "A010", "prompt": "Khan in crowd"}, {"id": "A011", "prompt": "Bazaar"},
                             {"id": "A012", "prompt": "Rural field"}]}
        after = {"assets": [{"id": "A010", "prompt": "Crowd only"}, {"id": "A011", "prompt": "Bazaar"},
                            {"id": "A012", "prompt": "Rural field"}]}
        review = {"pass": False, "score": 84, "issues": [
            {"severity": "major", "field": "A010,A011,A012.prompt", "problem": "No banner area"},
            {"severity": "major", "field": "A012.setting", "problem": "Rural not in evidence"},
            {"severity": "major", "field": "stage", "problem": "Arc"}]}
        screened = ru.screen_unchanged(review, before, after, raised=set(), spec=spec)
        self.assertEqual(screened["issues"][0]["field"], "A010")                     # narrowed to the changed unit
        self.assertEqual(screened["issues"][1]["severity"], "minor")                 # unchanged, never asked
        self.assertEqual(screened["issues"][2]["severity"], "major")                 # stage level untouched
        asked = ru.screen_unchanged({"issues": [{"severity": "major", "field": "A012", "problem": "Still rural"}]},
                                    before, after, raised={"A012"}, spec=spec)
        self.assertEqual(asked["issues"][0]["severity"], "major")                    # asked and not fixed: still blocks
        self.assertIs(ru.screen_unchanged(review, None, after, set(), spec), review)  # first review: nothing to compare
        lone = ru.screen_unchanged({"pass": False, "score": 85, "issues": [
            {"severity": "major", "field": "A011", "problem": "x"}]}, before, after, set(), spec)
        self.assertTrue(lone["pass"])                                                # nothing left blocking, score >= 82


class OtherListPointerTests(unittest.TestCase):
    """Jet engine C02 evidence: 'F16.narrative_priorities' flagged fact F16 for six rounds; the priority never changed."""

    def test_pointer_to_another_list_resolves_to_that_list(self):
        from infofilm import repair_units as ru
        from infofilm.planning import EVIDENCE_UNITS
        candidate = {"facts": [{"id": "F16", "claim": "Compressors raise pressure."}],
                     "uncertainties": ["Ratios vary."], "narrative_priorities": ["Say it squeezes air.", "Open with thrust."]}
        self.assertEqual(ru.resolve("F16.narrative_priorities", candidate, EVIDENCE_UNITS), ["N01", "N02"])
        self.assertEqual(ru.resolve("F16.claim", candidate, EVIDENCE_UNITS), ["F16"])
