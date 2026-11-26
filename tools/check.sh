#!/usr/bin/env bash
# Everything CI runs, in CI's order. Run inside the dev shell: `nix develop -c tools/check.sh`.
set -euo pipefail
cd "$(dirname "$0")/.."
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

ruff format --check src tests tools
ruff check src tests tools
mypy src tests tools
pylint src tests tools --score=n
python -m pytest -q "$@"
