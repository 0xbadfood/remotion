#!/usr/bin/env bash
# Add a score to all finished versions of an InfoFilm project, without Remotion.
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo 'Usage: scripts/add-bgm.sh PROJECT_FOLDER_OR_MP4 [acestep|none]' >&2
  exit 2
fi
task_input="$1"
task_engine="${2:-acestep}"
task_python="${INFOFILM_PYTHON:-python}"
task_project="$("$task_python" - "$task_input" <<'PY'
import json
import sys
from pathlib import Path

candidate = Path(sys.argv[1]).expanduser().resolve()
if not candidate.exists():
    raise SystemExit(f'Input does not exist: {candidate}')
if candidate.is_dir() and (candidate / 'delivery.json').is_file():
    print(candidate)
    raise SystemExit(0)
for parent in candidate.parents:
    if (parent / 'delivery.json').is_file():
        print(parent)
        raise SystemExit(0)
    manifest = parent / 'manifest.json'
    if manifest.is_file():
        for output in json.loads(manifest.read_text()).get('outputs', []):
            if Path(output.get('portable_video', '')).resolve() == candidate:
                for original_parent in Path(output['video']).resolve().parents:
                    if (original_parent / 'delivery.json').is_file():
                        print(original_parent)
                        raise SystemExit(0)
raise SystemExit('No completed InfoFilm project found for this input.')
PY
)"
cd -- "$task_root"
exec "$task_python" -m infofilm create --project "$task_project" --resume-from music --music-engine "$task_engine"
