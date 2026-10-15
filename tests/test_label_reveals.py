"""Diagram cards appear when the narration names them (renderer animation pass 1, 2026-10-15)."""
import unittest

from infofilm.video import label_reveals


def said(*pairs):
    return [{"word": word, "start": start} for word, start in pairs]


class LabelRevealTests(unittest.TestCase):
    def test_first_card_at_the_cut_then_each_when_named(self):
        words = said(("boycotts", 2.0), ("tax", 6.0), ("refusal", 6.3), ("forest", 9.0))
        self.assertEqual(label_reveals(["Boycott cloth", "Refuse taxes", "Forest laws"], ["", "", ""], words, 0, 360, 30),
                         [0, 172, 262])          # "taxes" matches "tax"; a quarter second early

    def test_words_only_one_card_has_decide_the_match(self):
        # Salt March scene 19: both cards say "March"; "12" and "start" belong to the second card only.
        words = said(("12", 2.0), ("March", 2.3), ("start", 4.3), ("11", 8.5), ("March", 8.8), ("deadline", 9.2))
        reveals = label_reveals(["11 March deadline", "12 March start", "6 April arrival"], ["", "", ""], words, 0, 311, 30)
        self.assertEqual(reveals[:2], [0, 52])
        self.assertTrue(52 < reveals[2] <= int(311 * .85))   # never named: between its neighbour and the deadline

    def test_unnamed_cards_spread_between_neighbours_and_all_show_by_85_percent(self):
        words = said(("boycotts", 2.0), ("forest", 9.0))
        self.assertEqual(label_reveals(["Boycott", "Something unsaid", "Forest laws"], ["", "", ""], words, 0, 360, 30),
                         [0, 131, 262])
        self.assertEqual(label_reveals(["A", "B", "C"], ["", "", ""], [], 0, 300, 30), [0, 85, 170])
        late = label_reveals(["A", "Coast"], ["", ""], said(("coast", 11.8)), 0, 360, 30)
        self.assertEqual(late, [0, 306])

    def test_words_outside_the_scene_do_not_count(self):
        words = said(("forest", 1.0), ("forest", 20.0))
        self.assertEqual(label_reveals(["Start", "Forest"], ["", ""], words, 300, 600, 30), [0, 128])   # the 20 s "forest" is after the scene: unnamed


if __name__ == "__main__":
    unittest.main()
