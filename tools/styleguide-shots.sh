#!/usr/bin/env bash
# Regenerate docs/styleguide/*.png twice: Qt's own style ("qt", as in CI) and the desktop theme.
set -euo pipefail
cd "$(dirname "$0")/.."
out="${1:-docs/styleguide}"
env -u QT_STYLE_OVERRIDE -u QT_QPA_PLATFORMTHEME QT_QPA_PLATFORM=offscreen \
    nix develop -c python tools/styleguide_shots.py "$out" qt
nix develop -c python tools/styleguide_shots.py "$out" desktop
ls ~/.qttest 2>/dev/null && echo "warning: ~/.qttest left behind" >&2 || true
