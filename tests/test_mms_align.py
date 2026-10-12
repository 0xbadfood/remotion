import unittest

import torch

from infofilm.mms_align import _characters, _viterbi


class ViterbiTests(unittest.TestCase):
    def emission(self, frames, peaks, classes=6, blank=0):
        """Log-probabilities where `peaks` maps frame -> class with high probability; elsewhere blank."""
        logits = torch.full((frames, classes), -8.0)
        logits[:, blank] = 0.0
        for frame, cls in peaks.items():
            logits[frame] = -8.0
            logits[frame, cls] = 0.0
        return torch.log_softmax(logits, dim=-1)

    def test_path_places_each_token_on_its_peak(self):
        path = _viterbi(self.emission(12, {2: 3, 6: 4, 9: 5}), [3, 4, 5], 0, torch)
        tokens = {frame: state // 2 for frame, state in enumerate(path) if state % 2}
        self.assertEqual(tokens, {2: 0, 6: 1, 9: 2})

    def test_repeated_tokens_need_a_blank_between_them(self):
        path = _viterbi(self.emission(10, {2: 3, 6: 3}), [3, 3], 0, torch)
        self.assertEqual(sorted({state // 2 for state in path if state % 2}), [0, 1])

    def test_too_few_frames_for_the_text_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, 'too short'):
            _viterbi(self.emission(2, {}), [3, 3, 3], 0, torch)

    def test_characters_fold_accents_and_drop_unknown_symbols(self):
        dictionary = {'-': 0, 'a': 1, 'e': 2, "'": 3}
        self.assertEqual(_characters("é-a'", dictionary), [2, 1, 3])


if __name__ == '__main__':
    unittest.main()
