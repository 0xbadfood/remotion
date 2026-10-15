"""Cancelling a film started by mistake (user request, 2026-10-15)."""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infofilm import queue as queue_module
from infofilm.queue import Queue


class CancelTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root = Path(folder.name); (self.root / "config.json").write_text("{}")
        patcher = patch.object(queue_module, "ROOT", self.root); patcher.start(); self.addCleanup(patcher.stop)
        self.queue = Queue(self.root / "artifacts/interface", self.root / "config.json")

    def add(self, ident, status, **extra):
        columns = {"id": ident, "subject": ident, "request": "{}", "project": str(self.root / "artifacts/runs" / ident),
                   "status": status, "created": time.time(), "updated": time.time(), **extra}
        with self.queue.connection() as db:
            db.execute(f"INSERT INTO jobs ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})", list(columns.values()))

    def test_queued_film_never_starts_and_can_be_resumed(self):
        self.add("queued", "queued")
        job = self.queue.cancel("queued")
        self.assertEqual((job["status"], job["error"]), ("interrupted", Queue.CANCELLED))
        self.assertEqual(self.queue.resume("queued")["status"], "queued")       # "make again" carries on

    def test_running_worker_process_group_is_stopped(self):
        result = self.root / "artifacts/runs/running/interface-runs/r1/result.json"
        result.parent.mkdir(parents=True)
        # A stand-in worker whose command line looks like the real one (that is what _alive checks).
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "infofilm.worker", str(result)],
                                  start_new_session=True)
        self.addCleanup(lambda: worker.poll() is None and worker.kill())
        self.add("running", "running", pid=worker.pid, run_id="r1")
        self.assertTrue(self.queue._alive(self.queue.get("running")))
        job = self.queue.cancel("running", grace_seconds=5)
        self.assertEqual((job["status"], job["pid"]), ("interrupted", None))
        self.assertIsNotNone(worker.wait(timeout=10))
        self.queue._tick({})                                      # the scheduler leaves the cancelled run alone
        self.assertEqual(self.queue.get("running")["error"], Queue.CANCELLED)

    def test_finished_film_cannot_be_cancelled(self):
        self.add("done", "succeeded")
        with self.assertRaises(ValueError):
            self.queue.cancel("done")


if __name__ == "__main__":
    unittest.main()
