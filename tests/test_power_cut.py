"""Great Stink run, 2026-10-13: a UPS failure rebooted the Spark four times mid-run and left 157 zero-byte files
(152 JSON, 4 PNG, 1 WAV); the resume then failed six chapters on empty JSON and changed image checkpoints."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from infofilm.core import digest, read, sweep_empty, write, write_bytes


class PowerCutTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup); self.root = Path(folder.name)

    def test_durable_write_replaces_without_leaving_a_temporary(self):
        path = write(self.root / "a" / "state.json", {"x": 1})
        write(path, {"x": 2})
        self.assertEqual(read(path), {"x": 2})
        write_bytes(self.root / "a" / "take.png", b"png")
        self.assertEqual(sorted(p.name for p in (self.root / "a").iterdir()), ["state.json", "take.png"])

    def test_sweep_removes_empty_data_files_and_keeps_the_job_database(self):
        for name in ["chapters/c1/image_manifest.json", "chapters/c1/take.png", "chapters/c1/narration.wav",
                     "jobs.sqlite3-wal", "chapters/c1/jobs.sqlite3-shm"]:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True); (self.root / name).write_bytes(b"")
        write(self.root / "chapters/c1/plan.json", {"ok": True})
        removed = sweep_empty(self.root)
        self.assertEqual(sorted(Path(p).name for p in removed), ["image_manifest.json", "narration.wav", "take.png"])
        self.assertTrue((self.root / "jobs.sqlite3-wal").exists())
        self.assertTrue((self.root / "chapters/c1/jobs.sqlite3-shm").exists())
        self.assertEqual(read(self.root / "chapters/c1/plan.json"), {"ok": True})

    def test_emptied_image_take_restarts_the_asset_instead_of_failing(self):
        from infofilm.image_recovery import intact_checkpoint
        take = self.root / "takes" / "take-00" / "candidate.png"
        take.parent.mkdir(parents=True); take.write_bytes(b"image")
        case = {"asset_id": "A003", "message_annotation": {"initial_scene": "scene"}}
        state = {"visual_message": "m", "current_scene": "later", "status": "accepted", "repairs": [{}],
                 "takes": [{"candidate": str(take), "sha256": digest(b"image")}]}
        store = Mock()
        self.assertIs(intact_checkpoint(state, self.root, store, case), state)      # intact: kept as saved
        take.write_bytes(b"")                                                       # the power cut empties it
        fresh = intact_checkpoint(state, self.root, store, case)
        self.assertEqual((fresh["takes"], fresh["status"], fresh["current_scene"]), ([], "running", "scene"))
        self.assertFalse((self.root / "takes").exists())                           # old provider records set aside
        self.assertEqual(len(list(self.root.glob("takes-reset-*"))), 1)
        store.event.assert_called_once()


if __name__ == "__main__":
    unittest.main()
