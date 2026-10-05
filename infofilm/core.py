from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)
    return path


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "jobs.sqlite3", timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL, stage TEXT, state TEXT, detail TEXT)")
        self.db.commit()

    def event(self, stage, state, detail=None):
        self.db.execute("INSERT INTO events(time,stage,state,detail) VALUES(?,?,?,?)", (time.time(), stage, state, json.dumps(detail or {})))
        self.db.commit()
        print(f"[{stage}] {state}: {json.dumps(detail or {}, ensure_ascii=False)}", flush=True)


@contextlib.contextmanager
def gpu_lease(config):
    path = Path(config.get("gpu_lock", "/tmp/infofilm-rtx3090.lock"))
    with path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
