#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$task_root"
benchmark_python="${INFOFILM_PYTHON:-/home/nitin/.pyenv/versions/3.11.9/bin/python}"
benchmark_log="artifacts/image-ladder-experiment/ladder.log"
mkdir -p artifacts/image-ladder-experiment
exec > >(tee -a "$benchmark_log") 2>&1
date -Is
echo "Ladder-only experiment; current production baseline will not be rerun."
echo "Waiting for any already-submitted image request to finish before using its provider."
"$benchmark_python" -u - <<'PY'
import time
import httpx
from infofilm.core import configuration
config = configuration()
deadline = time.monotonic() + 1800
while True:
    image = httpx.get(config['image_url'] + '/api/status', timeout=15)
    image.raise_for_status()
    state = image.json()
    health = httpx.get(config['tts_url'] + '/health', timeout=15)
    health.raise_for_status()
    voice = health.json()
    busy = state.get('busy') or state.get('state') in {'loading', 'unloading'} or voice.get('active_job') or voice.get('queued_or_running_jobs')
    if not busy:
        print('[preflight] Providers are idle; starting the ladder.', flush=True)
        break
    print('[preflight] Waiting for provider work to finish:', {'image_state': state.get('state'), 'image_busy': state.get('busy'), 'voice_busy': voice.get('active_job')}, flush=True)
    if time.monotonic() >= deadline:
        raise RuntimeError('Providers did not become idle; no experiment generation was submitted')
    time.sleep(10)
PY
"$benchmark_python" -u scripts/experiment-images-ladder.py --execute
echo "Ladder run finished. Outcomes and timings: artifacts/image-ladder-experiment/ladder/summary.json"
echo "The baseline script remains prepared for a future matched rerun."
date -Is
