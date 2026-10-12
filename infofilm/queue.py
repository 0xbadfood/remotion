"""Persistent, single-worker queue; pipeline processes survive web restarts."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .core import ROOT, configuration, digest, read, write

STAGES = [("research", "Research"), ("evidence", "Evidence & points"),
          ("creative", "Mood & direction"),
          ("narration", "Script & narration"), ("timing", "Word timing"),
          ("scenes", "Visual beats"), ("images", "Images & banners"),
          ("music", "Score & speech mix"), ("video", "Video & checks")]
LONG_STAGES = [("research", "Subject research"), ("evidence", "Orientation brief"), ("planning", "Chapter plan"),
               ("creative", "Shared direction"), ("chapters", "Chapter productions"),
               ("continuity", "Narrative continuity"), ("assembly", "Join & delivery checks")]


class Queue:
    def __init__(self, directory, config_path, *, process_factory=subprocess.Popen):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_path = Path(config_path).resolve()
        self.process_factory = process_factory
        self.db_path = self.root / "queue.sqlite3"
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.lock = None
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, subject TEXT NOT NULL, request TEXT NOT NULL,
                project TEXT NOT NULL UNIQUE, status TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
                run_id TEXT, pid INTEGER, error TEXT, idempotency_key TEXT UNIQUE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                revision TEXT NOT NULL, expires REAL NOT NULL)""")
        os.chmod(self.db_path, 0o600)

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self):
        self.lock = (self.root / "queue.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another web queue already owns this directory")
        self.import_completed()
        self.thread = threading.Thread(target=self._worker, name="infofilm-queue", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)
        if self.lock:
            self.lock.close()

    def get(self, job_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["request"] = json.loads(result["request"])
        return result

    def list(self):
        with self.connection() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM jobs ORDER BY created DESC")]
        return [self.get(job_id) for job_id in ids]

    def submit(self, request, idempotency_key=None):
        from .llm_settings import Registry
        config = configuration(self.config_path)
        # Planner/reviewer endpoints come from the LLM settings page (or a named configuration chosen for this run).
        config = Registry(self.root, config).freeze(config, request.get("llm_configuration") or None)
        job_id = uuid.uuid4().hex
        slug = re.sub(r"[^a-z0-9]+", "-", request["subject"].lower()).strip("-")[:50] or "film"
        project = ROOT / "artifacts/runs" / f"{slug}-{job_id[:10]}"
        payload = {"subject": request["subject"], "durations": request["durations"],
            "orientation": request["orientation"], "review_mode": request["review_mode"],
            "image_review": request.get('image_review', True),
            "premium_rescue": request.get('premium_rescue', True),
            "generation_mode": request.get("generation_mode", "short"),
            "production_target": request.get("production_target", "video"),
            "subject_graph": request.get("subject_graph", False),
            "animation_chapters": request.get('animation_chapters', False),
            "start_year": None, "end_year": None,
            "tts_engine": request.get("tts_engine", "vibevoice"),
            "omni_speed": request.get("omni_speed", .8), "mood": request.get("mood", "auto"),
            "mood_intensity": request.get("mood_intensity", "restrained"),
            "visual_balance": request.get("visual_balance", "auto"), "music_engine": request.get("music_engine", "none"),
            "project": str(project), "config": str(project / "config-snapshot.json"),
            "reference_audio": request["reference_audio"], "language": "en_indian",
            "reference_transcript": request.get("reference_transcript"),
            "voice_id": request.get("voice_id", "default"), "narrator_label": request.get("narrator_label"),
            "sources": None, "script_dir": None, "visual_plan_dir": None, "image_manifest": None,
            "until": "audio" if request.get("production_target") == "audio" else "video", "use_evidence": True,
            "llm_configuration": config["llm_configuration"]}
        if payload['production_target'] not in {'audio', 'video'}:
            raise ValueError('Unknown production target')
        if type(payload['image_review']) is not bool:
            raise ValueError('image_review must be boolean')
        if type(payload['premium_rescue']) is not bool:
            raise ValueError('premium_rescue must be boolean')
        if not payload['premium_rescue'] and config.get('image_terminal_recovery', {}).get('enabled'):
            # Runs that test other aspects skip the cloud rescue: the best of the local tries is kept and tagged.
            config['image_terminal_recovery'] = {**config['image_terminal_recovery'], 'max_candidates': 0}
        if type(payload['animation_chapters']) is not bool:
            raise ValueError('animation_chapters must be boolean')
        if payload['animation_chapters']:
            if payload['generation_mode'] != 'long':raise ValueError('Animation chapter routing requires long generation')
            if payload['tts_engine'] != 'vibevoice':raise ValueError('Animation chapters currently require VibeVoice')
            from .animation_production import provider_settings
            config['animation_provider'] = provider_settings(config)
        if payload["generation_mode"] == "long":
            from .longform import planner_settings, year_bounds
            payload["start_year"], payload["end_year"] = year_bounds(payload["subject"])
            config["longform_settings"] = planner_settings(config)
            if payload['subject_graph']:
                from .graph_planning import freeze_graph
                config = freeze_graph(config)
        elif payload['subject_graph']:
            raise ValueError('Subject graph planning requires long generation')
        # The model/service configuration is frozen for reproducible resumption.
        # Credentials remain environment variables, outside these snapshots.
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                previous = db.execute("SELECT id,request FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if previous:
                    old = json.loads(previous["request"])
                    defaults = {"omni_speed": .8, "mood": "auto", "mood_intensity": "restrained", "visual_balance": "auto", "music_engine": "none",
                                "generation_mode": "short", "start_year": None, "end_year": None, "subject_graph": False,
                                "production_target": "video", "image_review": True, "premium_rescue": True, "animation_chapters": False}
                    if any(old.get(k, defaults.get(k)) != payload.get(k) for k in ["subject", "durations", "orientation", "review_mode", "reference_audio", "reference_transcript", "tts_engine", *defaults]):
                        raise ValueError("This submission key was already used for different settings")
                    return self.get(previous["id"])
            write(project / "config-snapshot.json", config)
            write(project / "web-request.json", payload)
            db.execute("INSERT INTO jobs(id,subject,request,project,status,created,updated,idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, payload["subject"], json.dumps(payload), str(project), "queued", now, now, idempotency_key))
        self.wake.set()
        return self.get(job_id)

    def resume(self, job_id, *, image_review=None):
        if image_review is not None and type(image_review) is not bool:
            raise ValueError('image_review must be boolean')
        job = self.get(job_id)
        if job['status'] == 'waiting_for_assets':
            from .animation_production import asset_waits
            waits = asset_waits(job['project'])
            if not waits or any(w['kind']=='assets' and not w['ready'] for w in waits):
                raise ValueError('Requested animation assets are still missing or do not satisfy their capabilities. Fill the cache before resuming.')
        resumable = "('failed','interrupted','waiting_for_assets','waiting_for_capabilities')"
        with self.connection() as db:
            if image_review is None:
                changed = db.execute("UPDATE jobs SET status='queued',error=NULL,updated=? WHERE id=? AND status IN " + resumable,
                                     (time.time(), job_id)).rowcount
            else:
                row = db.execute('SELECT request FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row is None:
                    raise KeyError(job_id)
                payload = json.loads(row[0])
                payload['image_review'] = image_review
                changed = db.execute("UPDATE jobs SET request=?,status='queued',error=NULL,updated=? WHERE id=? AND status IN " + resumable,
                                     (json.dumps(payload), time.time(), job_id)).rowcount
        if not changed:
            raise ValueError("Only failed, interrupted or waiting runs can be resumed")
        self.wake.set()
        return self.get(job_id)

    def revise_images(self, job_id, chapters=None):
        from .image_revision import revise_images
        return revise_images(self, job_id, chapters)

    def continue_video(self, job_id):
        from .audio_delivery import delivery_checkpoint
        job = self.get(job_id)
        if job['status'] != 'succeeded' or job['request'].get('production_target') != 'audio':
            raise ValueError('Only a completed audio-first run can continue to video')
        try:
            delivery_checkpoint(job['project'], long=job['request'].get('generation_mode') == 'long')
        except (OSError, KeyError) as exc:
            raise ValueError(f'Audio continuation checkpoint is incomplete: {exc}') from exc
        payload = {**job['request'], 'until': 'video', 'production_target': 'video', 'resume_from': 'scenes'}
        with self.connection() as db:
            changed = db.execute("UPDATE jobs SET request=?,status='queued',error=NULL,updated=? WHERE id=? AND status='succeeded' AND request=?",
                (json.dumps(payload), time.time(), job_id, json.dumps(job['request']))).rowcount
        if not changed:
            raise ValueError('This run already has an active continuation')
        write(Path(job['project']) / 'web-request.json', payload)
        self.wake.set()
        return self.get(job_id)

    def revise_music(self, job_id, engine="acestep"):
        from .resume import music_checkpoint
        job = self.get(job_id)
        if job["status"] != "succeeded":
            raise ValueError("Only completed runs can start a background-score revision")
        if engine not in {"none", "acestep"}:
            raise ValueError("Unknown background score engine")
        delivery, _, _ = music_checkpoint(job["project"])
        project = Path(job["project"])
        snapshot = project / "config-snapshot.json"
        config_path = snapshot if snapshot.exists() else self.config_path
        config = configuration(config_path)
        if engine == "acestep" and (not Path(config.get("acestep_python", "")).is_file() or not (
            Path(config.get("acestep_root", "")) / "checkpoints" / config.get("acestep_model", "acestep-v15-turbo") / "config.json").is_file()):
            raise ValueError("ACE-Step runtime or model is not installed")
        payload = {**job["request"], "resume_from": "music", "music_engine": engine,
                   "project": str(project), "config": str(config_path)}
        with self.connection() as db:
            changed = db.execute("UPDATE jobs SET request=?,status='queued',error=NULL,updated=? WHERE id=? AND status='succeeded'",
                (json.dumps(payload), time.time(), job_id)).rowcount
        if not changed:
            raise ValueError("This run already has an active revision")
        self.wake.set()
        return self.get(job_id)

    def revise_narration(self, job_id):
        from .resume import narration_checkpoint
        job = self.get(job_id)
        if job["status"] != "succeeded":
            raise ValueError("Only completed runs can start a narration repair")
        narration_checkpoint(job["project"])
        payload = {**job["request"], "resume_from": "narration"}
        with self.connection() as db:
            changed = db.execute("UPDATE jobs SET request=?,status='queued',error=NULL,updated=? WHERE id=? AND status='succeeded'",
                                 (json.dumps(payload), time.time(), job_id)).rowcount
        if not changed:
            raise ValueError("This run already has an active revision")
        self.wake.set()
        return self.get(job_id)

    def import_completed(self):
        # Expose completed CLI films and the accepted pilot without rerendering.
        for path in (ROOT / "artifacts").glob("**/delivery.json"):
            if "renderer-snapshot" in path.parts:
                continue
            try:
                delivery = read(path)
                if delivery.get("generation_mode") == "chapter":
                    continue
                project = path.parent.resolve()
                if not (ROOT / "output" / project.name / "index.html").exists():
                    continue
                job_id = "saved-" + digest(str(project))[:20]
                outputs = delivery["outputs"]
                params = {"subject": delivery["subject"], "durations": sorted({o["requested_seconds"] for o in outputs}),
                    "generation_mode": delivery.get("generation_mode", "short"),
                    'animation_chapters': delivery.get('animation_chapters',False),
                    "orientation": "both" if len({o["orientation"] for o in outputs}) > 1 else outputs[0]["orientation"],
                    "review_mode": delivery.get("review_mode", "off"), "tts_engine": delivery.get("tts_engine", "vibevoice"),
                    "mood": delivery.get("mood", "auto"), "music_engine": delivery.get("music_engine", "none")}
                now = path.stat().st_mtime
                with self.connection() as db:
                    db.execute("INSERT OR IGNORE INTO jobs(id,subject,request,project,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                        (job_id, delivery["subject"], json.dumps(params), str(project), "succeeded", now, now))
            except (KeyError, ValueError, OSError):
                continue

    def _update(self, job_id, **values):
        values["updated"] = time.time()
        columns = list(values)
        with self.connection() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in columns) + " WHERE id=?",
                       [values[k] for k in columns] + [job_id])

    def _result_path(self, job):
        return Path(job["project"]) / "interface-runs" / job["run_id"] / "result.json"

    def _alive(self, job):
        if not job.get("pid") or not job.get("run_id"):
            return False
        try:
            command = Path(f"/proc/{job['pid']}/cmdline").read_bytes().split(b"\0")
            return b"infofilm.worker" in command and str(self._result_path(job)).encode() in command
        except OSError:
            return False

    def _observe(self, job):
        if not job.get("run_id"):
            self._update(job["id"], status="interrupted", pid=None, error="Worker launch metadata is missing. Resume the saved run.")
            return False
        result_path = self._result_path(job)
        if result_path.exists():
            try:
                result = read(result_path)
                if not isinstance(result, dict) or result.get("status") not in {"failed", "succeeded", "waiting_for_assets", "waiting_for_capabilities"}:
                    raise ValueError("Invalid worker outcome")
            except (ValueError, OSError) as exc:
                if self._alive(job):
                    return True
                self._update(job["id"], status="failed", pid=None, error=f"Worker outcome could not be read: {exc}")
                return False
            self._update(job["id"], status=result["status"], error=result.get("error"), pid=None)
            return False
        if not self._alive(job):
            self._update(job["id"], status="interrupted", pid=None,
                error="The worker stopped before recording its outcome. Resume to reuse saved stages and provider jobs.")
            return False
        return True

    def _launch(self, job):
        run_id = uuid.uuid4().hex
        project = Path(job["project"])
        result = project / "interface-runs" / run_id / "result.json"
        result.parent.mkdir(parents=True, exist_ok=True)
        request_path = write(result.parent / "request.json", job["request"])
        # Save launch intent before spawning. On a rare server crash between
        # spawn and PID recording, discover the exact command on restart.
        self._update(job["id"], status="running", run_id=run_id, pid=None, error=None, attempt=job["attempt"] + 1)
        with (result.parent / "worker.log").open("ab", buffering=0) as log:
            process = self.process_factory([sys.executable, "-u", "-m", "infofilm.worker",
                "--request", str(request_path), "--result", str(result)],
                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self._update(job["id"], pid=process.pid)
        return process

    def _discover(self, job):
        if job.get("pid") or not job.get("run_id"):
            return job
        for process in Path("/proc").iterdir():
            if process.name.isdigit():
                probe = {**job, "pid": int(process.name)}
                if self._alive(probe):
                    self._update(job["id"], pid=probe["pid"])
                    return self.get(job["id"])
        return job

    def _tick(self, processes):
        active = [self._discover(j) for j in self.list() if j["status"] == "running"]
        busy = any([self._observe(j) for j in active])
        for job_id, process in list(processes.items()):
            if process.poll() is not None:
                del processes[job_id]
        if not busy:
            queued = sorted((j for j in self.list() if j["status"] == "queued"), key=lambda j: j["created"])
            for job in queued:
                try:
                    processes[job["id"]] = self._launch(job)
                    break
                except Exception as exc:
                    self._update(job["id"], status="failed", pid=None, error=f"Could not start worker: {exc}")

    def _worker(self):
        processes = {}
        while not self.stop.is_set():
            try:
                self._tick(processes)
            except Exception:
                import traceback
                traceback.print_exc()
            self.wake.wait(2)
            self.wake.clear()

    def events(self, job):
        path = Path(job["project"]) / "jobs.sqlite3"
        if not path.exists():
            return []
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3) as db:
                rows = db.execute("SELECT id,time,stage,state,detail FROM events WHERE stage LIKE 'pipeline:%' OR id IN (SELECT id FROM events ORDER BY id DESC LIMIT 120) ORDER BY id").fetchall()
            return [{"id":r[0], "time":r[1], "stage":r[2], "state":r[3], "detail":json.loads(r[4])} for r in reversed(rows)]
        except sqlite3.Error:
            return []

    def public(self, job_id, *, details=False):
        job = self.get(job_id)
        project = Path(job["project"])
        events = self.events(job)
        states = {e["stage"].split(":", 1)[1]:e["state"] for e in reversed(events) if e["stage"].startswith("pipeline:")}
        result = {k: job[k] for k in ["id", "subject", "status", "created", "updated", "attempt", "error"]}
        long = job["request"].get("generation_mode") == "long"
        audio_first = job['request'].get('production_target') == 'audio'
        result["settings"] = {k:job["request"].get(k) for k in ["durations", "orientation", "review_mode", "tts_engine", "mood", "mood_intensity", "visual_balance", "music_engine", "omni_speed", "generation_mode", "start_year", "end_year"]}
        result['settings']['subject_graph'] = job['request'].get('subject_graph', False)
        result['settings']['production_target'] = job['request'].get('production_target', 'video')
        result['settings']['llm_configuration'] = job['request'].get('llm_configuration')
        result['settings']['image_review'] = job['request'].get('image_review', True)
        result['settings']['premium_rescue'] = job['request'].get('premium_rescue', True)
        result['settings']['animation_chapters'] = job['request'].get('animation_chapters', False)
        stages = LONG_STAGES if long else STAGES
        if audio_first:
            stages = ([(key, 'Chapter narrations' if key == 'chapters' else 'Score & audio delivery' if key == 'assembly' else label)
                       for key, label in LONG_STAGES] if long else
                      [(key, 'Score & audio delivery' if key == 'music' else label) for key, label in STAGES
                       if key not in {'scenes', 'images', 'video'}])
        if long and result['settings']['subject_graph']:
            stages = [('classification', 'Subject classification'), *stages]
        result["stages"] = [{"id":key, "label":label,
            "state": ("skipped" if key == "music" and not audio_first and job["request"].get("music_engine", "none") == "none" else
                      "pending" if job['status'] == 'queued' and job['request'].get('resume_from') == 'scenes' and key in {'scenes', 'images', 'music', 'video', 'chapters', 'continuity', 'assembly'} else
                      "pending" if job["status"] == "queued" and job["request"].get("resume_from") == "music" and key in {"music", "video"} else
                      "pending" if job["status"] == "queued" and job["request"].get("resume_from") == "narration" and key in {"narration", "timing", "scenes", "music", "video"} else
                      "succeeded" if job["status"] == "succeeded" else states.get(key, "pending"))} for key,label in stages]
        result["can_revise_music"] = (not long and job["status"] == "succeeded" and (project / "delivery.json").is_file()
                                      and (project / "creative-brief.json").is_file())
        result["can_revise_narration"] = result["can_revise_music"] and (project / "image_manifest.json").is_file()
        result['can_continue_video'] = audio_first and job['status'] == 'succeeded' and (project / 'audio-delivery.json').is_file()
        if (project / "creative-brief.json").exists():
            result["creative_brief"] = read(project / "creative-brief.json")
        result["latest_event"] = events[0] if events else None
        result["review_url"] = f"/media/{job_id}/review/index.html" if job["status"] == "succeeded" else None
        if audio_first:
            result['review_url'] = f'/media/{job_id}/listen/index.html' if (project / 'audio-delivery.json').is_file() else None
        if details:
            result["chapter_plan_url"] = f"/media/{job_id}/plan/chapter-plan.json" if (project / "chapter-plan.json").is_file() else None
            from .animation_production import asset_waits
            result['asset_waits'] = asset_waits(project)
            result['can_resume_assets'] = bool(result['asset_waits']) and all(w['ready'] for w in result['asset_waits'])
            result["chapters"] = []
            if long and (project / "longform-status.json").is_file():
                from .longform import child_stage
                value = read(project / "longform-status.json")
                for row in value["chapters"]:
                    chapter = row["chapter"]
                    state = row.get("status", "pending")
                    item = {"id": chapter["id"], "title": chapter["title"], "scope": chapter["scope"],
                            "status": state, "error": row.get("error"), "advisory_seconds": row["duration"],
                            "mood": chapter["mood"]["preset"], **(child_stage(row["project"]) if state == "running" else {})}
                    # Keep the chapter's overall state distinct from its current pipeline stage.
                    item["status"] = state
                    from .external_planner import production_kind
                    item['production_kind'] = production_kind(chapter)
                    child = Path(row["project"])
                    if item['production_kind']=='animation' and (child/'animation/public-report.json').is_file():
                        item['animation_report_url'] = f"/media/{job_id}/chapter/{chapter['id']}/animation/report.html"
                        if (child/'animation/object-requests.json').is_file():
                            item['asset_requests_url'] = f"/media/{job_id}/chapter/{chapter['id']}/animation/object-requests.json"
                    if (child / f"film-{row['duration']}/audio-selection.json").is_file():
                        item["audio_url"] = f"/media/{job_id}/chapter/{chapter['id']}/audio.wav"
                        item["script_url"] = f"/media/{job_id}/chapter/{chapter['id']}/script.txt"
                    if state == "succeeded" and (child / 'delivery.json').is_file():
                        item["review_url"] = (item.get('animation_report_url') if item['production_kind']=='animation' else
                                              f"/media/{job_id}/chapter/{chapter['id']}/review/index.html")
                    result["chapters"].append(item)
                result["chapter_progress"] = {k:value[k] for k in ["completed", "failed", "total"]}
            result["events"] = events[:40]
            result["narrations"] = []
            for duration in job["request"].get("durations", []):
                selected = project / f"film-{duration}/audio-selection.json"
                if selected.exists():
                    value = read(selected)
                    result["narrations"].append({"requested_seconds": duration, "actual_seconds": value["actual_seconds"],
                        "point_count": value["point_count"], "script": value["script"],
                        "audio_url": f"/media/{job_id}/audio/{duration}.wav",
                        "script_url": f"/media/{job_id}/script/{duration}.txt"})
            result["music"] = []
            for duration in job["request"].get("durations", []):
                path = project / f"film-{duration}/music-selection.json"
                if path.exists():
                    value = read(path)
                    if value.get("engine") == "acestep":
                        result["music"].append({"requested_seconds": duration,
                            "score_url": f"/media/{job_id}/score/{duration}.wav", "master_url": f"/media/{job_id}/master/{duration}.wav"})
            result["outputs"] = []
            result['audio_deliveries'] = []
            if (project / 'audio-delivery.json').is_file():
                for row in read(project / 'audio-delivery.json')['outputs']:
                    result['audio_deliveries'].append({k: row[k] for k in ['requested_seconds', 'actual_seconds', 'point_count', 'music_engine']} |
                        {'master_url': f"/media/{job_id}/listen/{Path(row['master_audio']).name}",
                         'mp3_url': f"/media/{job_id}/listen/{Path(row['mp3']).name}",
                         'script_url': f"/media/{job_id}/listen/{Path(row['script_file']).name}"})
            portable = ROOT / "output" / project.name
            if job["status"] == "succeeded" and (portable / "manifest.json").exists():
                for row in read(portable / "manifest.json")["outputs"]:
                    name = Path(row["portable_video"]).name
                    prefix = re.sub(r"[^a-z0-9]+", "-", project.name.lower()).strip("-") or "infofilm"
                    poster = f"{prefix}-{row['requested_seconds']}s-{row['orientation']}/poster.png"
                    result["outputs"].append({k:row[k] for k in ["requested_seconds", "actual_seconds", "point_count", "orientation", "media_qa_passed", "layout_passed"]} |
                        {"visual_metrics": row.get("visual_metrics"), "music_engine": row.get("music_engine", "none")} |
                        {"video_url": f"/media/{job_id}/review/{name}", "poster_url": f"/media/{job_id}/review/{poster}"})
            if job.get("run_id"):
                log = project / "interface-runs" / job["run_id"] / "worker.log"
                if log.exists():
                    with log.open("rb") as handle:
                        handle.seek(max(0, log.stat().st_size - 16000))
                        result["log"] = handle.read().decode(errors="replace")
        return result
