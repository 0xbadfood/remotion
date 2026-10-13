"""Block until a long-film job fails a chapter, fails a stage or ends; print what happened.
usage: .venv/bin/python scripts/watch_run.py <project dir> <job id> [since epoch]"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from infofilm.core import ROOT  # noqa: E402
from infofilm.queue import Queue  # noqa: E402

project, job_id = sys.argv[1], sys.argv[2]
since = float(sys.argv[3]) if len(sys.argv) > 3 else time.time()
queue = Queue(ROOT / "artifacts/interface", ROOT / "config.json")
clock = lambda t: datetime.fromtimestamp(t).strftime("%H:%M:%S")
while True:
    db = sqlite3.connect(Path(project) / "jobs.sqlite3")
    rows = db.execute("select time, stage, state, detail from events where time > ? order by id", (since,)).fetchall()
    db.close()
    for t, stage, state, detail in rows:
        since = t
        notable = (state in {"chapter_failed", "chapter_succeeded"} or
                   (stage.startswith("pipeline:") and state in {"succeeded", "failed"}))
        if notable:
            d = json.loads(detail or "{}")
            print(clock(t), stage, state, json.dumps({k: d[k] for k in d if k in
                  ("chapter", "error", "people", "referenced", "chapters", "source_floor", "accepted_with_findings")})[:600], flush=True)
    status = queue.get(job_id)["status"]
    if status not in {"running", "queued"}:
        print("JOB STATUS:", status, (queue.get(job_id).get("error") or "")[:400], flush=True)
        break
    if any(r[2] in {"chapter_failed", "failed"} and (r[2] == "chapter_failed" or r[1].startswith("pipeline:")) for r in rows):
        print("(failure seen; job still running)", flush=True)
        break
    time.sleep(20)
