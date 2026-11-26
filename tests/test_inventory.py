"""Plugin and patch inventory on fake reader trees (read-only)."""

from __future__ import annotations

from pathlib import Path

from kodesk.core.inventory import (
    Origin,
    Patch,
    Plugin,
    built_in_plugins,
    list_patches,
    list_plugins,
)
from kodesk.core.luadump import SettingsDocument

INDEX = """../applications/koreader.app
../applications/koreader/README.md
../applications/koreader/plugins/hotkeys.koplugin/main.lua
../applications/koreader/plugins/hotkeys.koplugin/_meta.lua
../applications/koreader/plugins/statistics.koplugin/main.lua
../applications/koreader/update_once.marker
"""


def _koreader(tmp_path: Path, plugins: tuple[str, ...], index: str | None = INDEX) -> Path:
    koreader = tmp_path / "koreader"
    for name in plugins:
        (koreader / "plugins" / f"{name}.koplugin").mkdir(parents=True)
    (koreader / "plugins" / "README.txt").parent.mkdir(parents=True, exist_ok=True)
    (koreader / "plugins" / "README.txt").write_text("not a plugin")
    if index is not None:
        (koreader / "ota").mkdir()
        (koreader / "ota" / "package.index").write_text(index)
    return koreader


def _settings(text: str = "return {}\n") -> SettingsDocument:
    return SettingsDocument.parse(text)


def test_built_in_names_come_from_the_package_index(tmp_path: Path) -> None:
    koreader = _koreader(tmp_path, ())
    assert built_in_plugins(koreader) == frozenset({"hotkeys", "statistics"})


def test_third_party_plugins_are_the_ones_not_shipped(tmp_path: Path) -> None:
    koreader = _koreader(tmp_path, ("hotkeys", "statistics", "projecttitle", "SSH"))
    assert list_plugins(koreader, _settings()) == (
        Plugin("hotkeys", Origin.BUILT_IN, enabled=True),
        Plugin("projecttitle", Origin.THIRD_PARTY, enabled=True),
        Plugin("SSH", Origin.THIRD_PARTY, enabled=True),
        Plugin("statistics", Origin.BUILT_IN, enabled=True),
    )


def test_origin_unknown_without_index(tmp_path: Path) -> None:
    koreader = _koreader(tmp_path, ("projecttitle",), index=None)
    assert list_plugins(koreader, _settings())[0].origin is Origin.UNKNOWN


def test_disabled_plugins(tmp_path: Path) -> None:
    koreader = _koreader(tmp_path, ("hotkeys", "statistics", "projecttitle"))
    settings = _settings(
        'return {\n    ["plugins_disabled"] = {\n        ["statistics"] = true,\n    },\n}\n'
    )
    enabled = {p.name: p.enabled for p in list_plugins(koreader, settings)}
    assert enabled == {"hotkeys": True, "statistics": False, "projecttitle": True}


def test_disable_external_turns_off_third_party_only(tmp_path: Path) -> None:
    koreader = _koreader(tmp_path, ("hotkeys", "projecttitle"))
    settings = _settings('return {\n    ["plugins_disable_external"] = true,\n}\n')
    enabled = {p.name: p.enabled for p in list_plugins(koreader, settings)}
    assert enabled == {"hotkeys": True, "projecttitle": False}


def test_oversized_index_is_ignored(tmp_path: Path) -> None:
    koreader = _koreader(tmp_path, ("hotkeys",), index="x" * (4 * 1024 * 1024 + 1))
    assert built_in_plugins(koreader) is None


def test_patches_follow_koreaders_naming_rule(tmp_path: Path) -> None:
    patches = tmp_path / "patches"
    patches.mkdir()
    for name in (
        "2-reader-footer.lua",
        "10-early.lua",
        "0-once.lua",
        "5-reserved.lua",
        "notes.txt",
        "2-readme.md",
        "no-number.lua",
    ):
        (patches / name).write_text("")
    (patches / "2-folder.lua").mkdir()
    assert list_patches(tmp_path) == (
        Patch("0-once.lua", "0", runs=True),
        Patch("10-early.lua", "1", runs=True),
        Patch("2-reader-footer.lua", "2", runs=True),
        Patch("2-readme.md", None, runs=False),
        Patch("5-reserved.lua", "5", runs=False),
        Patch("no-number.lua", None, runs=False),
        Patch("notes.txt", None, runs=False),
    )


def test_patches_switched_off_all_together(tmp_path: Path) -> None:
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "2-reader-footer.lua").write_text("")
    (patches / ".patches_disabled").write_text("")
    assert list_patches(tmp_path) == (Patch("2-reader-footer.lua", "2", runs=False),)


def test_no_patches_folder(tmp_path: Path) -> None:
    assert not list_patches(tmp_path)
