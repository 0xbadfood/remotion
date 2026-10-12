import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from infofilm.beat_sheet import UNITS, beat_errors, chapter_inputs, chapter_slice, make_beat_sheet
from infofilm.core import write
from infofilm.planning import script_errors

CHAPTERS = [{"id": "C01", "word_budget": 200, "facts": [{"id": "F01"}, {"id": "F02"}]},
            {"id": "C02", "word_budget": 240, "facts": [{"id": "F01"}, {"id": "F03"}]}]


def sheet(**changes):
    value = {"chapters": [{"id": "C01", "opening": "Opens the film.", "closing": "Hands to C02.", "people": ["Leslie Groves"]},
                          {"id": "C02", "opening": "From the decision.", "closing": "Concludes the film.", "people": []}],
             "beats": [{"id": "B01", "chapter": "C01", "intent": "Fission found", "fact_ids": ["F01"], "words": 100},
                       {"id": "B02", "chapter": "C01", "intent": "Letter sent", "fact_ids": ["F02"], "words": 100},
                       {"id": "B03", "chapter": "C02", "intent": "Army takes over", "fact_ids": ["F03"], "words": 120},
                       {"id": "B04", "chapter": "C02", "intent": "Sites chosen", "fact_ids": ["F01"], "words": 120}]}
    value.update(changes)
    return value


class BeatSheetTests(unittest.TestCase):
    def test_a_valid_sheet_passes(self):
        self.assertEqual(beat_errors(sheet(), CHAPTERS, .15), [])

    def test_errors_name_the_beat_or_chapter_they_belong_to(self):
        bad = sheet()
        bad["beats"][1]["fact_ids"] = ["F03"]                  # C02's fact used in C01
        bad["beats"][2]["words"] = 400                         # C02 now 520 words for a 240 budget
        errors = beat_errors(bad, CHAPTERS, .15)
        self.assertTrue(any(e.startswith("B02: fact_ids must be one or more of C01's") for e in errors))
        budget = next(e for e in errors if "beats allocate 520 words; its budget is 240" in e)
        self.assertTrue(budget.startswith("C02 (B03, B04):"))
        from infofilm.repair_units import error_units
        self.assertEqual(sorted(error_units(budget, bad, UNITS)), ["B03", "B04", "C02"])   # the beats get flagged too

    def test_order_count_and_grouping_are_code_checked(self):
        self.assertIn("chapters must list exactly C01 C02", beat_errors(sheet(chapters=sheet()["chapters"][::-1]), CHAPTERS, .15)[0])
        shuffled = sheet(); shuffled["beats"] = [shuffled["beats"][2], shuffled["beats"][0], shuffled["beats"][1], shuffled["beats"][3]]
        self.assertIn("beats must be grouped by chapter in chapter order", beat_errors(shuffled, CHAPTERS, .15))
        thin = sheet(); thin["beats"] = thin["beats"][:3]
        self.assertTrue(any(e.startswith("C02: needs at least two beats") for e in beat_errors(thin, CHAPTERS, .15)))

    def test_word_budgets_come_from_the_measured_rate_and_code_owns_them(self):
        with tempfile.TemporaryDirectory() as folder:
            records = []
            for cid in ("C01", "C02"):
                write(Path(folder) / cid / "evidence-final.json", {"facts": [{"id": "F01", "claim": "x"}]})
                records.append({"chapter": {"id": cid}, "project": str(Path(folder) / cid)})
            plan = {"film": {"title": "T", "advisory_seconds": 900},
                    "chapters": [{"id": "C01", "title": "One", "advisory_seconds": 110}, {"id": "C02", "title": "Two", "advisory_seconds": 120}]}
            self.assertEqual([c["word_budget"] for c in chapter_inputs(plan, records, 130)], [238, 260])
            models = Mock(); models.loop.return_value = {**sheet(), "chapters": [{**c, "word_budget": 9999} for c in sheet()["chapters"]]}
            result = make_beat_sheet(models, plan, records, 130)
            self.assertEqual([c["word_budget"] for c in result["chapters"]], [238, 260])
            self.assertEqual(models.loop.call_args.kwargs["units"], UNITS)

    def test_a_chapter_slice_and_the_script_length_check(self):
        part = chapter_slice({**sheet(), "chapters": [{**c, "word_budget": 20, "length_tolerance": .15} for c in sheet()["chapters"]]}, "C01")
        self.assertEqual([b["id"] for b in part["beats"]], ["B01", "B02"]); self.assertEqual(part["people"], ["Leslie Groves"])
        evidence = {"facts": [{"id": "F01"}], "chapter_context": {"beat_sheet": part}}
        point = lambda words: {"id": "P01", "heading": "Fission", "fact_ids": ["F01"], "narration": " ".join(["word"] * words)}
        self.assertEqual(script_errors({"points": [point(21)]}, evidence), [])
        long = script_errors({"points": [point(30)]}, evidence)
        self.assertEqual(len(long), 1); self.assertIn("Narration totals 30 words; this chapter's budget is 20", long[0])


if __name__ == "__main__":
    unittest.main()
