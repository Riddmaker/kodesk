"""Where KOReader finds an icon by name (frontend/ui/widget/iconwidget.lua:19-74); no Qt.

KOReader looks in the user's ``icons/`` folder in its data directory first (on PocketBook and
Kobo: the KOReader folder itself), then in its own ``resources/icons/mdlight``, then two
fallback folders; ``.svg`` before ``.png``. Icon Pack Changer works by filling ``icons/``.
"""

from __future__ import annotations

from pathlib import Path

DIRS = ("icons", "resources/icons/mdlight", "resources/icons", "resources")
EXTS = (".svg", ".png")
MAX_BYTES = 512 * 1024  # KOReader's icons are a few kB; anything far bigger is not drawn
MENU_TABS = (  # readermenu.lua:29-54, in reader_menu_order.lua's tab order
    "appbar.navigation",
    "appbar.typeset",
    "appbar.settings",
    "appbar.tools",
    "appbar.search",
    "appbar.filebrowser",
    "appbar.menu",
)


def icon_file(koreader_root: Path, name: str) -> Path | None:
    """The file KOReader would draw for icon ``name``, or None (only plain files count)."""
    if "/" in name or name.startswith("."):
        return None
    for folder in DIRS:
        for ext in EXTS:
            path = koreader_root / folder / f"{name}{ext}"
            if path.is_file() and not path.is_symlink():
                return path if path.stat().st_size <= MAX_BYTES else None
    return None


def user_icons(koreader_root: Path) -> bool:
    """Whether the reader has its own icons (a non-empty ``icons/`` folder)."""
    folder = koreader_root / "icons"
    return folder.is_dir() and any(folder.glob("*.svg"))
