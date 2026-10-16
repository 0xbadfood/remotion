"""Clip stage and video pool: slowed LTX clips from accepted stills across one or more video workers (2026-10-16)."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from infofilm import video_pool
from infofilm.clips import current, make_clips, settings
from infofilm.core import Store, digest

CONFIG = {"clips": {"enabled": True, "workers": [{"id": "amd", "kind": "ltx_comfy", "url": "http://amd"},
                                                 {"id": "spark", "kind": "ltx_storyvault", "url": "http://spark"}]}}


class FakeWorker:
    def __init__(self, wid, delay=0.0, fail_prepare=False, fail_render=False):
        self.id, self.delay, self.fail_prepare, self.fail_render = wid, delay, fail_prepare, fail_render
        self.rendered, self.prepared, self.released = [], False, False

    def warm(self):
        pass

    def prepare(self):
        if self.fail_prepare:
            raise RuntimeError("not ready")
        self.prepared = True

    def release(self):
        self.released = True

    def render(self, still, motion, target, key):
        time.sleep(self.delay)
        if self.fail_render:
            raise RuntimeError(f"{self.id} broke")
        self.rendered.append(key)
        target.write_bytes(b"video " + key.encode())
        return target


class ClipStageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name); self.store = Store(self.root); self.addCleanup(self.store.db.close)
        self.images = {}
        for n in range(1, 7):
            still = self.root / f"A00{n}.png"; Image.new("RGB", (64, 36), (90, 60 + n, 30)).save(still)
            self.images[f"A00{n}"] = {"file": str(still), "sha256": digest(still.read_bytes()), "prompt": "Scene: hills"}

    def run_stage(self, workers):
        motion = {k: "Haze drifts." for k in self.images}
        with patch("infofilm.video_pool.build", return_value=workers), \
             patch("infofilm.clips.breakage", return_value=None), \
             patch("infofilm.clips.probe_seconds", return_value=10.04), \
             patch("infofilm.motion.make_motion_prompts", return_value=motion):
            return make_clips(CONFIG, self.store, None, self.images, {"assets": []})

    def test_two_workers_share_the_scenes_and_both_are_released(self):
        amd, spark = FakeWorker("amd", .02), FakeWorker("spark", .05)
        made = self.run_stage([amd, spark])
        self.assertEqual(set(made), set(self.images))
        self.assertTrue(amd.rendered and spark.rendered)                       # both took scenes
        self.assertEqual(len(amd.rendered) + len(spark.rendered), 6)
        self.assertTrue(amd.released and spark.released)
        self.assertEqual({v["worker"] for v in made.values()}, {"amd", "spark"})
        again = FakeWorker("amd")
        self.assertEqual(set(self.run_stage([again])), set(self.images))     # cached: nothing rendered again
        self.assertEqual(again.rendered, [])

    def test_a_worker_that_cannot_start_takes_nothing_and_is_still_released(self):
        amd, spark = FakeWorker("amd"), FakeWorker("spark", fail_prepare=True)
        self.assertEqual(set(self.run_stage([amd, spark])), set(self.images))
        self.assertEqual((len(amd.rendered), spark.rendered, spark.released), (6, [], True))

    def test_a_failing_worker_hands_its_scenes_to_the_other(self):
        amd, spark = FakeWorker("amd", .03), FakeWorker("spark", fail_render=True)
        self.assertEqual(set(self.run_stage([amd, spark])), set(self.images))
        self.assertEqual(len(amd.rendered), 6)

    def test_no_worker_means_stills_and_the_film_goes_on(self):
        self.assertEqual(self.run_stage([FakeWorker("amd", fail_prepare=True)]), {})
        states = [r[0] for r in self.store.db.execute("SELECT state FROM events WHERE stage='clips'")]
        self.assertIn("worker_unavailable", states)
        self.assertIn("succeeded", states)

    def test_disabled_or_replaced_images(self):
        self.run_stage([FakeWorker("amd")])
        changed = {**self.images, "A001": {**self.images["A001"], "sha256": "new"}}
        self.assertNotIn("A001", current(self.store, changed))
        self.assertEqual(make_clips({"clips": {"enabled": False}}, self.store, None, self.images, {}), {})


class VideoPoolTests(unittest.TestCase):
    def test_single_worker_from_the_old_settings(self):
        configs = video_pool.worker_configs(settings({"clips": {"url": "http://x:8011", "mode_switch": "http://x:8099"}}))
        self.assertEqual(configs, [{"id": "video", "kind": "ltx_comfy", "url": "http://x:8011", "mode_switch": "http://x:8099"}])

    def test_the_comfy_worker_does_not_wait_for_image_mode_on_release(self):
        calls = []
        worker = video_pool.ComfyWorker({"kind": "ltx_comfy", "url": "http://x", "mode_switch": "http://s"}, settings({}))
        with patch("infofilm.video_pool.switch_mode", side_effect=lambda url, mode, timeout, wait=True: calls.append((mode, wait))):
            worker.prepare(); worker.release()
        self.assertEqual(calls, [("video", True), ("image", False)])

    def test_the_storyvault_worker_interpolates_locally(self):
        worker = video_pool.StoryVaultWorker({"kind": "ltx_storyvault", "url": "http://spark"}, settings({}))
        with tempfile.TemporaryDirectory() as d:
            still = Path(d) / "s.png"; Image.new("RGB", (8, 8)).save(still)
            target = Path(d) / "clip.mp4"

            class Client:
                def __init__(self, *a, **k): pass
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def post(self, url, files, data):
                    self.data = data
                    return type("R", (), {"raise_for_status": lambda r: r, "json": lambda r: {"id": "j1", "token": "t"}})()
                def get(self, url, params=None, timeout=None):
                    body = {"status": "succeeded", "download_url": "http://192.168.1.102:8010/v1/videos/j1/content?token=t"}
                    return type("R", (), {"json": lambda r: body, "raise_for_status": lambda r: r, "content": b"raw", "url": url})()
            seen = {}
            with patch("infofilm.video_pool.httpx.Client", Client), \
                 patch("infofilm.rife.interpolate", side_effect=lambda src, dst, m: (seen.update(src=src, m=m), Path(dst).write_bytes(b"x2"))):
                worker.render(str(still), "Haze drifts.", target, "abc123")
            self.assertEqual((target.read_bytes(), seen["m"]), (b"x2", 2))


if __name__ == "__main__":
    unittest.main()
