#!/usr/bin/env bash
set -euo pipefail
planner_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$planner_root"
exec "${INFOFILM_PYTHON:-python}" -u -m infofilm.external_planner "$@"
