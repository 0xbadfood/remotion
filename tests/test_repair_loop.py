"""The planning loop keeps code between reviewer and planner: findings map to unit ids, only flagged units are asked
for, and the planner's partial answer is merged back by id."""
import json
import tempfile
import unittest
from unittest.mock import Mock

from infofilm.core import Store
from infofilm.llm import Models

UNITS = {"lists": {"points": "id"}, "operations": ["points"]}


def script(*texts):
    return {"points": [{"id": f"P{i:02d}", "narration": t} for i, t in enumerate(texts, 1)]}


def passing():
    return {"pass": True, "score": 90, "issues": []}


class RepairLoopTests(unittest.TestCase):
    def run_loop(self, responses, validator=lambda x: [], units=UNITS):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        store = Store(self.folder.name); self.addCleanup(store.db.close)
        models = Models({"planner_url": "p", "reviewer_url": "r", "max_repairs": 2}, store)
        models.call = Mock(side_effect=responses)
        result = models.loop("script-60", "Task", {}, validator, experimental=True, units=units)
        prompts = [(c.args[0], c.args[1], c.args[2]) for c in models.call.call_args_list]
        return result, prompts

    def test_unparseable_pointer_goes_back_to_the_reviewer_then_only_the_flagged_point_is_asked_for(self):
        bad = {"pass": False, "score": 70, "issues": [{"field": "the second point", "severity": "major", "problem": "wrong date"}]}
        fixed = {"issues": [{"field": "P02", "severity": "major", "problem": "wrong date"}]}
        result, prompts = self.run_loop([script("One.", "Two in 1950.", "Three."), bad, fixed,
                                         {"units": {"points": [{"id": "P02", "narration": "Two in 1945."}]}},
                                         passing(), {"preferred": "A", "reasons": [], "regressions": []}])
        self.assertEqual([p["narration"] for p in result["points"]], ["One.", "Two in 1945.", "Three."])
        self.assertTrue(prompts[2][1].endswith("-pointers"))                  # the reviewer fixed its pointer
        repair = prompts[3][2]
        self.assertIn("TARGETED REPAIR", repair); self.assertIn('"P02"', repair.split("FLAGGED UNITS")[1])
        self.assertNotIn('"P01"', repair.split("FLAGGED UNITS")[1]); self.assertNotIn("the second point", repair)

    def test_a_planner_that_rewrites_everything_still_changes_only_the_flagged_point(self):
        review = {"pass": False, "score": 70, "issues": [{"field": "P02.narration", "severity": "major", "problem": "x"}]}
        result, _ = self.run_loop([script("One.", "Two.", "Three."), review,
                                   script("Changed one.", "Fixed two.", "Changed three.", "Extra four."),
                                   passing(), {"preferred": "A", "reasons": [], "regressions": []}])
        self.assertEqual([p["narration"] for p in result["points"]], ["One.", "Fixed two.", "Three."])

    def test_stage_level_findings_get_a_full_redraft_from_structured_findings(self):
        review = {"pass": False, "score": 60, "issues": [{"field": "stage", "severity": "major", "problem": "no causal arc"}]}
        result, prompts = self.run_loop([script("One.", "Two."), review, script("Because one,", "two."),
                                         passing(), {"preferred": "A", "reasons": [], "regressions": []}])
        self.assertEqual(result, script("Because one,", "two."))
        self.assertIn("REPAIR the prior candidate as a whole", prompts[2][2]); self.assertIn("no causal arc", prompts[2][2])
        self.assertNotIn('"score": 60', prompts[2][2])                         # the raw review is never forwarded

    def test_validator_errors_naming_a_unit_are_targeted(self):
        validator = lambda x: [f"{p['id']}: heading has 9 words; at most 7" for p in x["points"] if p["narration"] == "bad"]
        result, prompts = self.run_loop([script("One.", "bad"), {"units": {"points": [{"id": "P02", "narration": "good"}]}},
                                         passing(), {"preferred": "A", "reasons": [], "regressions": []}], validator)
        self.assertEqual(result["points"][1]["narration"], "good")
        self.assertIn("FLAGGED UNITS", prompts[1][2])

    def test_rounds_lost_only_to_code_checks_do_not_use_up_the_review_budget(self):
        validator = lambda x: ["P01: heading has 9 words; at most 7"] if x["points"][0]["narration"].startswith("bad") else []
        bad = lambda n: {"units": {"points": [{"id": "P01", "narration": f"bad {n}"}]}}
        review = {"pass": False, "score": 70, "issues": [{"field": "P02", "severity": "major", "problem": "x"}]}
        responses = [script("bad 0", "Two."), bad(1), bad(2), {"units": {"points": [{"id": "P01", "narration": "Good."}]}},
                     review, {"units": {"points": [{"id": "P02", "narration": "Fixed."}]}}, passing(),
                     {"preferred": "A", "reasons": [], "regressions": []}]
        result, _ = self.run_loop(responses, validator)          # 3 review rounds + 3 mechanical ones
        self.assertEqual([p["narration"] for p in result["points"]], ["Good.", "Fixed."])

    def test_an_unchanged_replacement_is_called_out_in_the_next_request(self):
        validator = lambda x: ["P02: heading has 9 words; at most 7"] if x["points"][1]["narration"] == "same" else []
        same = {"units": {"points": [{"id": "P02", "narration": "same"}]}}
        result, prompts = self.run_loop([script("One.", "same"), same, {"units": {"points": [{"id": "P02", "narration": "new"}]}},
                                         passing(), {"preferred": "A", "reasons": [], "regressions": []}], validator)
        self.assertEqual(result["points"][1]["narration"], "new")
        self.assertNotIn("UNCHANGED", prompts[1][2]); self.assertIn("UNCHANGED: your last replacement for P02", prompts[2][2])

    def test_cast_people_get_code_assigned_ids(self):
        units = {"lists": {"people": "id"}, "assign": {"people": "R"}}
        result, prompts = self.run_loop([{"people": [{"name": "Leslie Groves"}, {"name": "J. Robert Oppenheimer"}]},
                                         passing()], units=units)
        self.assertEqual([p["id"] for p in result["people"]], ["R01", "R02"])
        self.assertIn("R01", prompts[1][2])                                     # the reviewer is told the unit ids


class ContinuitySeedTests(unittest.TestCase):
    def test_notes_on_this_chapter_flag_points_and_notes_on_another_chapter_stay_stage_level(self):
        from infofilm.planning import continuity_findings
        notes = [{"field": "C02 P03", "problem": "repeats the tank story"},
                 {"field": "C01 script P06-P07", "problem": "C01 reveals the splashdown"},     # Apollo: about C01
                 {"field": "C02", "problem": "handoff missing"}]
        self.assertEqual([f["field"] for f in continuity_findings(notes, "C02")], [" P03", "stage", "stage"])

    def test_a_continuity_note_repairs_only_the_named_point_of_the_accepted_script(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        store = Store(folder.name); self.addCleanup(store.db.close)
        models = Models({"planner_url": "p", "reviewer_url": "r", "max_repairs": 2}, store)
        accepted = script("Bridge from chapter one.", "The tank was built.", "Repeats chapter one's story.")
        models.call = Mock(side_effect=[{"units": {"points": [{"id": "P03", "narration": "Testing began in March."}]}},
                                        passing(), {"preferred": "A", "reasons": [], "regressions": []}])
        result = models.loop("script-60", "Task", {"notes": 1}, lambda x: [], experimental=True, units=UNITS,
                             seed=(accepted, [{"field": " P03", "problem": "repeats chapter one"}]))
        self.assertEqual([p["narration"] for p in result["points"]][:2], ["Bridge from chapter one.", "The tank was built."])
        self.assertEqual(result["points"][2]["narration"], "Testing began in March.")
        first = models.call.call_args_list[0].args[2]
        self.assertIn("TARGETED REPAIR", first); self.assertIn('"P03"', first.split("FLAGGED UNITS")[1])


if __name__ == "__main__":
    unittest.main()


class OutputLimitEscalationTests(unittest.TestCase):
    """Dandi March beat sheet: four drafts at the same 7,000-token cap, each told only to be concise."""

    def test_output_limit_raises_the_cap_for_the_next_draft(self):
        from infofilm.llm import ModelOutputError
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        store = Store(folder.name); self.addCleanup(store.db.close)
        models = Models({"planner_url": "p", "reviewer_url": "r", "max_repairs": 3}, store)
        limit = ModelOutputError("LLM response reached its 7000-token output limit; raw response preserved")
        models.call = Mock(side_effect=[limit, limit, script("One."), passing()])
        result = models.loop("beat-sheet", "Task", {}, lambda x: [], experimental=True, units=UNITS, max_tokens=7000)
        self.assertEqual(result["points"][0]["narration"], "One.")
        caps = [c.kwargs.get("max_tokens") for c in models.call.call_args_list[:3]]
        self.assertEqual(caps, [7000, 11200, 12000])                          # x1.6, then the 12k ceiling


class CallEscalationTests(unittest.TestCase):
    """Calls outside the repair loop (creative brief, orientation, reviews, arbiter) failed or retried at the same cap."""

    def test_output_limit_retries_once_with_30_percent_more_room(self):
        from infofilm.llm import ModelOutputError
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        store = Store(folder.name); self.addCleanup(store.db.close)
        models = Models({"planner_url": "p", "reviewer_url": "r"}, store)
        limit = ModelOutputError("LLM response reached its 7000-token output limit; raw response preserved")
        models._call_once = Mock(side_effect=[limit, {"ok": True}])
        self.assertEqual(models.call("planner", "creative-brief", "Task", max_tokens=7000), {"ok": True})
        self.assertEqual([c.kwargs["max_tokens"] for c in models._call_once.call_args_list], [7000, 9100])
        self.assertTrue(models._call_once.call_args_list[1].args[1].endswith("-escalated"))
        models._call_once = Mock(side_effect=[limit, limit])                   # a second cut-off stands
        with self.assertRaises(ModelOutputError):
            models.call("planner", "x", "Task", max_tokens=7000)
        for kwargs, error in (({}, ModelOutputError("LLM returned invalid JSON")), ({"escalate": False}, limit)):
            models._call_once = Mock(side_effect=[error])
            with self.assertRaises(ModelOutputError):
                models.call("planner", "y", "Task", **kwargs)
            self.assertEqual(models._call_once.call_count, 1)                  # bad JSON / loop drafts: not here
