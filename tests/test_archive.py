"""Safe unpacking of plugin archives on the PC (B-08): zip-slip, links, bombs, layouts."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodesk.core import archive
from kodesk.core.archive import ArchiveError, stage_plugin
from tests.conftest import plugin_files, plugin_zip


@pytest.mark.parametrize(
    ("files", "repo", "name"),
    [
        (plugin_files("clock.koplugin"), "whatever", "clock"),  # release asset layout
        (plugin_files("readingclock.koplugin-1.2.0"), "readingclock.koplugin", "readingclock"),
        (plugin_files("dictsync-main"), "dictsync", "dictsync"),  # repo root is the plugin
        (plugin_files("repo-abc/sub/x.koplugin"), "repo", "x"),
    ],
)
def test_layouts(tmp_path: Path, files: dict[str, bytes], repo: str, name: str) -> None:
    staged = stage_plugin(plugin_zip(files), repo, tmp_path)
    assert staged.name == name
    assert staged.folder == tmp_path / f"{name}.koplugin"
    assert sorted(staged.files) == ["_meta.lua", "lib/util.lua", "main.lua"]
    assert (staged.folder / "lib" / "util.lua").read_bytes() == b"-- util 1.0\n"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"x.koplugin/../../evil.lua": b"x"}, "unsafe name"),
        ({"/etc/evil.lua": b"x"}, "absolute"),
        ({"C:/evil.lua": b"x"}, "absolute"),
        ({"x.koplugin/a\\b.lua": b"x"}, "unsafe name"),
        ({"x.koplugin/con:x.lua": b"x"}, "unsafe name"),
        ({"x.koplugin/Main.lua": b"x"}, "differ only in case"),
    ],
)
def test_hostile_members_are_refused(tmp_path: Path, extra: dict[str, bytes], message: str) -> None:
    data = plugin_zip({**plugin_files("x.koplugin"), **extra})
    with pytest.raises(ArchiveError, match=message):
        stage_plugin(data, "x", tmp_path / "staging")
    assert not (tmp_path / "staging").exists()  # nothing written before the checks pass
    assert not (tmp_path / "evil.lua").exists()


def test_symbolic_links_are_refused(tmp_path: Path) -> None:
    data = plugin_zip(plugin_files("x.koplugin"), links=("x.koplugin/passwd",))
    with pytest.raises(ArchiveError, match="symbolic link"):
        stage_plugin(data, "x", tmp_path)


def test_not_exactly_one_plugin(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError, match="not a KOReader plugin"):
        stage_plugin(plugin_zip({"README.md": b"hi"}), "x", tmp_path / "a")
    two = plugin_zip({**plugin_files("a.koplugin"), **plugin_files("b.koplugin")})
    with pytest.raises(ArchiveError, match="several plugins"):
        stage_plugin(two, "x", tmp_path / "b")
    with pytest.raises(ArchiveError, match="readable zip"):
        stage_plugin(b"not a zip", "x", tmp_path / "c")
    with pytest.raises(ArchiveError, match="unusable plugin folder name"):
        stage_plugin(plugin_zip(plugin_files("top")), "we ird", tmp_path / "d")


def test_zip_bombs_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "MAX_UNPACKED", 1000)
    big = plugin_zip({**plugin_files("x.koplugin"), "x.koplugin/big.bin": b"0" * 2000})
    with pytest.raises(ArchiveError, match="MB unpacked"):
        stage_plugin(big, "x", tmp_path / "a")
    monkeypatch.setattr(archive, "MAX_MEMBERS", 3)
    with pytest.raises(ArchiveError, match="more than 3 files"):
        stage_plugin(plugin_zip(plugin_files("x.koplugin") | {"y": b""}), "x", tmp_path / "b")
