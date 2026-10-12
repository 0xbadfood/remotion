"""A detached, checkpointed pipeline process observed by the web queue."""
from __future__ import annotations

import argparse
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from .core import read, write
from .pipeline import create
from .production_state import ProductionPaused


def run(request_path, result_path):
    try:
        value = read(request_path)
        value.setdefault("tts_engine", "vibevoice")
        value.setdefault("reference_transcript", None)
        args = SimpleNamespace(**value)
        for field in ["project", "config", "sources", "reference_audio", "script_dir", "visual_plan_dir", "image_manifest", "chapter_context", "chapter_plan", "orientation_brief"]:
            setattr(args, field, Path(getattr(args, field)) if getattr(args, field, None) else None)
        create(args)
    except ProductionPaused as exc:
        write(result_path, {'status': exc.status, 'error': str(exc), 'details': exc.details, 'finished': time.time()})
        return 0
    except (Exception, SystemExit) as exc:
        traceback.print_exc()
        try:
            from .core import Store
            store = Store(args.project)
            row = store.db.execute("SELECT stage FROM events WHERE stage LIKE 'pipeline:%' ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                store.event(row[0], 'failed', {'error': f'{type(exc).__name__}: {exc}'})
            store.db.close()
        except Exception:
            pass
        write(result_path, {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "finished": time.time()})
        return 1
    write(result_path, {"status": "succeeded", "finished": time.time()})
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.request, args.result))


if __name__ == "__main__":
    main()
