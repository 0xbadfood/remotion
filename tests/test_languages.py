import math
import unittest

from infofilm import languages
from infofilm.video import point_captions

SCRIPT = {"points": [{"id": "P01", "narration": "Many parents ask why. The answer starts here."},
                     {"id": "P02", "narration": "Every baby has a short vessel."}]}
# the English film's timing: P01's words plus a room phrase ("Look carefully.") at its end, then P02
TIMING = {"points": [{"point_id": "P01", "word_ids": [f"w{i:04d}" for i in range(1, 11)]},
                     {"point_id": "P02", "word_ids": [f"w{i:04d}" for i in range(11, 17)]}]}
STORYBOARD = {"scenes": [{"id": "S01", "start_word_id": "w0001", "heading": "Why"},
                         {"id": "S02", "start_word_id": "w0005", "heading": "Answer"},
                         {"id": "S03", "start_word_id": "w0010", "heading": "Look"},
                         {"id": "S04", "start_word_id": "w0011", "heading": "Vessel"}]}


class Anchors(unittest.TestCase):
    def test_marks_follow_the_film_timing_not_word_counts(self):
        marked = languages.marked_points(SCRIPT, STORYBOARD, TIMING)
        self.assertEqual(marked[0]["narration"], "«S01» Many parents ask why. «S02» The answer starts «S03» here.")
        self.assertEqual(marked[1]["narration"], "«S04» Every baby has a short vessel.")

    def test_translation_checks(self):
        english = languages.marked_points(SCRIPT, STORYBOARD, TIMING)
        good = {"points": [{"id": "P01", "narration": "«S01» कई माता-पिता पूछते हैं क्यों। «S02» जवाब यहाँ से शुरू «S03» होता है।"},
                           {"id": "P02", "narration": "«S04» हर बच्चे में एक छोटी vessel होती है।"}],
                "scenes": [{"id": s["id"], "heading": "x"} for s in STORYBOARD["scenes"]]}
        self.assertEqual(languages.translation_problems(good, english, STORYBOARD, "hi"), [])
        bad = {"points": [{"id": "P01", "narration": "कई माता-पिता «S02» पूछते हैं 17 बार। «S01» जवाब यहाँ है।"},
                          {"id": "P02", "narration": "«S04» every baby has a short vessel"}],
               "scenes": [{"id": "S01"}]}
        problems = " ".join(languages.translation_problems(bad, english, STORYBOARD, "hi"))
        for wanted in ("scene marks must be", "starts with a scene mark", "Hindi words, not digits", "mostly Devanagari", "scenes missing"):
            self.assertIn(wanted, problems)


class Hearing(unittest.TestCase):
    TEXT = ("आपके बच्चे के दिल की दो ऊपरी chambers के बीच की दीवार में एक छोटा सा छेद है। आज डॉक्टर इसे एक छोटे से device से "
            "बंद कर सकते हैं, जो पैर की नस से एक पतली tube के ज़रिए दिल तक पहुँचता है, छाती खोले बिना।")

    def test_whisper_spelling_english_words_in_devanagari_still_counts(self):
        heard = ("आपके बच्चे के दिल की दो उपरी चेंबर्स के बीच की दीवार में एक छोटा सा छेद है। आज डॉक्टर इसे एक छोटे से डिवाइस से बंद "
                 "कर सकते हैं जो पैर की नस से एक पतली ट्यूब के जरिये दिल तक पहुँचता है छाती खोले बिला।")
        self.assertGreater(languages.heard_score(self.TEXT, heard), languages.MIN_HEARD)

    def test_a_half_take_or_an_invented_tail_fails(self):
        half = "आपके बच्चे के दिल की दो उपरी चेंबर्स के बीच की दिवार में एक छोटा सा छेद है।"
        tail = self.TEXT + " और फिर हम सब घर चले गए और खाना खाया बहुत अच्छा लगा"
        self.assertLess(languages.heard_score(self.TEXT, half), languages.MIN_HEARD)
        self.assertLess(languages.heard_score(self.TEXT, tail), languages.MIN_HEARD)

    def test_whisper_hears_in_short_windows(self):
        # 2026-11-06, P16 of the Hindi BAV film: a 30 s window ran out of tokens mid-word on every take
        from unittest import mock
        model = mock.Mock()
        model.transcribe.return_value = ([mock.Mock(text=" छेद है। ")], None)
        with mock.patch.object(languages, "_whisper", model), mock.patch.object(languages, "seconds", return_value=21.4):
            self.assertEqual(languages.hear("x.wav", "hi"), "छेद है।")
        self.assertLessEqual(model.transcribe.call_args.kwargs["chunk_length"], 15)

    def test_windows_leave_no_scrap_for_whisper_to_fill(self):
        # 2026-11-06, P18 of the Hindi PDA closure film: 10 s windows left 0.1 s of a 10.1 s take, heard as made-up words
        for length in (4.0, 10.1, 11.3, 21.4, 24.0, 37.0):
            window = languages.hear_window(length)
            self.assertLessEqual(window, 15)
            pieces = math.ceil(length / window)
            self.assertGreater(length - (pieces - 1) * window, window / 2)

    def test_romanize_for_the_aligner(self):
        self.assertEqual(languages.romanize("दिल"), "dil")
        self.assertEqual(languages.romanize("stent"), "stent")
        self.assertEqual(languages.tokens("छेद है। device,"), ["छेद", "है", "device"])


class Captions(unittest.TestCase):
    def test_hindi_full_stop_ends_a_caption(self):
        words = [{"id": "w1", "word": "छेद", "start": 0, "end": .4}, {"id": "w2", "word": "है।", "start": .4, "end": .8},
                 {"id": "w3", "word": "आज", "start": 1, "end": 1.3}]
        caps = point_captions({"words": words, "points": [{"point_id": "P01", "word_ids": ["w1", "w2", "w3"]}]})
        self.assertEqual([c["text"] for c in caps], ["छेद है।", "आज"])


if __name__ == "__main__":
    unittest.main()


class NodeForRenders(unittest.TestCase):
    def test_render_tools_find_node_without_it_on_path(self):
        import os
        from unittest import mock
        from infofilm import video
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            env = video.node_env()
        if (video.NODE_DIR / "node").exists():
            self.assertTrue(env["PATH"].startswith(str(video.NODE_DIR)))
