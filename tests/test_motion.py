"""Motion prompts for clips: one call, code checks only, corrected at most twice (2026-10-16)."""
import unittest
from unittest.mock import Mock

from infofilm.motion import make_motion_prompts, motion_errors

SCENES = [{"id": "A001", "visual_message": "Hill towns", "scene": "Hill towns at dawn", "people": []},
          {"id": "A002", "visual_message": "Cavour at his desk", "scene": "Cavour at a desk", "people": ["Cavour"]}]
GOOD = {"clips": [{"id": "A001", "motion": "Soft haze drifts through the valleys while dust stirs. Painterly editorial illustration."},
                  {"id": "A002", "motion": "Cavour breathes slowly and lifts his eyes toward the window. Painterly editorial illustration."}]}


class MotionTests(unittest.TestCase):
    def test_every_scene_once_with_a_short_gentle_prompt(self):
        self.assertEqual(motion_errors(GOOD, {"A001", "A002"}), [])
        self.assertIn("missing scenes: A002", motion_errors({"clips": GOOD["clips"][:1]}, {"A001", "A002"}))
        bad = {"clips": [GOOD["clips"][0], {"id": "A002", "motion": "Cavour writes; a caption appears with the date of the treaty."}]}
        self.assertTrue(any("caption" in e for e in motion_errors(bad, {"A001", "A002"})))
        self.assertTrue(motion_errors({"clips": [{"id": "A001", "motion": "Haze."}]}, {"A001"}))

    def test_one_call_and_a_corrected_answer_when_checks_fail(self):
        models = Mock(); models.call.side_effect = [{"clips": GOOD["clips"][:1]}, GOOD]
        self.assertEqual(set(make_motion_prompts(models, SCENES)), {"A001", "A002"})
        self.assertEqual(models.call.call_count, 2)
        self.assertIn("missing scenes: A002", models.call.call_args.args[2])     # the problems go back to the planner

    def test_gives_up_after_three_answers(self):
        models = Mock(); models.call.return_value = {"clips": []}
        with self.assertRaises(ValueError):
            make_motion_prompts(models, SCENES)
        self.assertEqual(models.call.call_count, 3)


if __name__ == "__main__":
    unittest.main()
