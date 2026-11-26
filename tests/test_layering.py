"""kodesk.core must stay free of Qt (CLAUDE.md conventions; unit tests run without Qt)."""

from __future__ import annotations

import ast
from pathlib import Path

CORE = Path(__file__).parents[1] / "src" / "kodesk" / "core"


def test_core_never_imports_qt() -> None:
    for path in CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("PySide6", "shiboken6", "kodesk.ui")), (
                    f"{path.name}: {name}"
                )
