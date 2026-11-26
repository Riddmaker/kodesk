#!/usr/bin/env bash
# Everything CI runs, in CI's order. Run inside the dev shell: `nix develop -c tools/check.sh`.
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v ruff >/dev/null; then  # outside the dev shell: re-run inside it
    exec nix develop -c tools/check.sh "$@"
fi
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

ruff format --check src tests tools
ruff check src tests tools
mypy src tests tools
pylint src tests tools --score=n
python -m pytest -q "$@"
