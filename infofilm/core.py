from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write_bytes(path, data):
    """Durable replace: the bytes reach the disk before the rename, so a power cut leaves the old file or the new one,
    never an empty one (the Great Stink run lost 157 files to zero bytes after a UPS failure, 2026-10-13)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return path


def write(path, value):
    return write_bytes(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())


EMPTY_SWEEP = {".json", ".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".mp4", ".txt", ".tmp"}


def sweep_empty(root):
    """Remove zero-byte data files under a run folder (left by a power cut before durable writes) so the steps that
    own them run again on resume. The job database and its journal files are never touched."""
    removed = []
    for folder, _, files in os.walk(root):
        for name in files:
            path = Path(folder) / name
            if path.suffix.lower() in EMPTY_SWEEP and not name.startswith("jobs.sqlite3"):
                try:
                    if path.stat().st_size == 0:
                        path.unlink()
                        removed.append(str(path))
                except FileNotFoundError:
                    pass
    return removed


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Long films run chapter workers from threads of one parent; events are serialised by a lock.
        self.db = sqlite3.connect(self.root / "jobs.sqlite3", timeout=30, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, stage TEXT, state TEXT, detail TEXT)")
        self.db.commit()

    def event(self, stage, state, detail=None):
        with self.lock:
            self.db.execute("INSERT INTO events(time,stage,state,detail) VALUES(?,?,?,?)", (time.time(), stage, state, json.dumps(detail or {})))
            self.db.commit()
        print(f"[{stage}] {state}: {json.dumps(detail or {}, ensure_ascii=False)}", flush=True)


@contextlib.contextmanager
def gpu_lease(config, service=None):
    """Exclusive use of the local GPU. `shared_gpu_services` lists the services that really run on it (e.g. music
    and Flux once voice and Qwen images are remote); others pass straight through so parallel chapters can queue at
    their remote providers. Without that setting every service takes the lease, as before."""
    shared = config.get("shared_gpu_services")
    if service is not None and shared is not None and service not in shared:
        yield None
        return
    path = Path(config.get("gpu_lock", "/tmp/infofilm-rtx3090.lock"))
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def shares_gpu(config, *services):
    """True when these services run on the same local GPU. Without `shared_gpu_services` everything does (the old
    single-3090 setup); with it, only the services it lists."""
    shared = config.get("shared_gpu_services")
    return shared is None or all(service in shared for service in services)


@contextlib.contextmanager
def resource_slot(name, slots):
    """Cross-process limit for a local resource (e.g. two video renders at once): one lock file per slot."""
    folder = Path(tempfile.gettempdir()) / "infofilm-slots" / name
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        for index in range(max(1, int(slots))):
            handle = (folder / f"slot-{index}").open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            try:
                yield index
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
            return
        time.sleep(1)


def configuration(path=None):
    path = Path(path or ROOT / "config.json")
    config = read(path)
    for key in ["planner_url", "reviewer_url", "tts_url", "image_url", "omni_url"]:
        if key in config:
            config[key] = config[key].rstrip("/")
    # Embed settings in queue snapshots: editing the registry affects new runs,
    # while an already submitted film keeps its original provider policy.
    if config.get("image_provider_settings") and "image_provider_registry" not in config:
        settings = Path(config["image_provider_settings"])
        if not settings.is_absolute():
            settings = path.parent / settings
        from .image_providers import validate_registry
        config["image_provider_registry"] = validate_registry(read(settings))
    if config.get("image_provider_registry"):
        from .image_providers import validate_registry
        validate_registry(config["image_provider_registry"])
        policy_name = config.get("image_provider_policy", config["image_provider_registry"]["default_policy"])
        if policy_name not in config["image_provider_registry"]["policies"]:
            raise ValueError(f"Unknown image provider policy: {policy_name}")
    return config
