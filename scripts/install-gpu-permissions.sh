#!/usr/bin/env bash
set -euo pipefail
remotion_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Polkit watches this directory; neither provider nor web app needs a restart.
sudo -n install -o root -g root -m 0644 \
  "$remotion_root/deploy/50-remotion-gpu-services.rules" \
  /etc/polkit-1/rules.d/50-remotion-gpu-services.rules
echo 'Installed start/stop authorization for nitin and vibevoice-api-local.service.'
