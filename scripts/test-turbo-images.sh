#!/usr/bin/env bash
# Replay original film image prompts using the unchanged production repair loop.
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$task_root"
exec "${INFOFILM_PYTHON:-python}" -u -m infofilm.test_turbo_images "$@"
