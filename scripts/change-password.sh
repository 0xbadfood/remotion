#!/usr/bin/env bash
set -euo pipefail
umask 077
remotion_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
remotion_python="${REMOTION_PYTHON:-/home/nitin/.pyenv/versions/3.11.9/bin/python}"
cd "$remotion_root"
exec "$remotion_python" -m infofilm.auth set-password "$@"
