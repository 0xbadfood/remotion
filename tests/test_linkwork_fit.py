import unittest
from pathlib import Path


class LinkworkFitTests(unittest.TestCase):
    def test_a_linkwork_scene_is_fitted_above_the_captions(self):
        # 2026-10-29, Class 10 Magnetic Effects: axis names, the earth plate's label and a coil's readout sat under
        # the captions; Linkwork lays out to 96% down, and the film fits that above a one-line caption (88.5%)
        video = (Path(__file__).parents[1] / "renderer/src/Video.tsx").read_text()
        fit = float(video.split("export const LINKWORK_FIT = ")[1].split(";")[0])
        self.assertLessEqual(0.095 + (0.96 - 0.095) * fit, 0.886)
        self.assertIn("scale(${LINKWORK_FIT})", video)


if __name__ == "__main__":
    unittest.main()
