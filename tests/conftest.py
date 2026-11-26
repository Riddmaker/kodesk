"""Shared fixtures: fake reader trees in tmp dirs (never the real device, HABIT 7)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kodesk.core.luadump import dump_file

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

OWNER_LIKE_SETTINGS = {
    "copt_font_size": 22,
    "copt_h_page_margins": {1: 10, 2: 10},
    "copt_t_page_margin": 10,
    "copt_b_page_margin": 5,
    "copt_line_spacing": 100,
    "copt_status_line": 0,
    "cre_header_status_font_size": 28,
    "cre_font": "Noto Serif",
    "night_mode": True,
    "home_dir": "/mnt/ext1/Books",
    "footer": {"container_height": 14, "container_bottom_padding": 1, "disabled": False},
    "reader_footer_mode": 1,
}


def make_pocketbook(root: Path, settings: dict[object, object] | None = None) -> Path:
    """A minimal PocketBook volume with KOReader v2026.07.2 on a Verse Pro Color."""
    koreader = root / "applications" / "koreader"
    koreader.mkdir(parents=True)
    (koreader / "reader.lua").write_text("-- fake\n")
    (koreader / "git-rev").write_text("v2026.07.2\n")
    (koreader / "version.log").write_text(
        "2026-02-11, 19:37:30, v2025.10, PBVerseProColor\n"
        "2026-08-26, 08:01:22, v2026.07.2, PBVerseProColor\n"
    )
    data = OWNER_LIKE_SETTINGS if settings is None else settings
    (koreader / "settings.reader.lua").write_text(
        dump_file(data, "./settings.reader.lua")  # type: ignore[arg-type]
    )
    books = root / "Books"
    books.mkdir()
    (books / "Alice.epub").write_bytes(b"PK")
    (books / "Alice.sdr").mkdir()
    (books / "Classics").mkdir()
    return root


@pytest.fixture
def pocketbook(tmp_path: Path) -> Path:
    return make_pocketbook(tmp_path / "PB634K3")
