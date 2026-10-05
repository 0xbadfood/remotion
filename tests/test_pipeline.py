import json
import tempfile
import unittest
from pathlib import Path

from infofilm.core import Store, digest
from infofilm.llm import Models
from infofilm.planning import script_errors, script_text, word_count, make_image_prompts
from infofilm.media import map_source_words
from infofilm.video import point_captions
import re


class ReviewLoopTests(unittest.TestCase):
    def test_structural_failures_do_not_spend_reviewer_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            models = Models({"max_repairs": 2, "planner_url": "p", "reviewer_url": "r"}, store)
            calls = []
            candidates = iter([{"value": 0}, {"value": 1}])
            def fake(role, name, prompt, **kwargs):
                calls.append((role, name, prompt))
                if role == "planner":
                    return next(candidates)
                if "blind-comparison" in name:
                    return {"preferred": "B", "reasons": [], "regressions": []}
                return {"pass": True, "score": 90, "issues": []}
            models.call = fake
            value = models.loop("stage", "task", {}, lambda x: [] if x["value"] == 1 else ["invalid"])
            self.assertEqual(value["value"], 1)
            self.assertEqual(len([c for c in calls if c[0] == "planner"]), 2)
            self.assertEqual(len([c for c in calls if "-review-" in c[1]]), 1)
            comparison = next(c[2] for c in calls if "blind-comparison" in c[1])
            self.assertIn('CODE VALIDATION:', comparison)
            self.assertIn('"invalid"', comparison)

    def test_major_issues_cannot_be_accepted_by_positive_score(self):
        with tempfile.TemporaryDirectory() as directory:
            models = Models({"max_repairs": 0, "planner_url": "p", "reviewer_url": "r"}, Store(directory))
            models.call = lambda role, *args, **kwargs: {"value": 1} if role == "planner" else {
                "pass": True, "score": 99, "issues": [{"severity": "major", "problem": "wrong causal direction"}]}
            with self.assertRaises(RuntimeError):
                models.loop("stage", "task", {}, lambda x: [])
            self.assertFalse(list(Path(directory).glob("planning/**/selected.json")))

    def test_resume_validates_selected_content_and_keeps_provider_calls_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            models = Models({"max_repairs": 0, "planner_url": "p", "reviewer_url": "r"}, Store(directory))
            models.call = lambda role, *args, **kwargs: {"value": 1}
            models.loop("stage", "task", {}, lambda x: [], experimental=False)
            models.call = lambda *args, **kwargs: self.fail("Unexpected provider call during resume")
            self.assertEqual(models.loop("stage", "task", {}, lambda x: [], experimental=False), {"value": 1})
            with self.assertRaises(ValueError):
                models.loop("stage", "task", {}, lambda x: ["changed validator"], experimental=False)


class ScriptContractTests(unittest.TestCase):
    def test_captions_do_not_reveal_next_point_during_current_point(self):
        words=[{'id':str(i),'word':w,'start':i,'end':i+.5} for i,w in enumerate(['Rain','arrives.','The','winds','shift.'])]
        timing={'words':words,'points':[{'word_ids':['0','1']},{'word_ids':['2','3','4']}]}
        captions=point_captions(timing)
        self.assertEqual([c['text'] for c in captions],['Rain arrives.','The winds shift.'])
        self.assertEqual(captions[0]['end'],1.5)
        self.assertEqual(captions[1]['start'],2)

    def test_image_planner_receives_exact_scene_and_adjacent_narration(self):
        captured = {}
        class Planner:
            def loop(self, stage, task, context, validator, **kwargs):
                captured.update(context)
                value = {"style":"editorial", "assets": context["used_assets"],
                         "banners":[{"scene_key":"60:P2", "text":"Monsoon crops"}]}
                self.errors = validator(value)
                return value
        model = Planner()
        points=[{"id":f"P{i}","narration":line,"fact_ids":["F1"]} for i,line in enumerate(
            ['Heat sets the scene.','Rain supports crops.','Water stores refill.'],1)]
        film={"requested_seconds":60,"script":{"points":points},
              "timing":{"points":[{"start":i*5,"end":(i+1)*5} for i in range(3)]},
              "storyboard":{"scenes":[{"point_id":p["id"],"kind":"image" if i==1 else "flow",
                         "asset_id":"A1","heading":"Rain","rationale":"Crop context"} for i,p in enumerate(points)]}}
        library={"style":"editorial","assets":[{"id":"A1","purpose":"Crops","prompt":"A rain-fed field.","acceptance":["Crops visible"],"fact_ids":["F1"]}]}
        result=make_image_prompts(model,library,[film],{"title":"Monsoon","facts":[{"id":"F1"}]},['portrait'])
        context=captured['scene_contexts_by_asset']['A1'][0]
        self.assertEqual(context['narration'],'Rain supports crops.')
        self.assertEqual(context['previous_narration'],'Heat sets the scene.')
        self.assertEqual(context['next_narration'],'Water stores refill.')
        self.assertEqual(captured['orientations'],['portrait'])
        self.assertEqual(model.errors,[])

    def test_mfa_clitics_keep_source_spelling_and_timing(self):
        points = [{"id":"P1", "narration":"India’s rain."}, {"id":"P2", "narration":"It returns."}]
        aligned = [{"id":str(i), "word":word, "start":i*.2, "end":(i+1)*.2}
                   for i,word in enumerate(["india", "'s", "rain", "it", "returns"])]
        tokenize = lambda text: re.findall(r"[a-z]+",text.lower().replace("'", "").replace("’", ""))
        words, boundaries = map_source_words(points, aligned, tokenize)
        self.assertEqual(words[0]["word"], "India’s")
        self.assertEqual(words[0]["end"], .4)
        self.assertAlmostEqual(boundaries[1]["start"], .6)
        aligned[2]["word"] = "wind"
        with self.assertRaises(ValueError):
            map_source_words(points, aligned, tokenize)

    def test_mfa_unknown_numeric_spans_keep_display_text(self):
        points = [{"id": "P1", "narration": "In 376 AD, the 3rd century changed."}]
        aligned = [{"id": str(i), "word": word, "start": i * .2, "end": (i + 1) * .2}
                   for i, word in enumerate(["in", "<unk>", "ad", "the", "<unk>", "century", "changed"])]
        tokenize = lambda value: re.findall(r"[a-z0-9]+", value.lower())
        words, _ = map_source_words(points, aligned, tokenize)
        self.assertEqual([w["word"] for w in words], ["In", "376", "AD,", "the", "3rd", "century", "changed."])
        self.assertEqual(words[1]["mfa_word_ids"], ["1"])
        aligned[4]["word"] = "<unk>"
        points[0]["narration"] = "In 376 AD, the ancient century changed."
        with self.assertRaises(ValueError):
            map_source_words(points, aligned, tokenize)

    def test_short_script_can_have_fewer_points_and_preserves_order(self):
        evidence = {"facts": [{"id": "F1"}]}
        script = {"points": [{"id": "P1", "heading": "First", "narration": "A clear cause leads to an observable effect.", "fact_ids": ["F1"]},
                              {"id": "P2", "heading": "Then", "narration": "The next step explains why this matters today.", "fact_ids": ["F1"]}]}
        self.assertEqual(script_errors(script, evidence, 16), [])
        self.assertEqual(script_text(script).split("\n\n")[0], script["points"][0]["narration"])

    def test_unfetched_evidence_is_rejected(self):
        script = {"points": [{"id": "P1", "heading": "Claim", "narration": "A plausible statement still needs actual source support.", "fact_ids": ["INVENTED"]}]}
        self.assertTrue(script_errors(script, {"facts": [{"id": "F1"}]}, 8))

    def test_duration_is_advisory_and_does_not_reject_natural_narration(self):
        script = {"points": [{"id": "P1", "heading": "Cause and effect",
            "narration": "The main explanation needs enough room to connect its cause, mechanism, evidence, and practical importance clearly.",
            "fact_ids": ["F1"]}]}
        self.assertEqual(script_errors(script, {"facts": [{"id": "F1"}]}, 5), [])

    def test_word_count_handles_indian_names_and_contractions(self):
        self.assertEqual(word_count("India’s rainfall isn't constant. El Niño matters."), 7)


if __name__ == "__main__":
    unittest.main()
