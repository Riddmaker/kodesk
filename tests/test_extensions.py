"""kodesk's fixed extension list (B-23) and KOReader's icon lookup: pure logic, no Qt."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from kodesk.core.catalogue import Entry, Kind
from kodesk.core.extensions import (
    EXTENSIONS,
    ISSUE_TEMPLATE,
    Area,
    for_area,
    for_repo,
    gui_support_url,
    installed_for,
    supported,
)
from kodesk.core.icons import MAX_BYTES, icon_file, user_icons

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><rect width="4" height="4"/></svg>'
)


def test_every_area_has_an_extension_and_names_are_unique() -> None:
    assert all(for_area(area) for area in Area)
    plugins = [e.plugin for e in EXTENSIONS]
    assert len(plugins) == len(set(plugins))
    assert all(e.url.startswith("https://github.com/") for e in EXTENSIONS)


def test_installed_for_matches_folder_names_case_insensitively() -> None:
    assert installed_for(Area.HOME_KEY, ()) is None
    found = installed_for(Area.HOME_KEY, ("hotkeys", "PBFixes"))
    assert found is not None and found.plugin == "pbfixes"
    assert installed_for(Area.ICONS, ("pbfixes",)) is None


def test_unknown_plugins_are_never_supported() -> None:
    assert supported("iconschanger") is not None
    assert supported("koassistant") is None


def test_store_entries_match_by_repo_with_or_without_koplugin() -> None:
    assert for_repo("EbanDev", "iconschanger.koplugin") is not None
    assert for_repo("glaucon1984", "pbfixes") is not None
    assert for_repo("joshuacant", "projecttitle") is not None
    assert for_repo("someone-else", "pbfixes.koplugin") is None  # a fork is not checked


def test_icons_folder_comes_first_and_svg_before_png(tmp_path: Path) -> None:
    stock = tmp_path / "resources/icons/mdlight"
    stock.mkdir(parents=True)
    (stock / "appbar.menu.svg").write_bytes(SVG)
    assert icon_file(tmp_path, "appbar.menu") == stock / "appbar.menu.svg"
    assert not user_icons(tmp_path)
    own = tmp_path / "icons"
    own.mkdir()
    (own / "appbar.menu.png").write_bytes(b"png")
    (own / "appbar.menu.svg").write_bytes(SVG)
    assert icon_file(tmp_path, "appbar.menu") == own / "appbar.menu.svg"
    assert user_icons(tmp_path)


def test_odd_icon_files_are_not_drawn(tmp_path: Path) -> None:
    own = tmp_path / "icons"
    own.mkdir()
    (own / "big.svg").write_bytes(b"x" * (MAX_BYTES + 1))
    (own / "link.svg").symlink_to(tmp_path / "elsewhere.svg")
    (tmp_path / "elsewhere.svg").write_bytes(SVG)
    assert icon_file(tmp_path, "big") is None
    assert icon_file(tmp_path, "link") is None
    assert icon_file(tmp_path, "../elsewhere") is None
    assert icon_file(tmp_path, "missing") is None


FORM = Path(__file__).parent.parent / ".github/ISSUE_TEMPLATE/gui-support.yml"


def test_gui_support_url_is_prefilled_without_device_data() -> None:
    plugin = Entry(
        Kind.PLUGIN,
        "p1",
        "readingclock",
        "alice",
        "readingclock.koplugin",
        "https://github.com/alice/readingclock.koplugin",
        "A clock & more",
        branch="main",
        pushed_at="2026-10-01T12:00:00Z",
    )
    url = urlsplit(gui_support_url(plugin))
    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "github.com",
        "/Riddmaker/kodesk/issues/new",
    )
    fields = dict(parse_qsl(url.query))
    assert fields == {
        "template": ISSUE_TEMPLATE,
        "title": "GUI support: readingclock",
        "extension": "readingclock",
        "kind": "plugin",
        "source": "https://github.com/alice/readingclock.koplugin",
        "version": "main branch, last change 2026-10-01",
    }
    patch = Entry(
        Kind.PATCH,
        "x",
        "2-a.lua",
        "bob",
        "patches",
        "https://github.com/bob/x",
        "",
        file_sha="0123456789abcdef",
    )
    assert dict(parse_qsl(urlsplit(gui_support_url(patch)).query))["version"] == (
        "file 0123456789ab, last change unknown"
    )


def test_every_prefilled_field_is_in_the_issue_form() -> None:
    """GitHub ignores a query parameter whose id the form lacks: keep both in step."""
    text = FORM.read_text(encoding="utf-8")
    ids = set(re.findall(r"^\s+id: ([\w-]+)$", text, re.MULTILINE))
    entry = Entry(Kind.PLUGIN, "i", "n", "o", "r", "https://github.com/o/r", "")
    keys = {k for k, _v in parse_qsl(urlsplit(gui_support_url(entry)).query)}
    assert keys - {"template", "title"} <= ids
    assert f"/{ISSUE_TEMPLATE}" in FORM.as_posix()
