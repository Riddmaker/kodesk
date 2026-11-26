"""Shared fixtures: fake reader trees in tmp dirs (never the real device, HABIT 7)."""

from __future__ import annotations

import os
import zipfile
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


LISTINGS = Path(__file__).parent / "fixtures" / "listings"
RELEASE = "v2026.07.1"
# Where each archive is unpacked on the reader's USB volume (research: koreader-releases §2).
UNPACK_DIR = {"pocketbook": "", "kobo": ".adds/", "kindle": ""}
KOREADER_DIR = {
    "pocketbook": "applications/koreader",
    "kobo": ".adds/koreader",
    "kindle": "koreader",
}
THIRD_PARTY_PLUGIN = "projecttitle"
USER_PATCH = "2-reader-footer-tweak.lua"


def release_listing(platform: str) -> list[str]:
    """Member names of the real release archive (tools/fixture-listings.py)."""
    path = LISTINGS / f"koreader-{platform}-{RELEASE}.txt"
    return path.read_text(encoding="utf-8").split()


def make_release_zip(target: Path, platform: str, extra: dict[str, bytes] | None = None) -> Path:
    """A fake release archive with the real member names; ``extra`` adds hostile members."""
    with zipfile.ZipFile(target, "w") as archive:
        for name in release_listing(platform):
            archive.writestr(name, _shipped_content(name, platform))
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return target


def _shipped_content(name: str, platform: str) -> bytes:
    if name.endswith("/git-rev"):
        return f"{RELEASE}\n".encode()
    if name.endswith("/ota/package.index"):
        return "".join(f"../{member}\n" for member in release_listing(platform)).encode()
    return f"-- {name}\n".encode()


def make_reader(root: Path, platform: str, settings: dict[object, object] | None = None) -> Path:
    """A reader volume: the real release's files plus typical user state (B-05).

    User state: settings, history, a settings/ file, a style tweak, a third-party plugin, a user
    patch and a book with its .sdr metadata. Returns the KOReader directory.
    """
    for name in release_listing(platform):
        path = root / (UNPACK_DIR[platform] + name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_shipped_content(name, platform))
    koreader = root / KOREADER_DIR[platform]
    data = OWNER_LIKE_SETTINGS if settings is None else settings
    user_files = {
        "settings.reader.lua": dump_file(data, "./settings.reader.lua"),  # type: ignore[arg-type]
        "history.lua": 'return {\n    [1] = {\n        ["file"] = "/mnt/ext1/Books/Alice.epub",\n'
        '        ["time"] = 1764000000,\n    },\n}\n',
        "settings/vocabulary_builder.sqlite3": "fake db",
        "styletweaks/justify.css": "p { text-align: justify; }\n",
        f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/main.lua": "-- third-party\n",
        f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/_meta.lua": "return { name = 'projecttitle' }\n",
        f"patches/{USER_PATCH}": "-- user patch\n",
        "version.log": f"2026-08-01, 10:00:00, {RELEASE}, Fake\n",
    }
    for relative, content in user_files.items():
        path = koreader / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    sdr = root / "Books" / "Alice.sdr"
    sdr.mkdir(parents=True)
    (root / "Books" / "Alice.epub").write_bytes(b"PK")
    (sdr / "metadata.epub.lua").write_text('return {\n    ["percent_finished"] = 0.42,\n}\n')
    return koreader
