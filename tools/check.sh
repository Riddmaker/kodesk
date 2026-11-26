#!/usr/bin/env bash
# Everything CI runs, in CI's order. Run inside the dev shell: `nix develop -c tools/check.sh`.
# `--fix` first lets ruff format and fix what it can (CI itself never fixes, it only checks).
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v ruff >/dev/null; then  # outside the dev shell: re-run inside it
    exec nix develop -c tools/check.sh "$@"
fi
# Like CI: tests never open windows on the desktop, and use Qt's own style, not the theme.
export QT_QPA_PLATFORM=offscreen
unset QT_STYLE_OVERRIDE QT_QPA_PLATFORMTHEME

if [[ "${1:-}" == "--fix" ]]; then
    shift
    ruff format src tests tools
    ruff check --fix src tests tools
fi
ruff format --check src tests tools
ruff check src tests tools
mypy src tests tools
pylint src tests tools --score=n
python -m pytest -q "$@"
