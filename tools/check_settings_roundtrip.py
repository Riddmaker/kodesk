"""Read-only check: does kodesk's parser/serializer reproduce real KOReader settings files?

Usage: python tools/check_settings_roundtrip.py FILE...
Prints one status line per file — never a value (settings may hold API keys, HABIT 1).
"""

from __future__ import annotations

import sys
from pathlib import Path

from kodesk.core.luadump import LuaParseError, SettingsDocument


def check(path: Path) -> str:
    """Status of one file: parse, byte-identical re-serialisation, canonical layout."""
    raw = path.read_bytes()
    try:
        doc = SettingsDocument.parse(raw)
    except LuaParseError as error:
        return f"PARSE-ERROR ({str(error).split(':', maxsplit=1)[0]})"
    if doc.to_bytes() != raw:
        return "TEXT-MISMATCH"
    if not doc.is_canonical():
        return "parsed, not canonical (edits stay surgical)"
    return f"OK canonical, {len(doc.data)} top-level keys"


def main(argv: list[str]) -> int:
    bad = 0
    for name in argv:
        status = check(Path(name))
        bad += status.startswith(("PARSE", "TEXT"))
        print(f"{status:50} {Path(name).name}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
