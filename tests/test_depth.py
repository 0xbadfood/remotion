"""Depth maps for the 2.5D camera (animation pass 2, 2026-10-15)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from infofilm.core import Store, digest, read, write
from infofilm.depth import current, depth_maps


class DepthTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name); self.store = Store(self.root); self.addCleanup(self.store.db.close)
        image = self.root / "a.png"; Image.new("RGB", (64, 36), (120, 80, 40)).save(image)
        self.images = {"A001": {"file": str(image), "sha256": digest(image.read_bytes())}}

    def fake_worker(self, calls):
        def run(command, **kwargs):
            calls.append(command)
            request = read(command[-1]); root = Path(request["directory"]); maps = {}
            for asset_id in request["images"]:
                target = root / f"{asset_id}.png"; Image.new("L", (64, 36), 128).save(target)
                maps[asset_id] = {"file": str(target), "sha256": digest(target.read_bytes())}
            write(root / "result.json", {"protocol": request["protocol"], "maps": maps})
        return run

    def test_maps_are_made_once_and_follow_the_image(self):
        calls = []
        with patch("infofilm.depth.subprocess.run", side_effect=self.fake_worker(calls)):
            self.assertEqual(set(depth_maps({}, self.store, self.images)), {"A001"})
            self.assertEqual(set(depth_maps({}, self.store, self.images)), {"A001"})
            self.assertEqual(len(calls), 1)                              # cached by the image's hash
            changed = {"A001": {**self.images["A001"], "sha256": "different"}}
            self.assertEqual(current(self.store, changed), {})           # a replaced image needs a new map
            depth_maps({}, self.store, changed)
            self.assertEqual(len(calls), 2)

    def test_depth_never_stops_a_film_and_can_be_switched_off(self):
        with patch("infofilm.depth.subprocess.run", side_effect=RuntimeError("no GPU")):
            self.assertEqual(depth_maps({}, self.store, self.images), {})
        failed = self.store.db.execute("SELECT COUNT(*) FROM events WHERE stage='depth' AND state='failed'").fetchone()[0]
        self.assertEqual(failed, 1)
        with patch("infofilm.depth.subprocess.run") as run:
            self.assertEqual(depth_maps({"image_depth": False}, self.store, self.images), {})
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
