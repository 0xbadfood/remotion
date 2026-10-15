"""Clip stage: slowed LTX clips from accepted stills, advisory, with the video node switched in and out (2026-10-16)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from infofilm.clips import current, make_clips
from infofilm.core import Store, digest

CONFIG = {"clips": {"enabled": True, "mode_switch": "http://switch"}}


class ClipTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name); self.store = Store(self.root); self.addCleanup(self.store.db.close)
        self.images = {}
        for asset_id in ("A001", "A002"):
            still = self.root / f"{asset_id}.png"; Image.new("RGB", (64, 36), (90, 60, 30)).save(still)
            self.images[asset_id] = {"file": str(still), "sha256": digest(still.read_bytes()), "prompt": "Scene: hills",
                                     "visual_message": "Hills"}

    def run_stage(self, breakage=lambda clip, still, limit: None, render_error=None):
        switches, renders = [], []
        def render(config, still, motion, root, key):
            if render_error:
                raise render_error
            renders.append(key); clip = root / f"{key}.mp4"; clip.write_bytes(b"video " + key.encode()); return clip
        with patch("infofilm.clips.switch_mode", side_effect=lambda url, mode, timeout: switches.append(mode)), \
             patch("infofilm.clips.render_clip", side_effect=render), \
             patch("infofilm.clips.breakage", side_effect=breakage), \
             patch("infofilm.clips.probe_seconds", return_value=10.04), \
             patch("infofilm.motion.make_motion_prompts", return_value={"A001": "Haze drifts.", "A002": "Dust stirs."}) as motion:
            made = make_clips(CONFIG, self.store, None, self.images, {"assets": []})
        return made, switches, renders, motion

    def test_clips_are_made_once_and_the_node_goes_back_to_image_mode(self):
        made, switches, renders, motion = self.run_stage()
        self.assertEqual((set(made), switches, len(renders)), ({"A001", "A002"}, ["video", "image"], 2))
        self.assertEqual(made["A001"]["seconds"], 10.04)
        made, switches, renders, motion = self.run_stage()
        self.assertEqual((set(made), switches, renders), ({"A001", "A002"}, [], []))     # cached: no switch, no render
        motion.assert_not_called()

    def test_broken_or_missing_clips_keep_the_still_and_never_stop_the_film(self):
        made, switches, _, _ = self.run_stage(breakage=lambda clip, still, limit: "blank frame" if "A001" in str(still) else None)
        self.assertEqual(set(made), {"A002"})
        self.assertEqual(switches, ["video", "image"])
        made, switches, _, _ = self.run_stage(render_error=RuntimeError("video node down"))
        self.assertEqual(set(made), {"A002"})                                        # A001 still has no clip
        self.assertEqual(switches, ["video", "image"])                               # switched back even on failure

    def test_a_replaced_image_needs_a_new_clip_and_disabled_means_nothing(self):
        self.run_stage()
        changed = {**self.images, "A001": {**self.images["A001"], "sha256": "new"}}
        self.assertEqual(set(current(self.store, changed)), {"A002"})
        self.assertEqual(make_clips({"clips": {"enabled": False}}, self.store, None, self.images, {}), {})


if __name__ == "__main__":
    unittest.main()
