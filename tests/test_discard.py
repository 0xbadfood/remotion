import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infofilm import queue as queue_module
from infofilm.queue import Queue


class DiscardTests(unittest.TestCase):
    def test_superseded_incomplete_run_is_deleted_with_its_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("{}")
            with patch.object(queue_module, "ROOT", root):
                q = Queue(root / "artifacts/interface", root / "config.json")
                old = root / "artifacts/runs/old-film"; (old / "videos").mkdir(parents=True)
                public = root / "renderer/public/abc123"; public.mkdir(parents=True); (public / "x.png").write_bytes(b"x")
                (old / "videos/timeline.json").write_text(json.dumps({"data": {"scenes": [{"src": "abc123/x.png"}]}}))
                (root / "output/old-film").mkdir(parents=True)
                now = time.time()
                with q.connection() as db:
                    for ident, project, status, created in [("old", old, "failed", now - 100), ("new", root / "artifacts/runs/new-film", "running", now)]:
                        db.execute("INSERT INTO jobs (id,subject,request,project,status,created,updated) VALUES (?,?,?,?,?,?,?)",
                                   (ident, ident, "{}", str(project), status, created, created))
                self.assertTrue(q.can_discard(q.get("old")))
                self.assertFalse(q.can_discard(q.get("new")))                 # running, and nothing newer
                result = q.discard("old")
                self.assertFalse(old.exists()); self.assertFalse(public.exists()); self.assertFalse((root / "output/old-film").exists())
                self.assertEqual(len(result["removed"]), 3)
                with self.assertRaises(KeyError):
                    q.get("old")
                with self.assertRaises(ValueError):
                    q.discard("new")

    def test_latest_incomplete_run_cannot_be_discarded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / "config.json").write_text("{}")
            with patch.object(queue_module, "ROOT", root):
                q = Queue(root / "artifacts/interface", root / "config.json")
                with q.connection() as db:
                    db.execute("INSERT INTO jobs (id,subject,request,project,status,created,updated) VALUES (?,?,?,?,?,?,?)",
                               ("only", "x", "{}", str(root / "artifacts/runs/only"), "failed", time.time(), time.time()))
                self.assertFalse(q.can_discard(q.get("only")))                # still resumable: nothing replaced it
