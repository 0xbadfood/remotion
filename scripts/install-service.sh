#!/usr/bin/env bash
set -euo pipefail
umask 077
remotion_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
remotion_python="${REMOTION_PYTHON:-/home/nitin/.pyenv/versions/3.11.9/bin/python}"
cd "$remotion_root"
if [[ ! -f artifacts/interface/auth.json ]]; then
  echo 'Configure the login first: scripts/change-password.sh' >&2
  exit 1
fi
# Persist only provider credentials already present in this environment.
# Neither the login password nor a password salt is written to this file.
"$remotion_python" - <<'PY'
import json
import os
from pathlib import Path
path = Path('.service.env')
if not path.exists():
    values = {key:os.environ[key] for key in ['TAVILY_API_KEY', 'VLLM_API_KEY'] if os.environ.get(key)}
    if not values.get('TAVILY_API_KEY'):
        raise SystemExit('TAVILY_API_KEY must be set when installing the service, or supplied in .service.env.')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        for key,value in values.items():
            if '\n' in value or '\r' in value:
                raise SystemExit('Provider credentials must be single-line values.')
            handle.write(key + '=' + json.dumps(value) + '\n')
os.chmod(path, 0o600)
PY
sudo install -m 0644 deploy/remotion-video.service /etc/systemd/system/remotion-video.service
bash scripts/install-gpu-permissions.sh
sudo systemctl daemon-reload
sudo systemctl enable --now remotion-video.service
sudo systemctl --no-pager --full status remotion-video.service
