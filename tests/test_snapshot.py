"""Snapshot, verify and restore on fake reader trees (B-04). Never the real reader."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodesk.core import snapshot as snap
from kodesk.core.snapshot import (
    RestorePlan,
    SnapshotError,
    SnapshotStore,
    WriteRejectedError,
    device_id,
    guarded_write,
    is_protected,
)
from tests.conftest import THIRD_PARTY_PLUGIN, USER_PATCH


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "cache" not in p.relative_to(root).parts
    }


def test_snapshot_skips_cache_and_stores_each_content_once(
    reader: Path, store: SnapshotStore
) -> None:
    (reader / "cache").mkdir()
    (reader / "cache" / "big.bin").write_bytes(b"x" * 1000)
    (reader / "settings" / "copy-of-history.lua").write_bytes((reader / "history.lua").read_bytes())
    taken = store.take(reader)
    paths = {e.path for e in taken.files}
    assert "settings.reader.lua" in paths and "history.lua" in paths
    assert not any(p.startswith("cache/") for p in paths)
    blobs = list(store.blobs.glob("*/*"))
    assert len(blobs) == len({e.sha256 for e in taken.files}) < len(taken.files)
    assert store.snapshots() == [taken]


def test_second_snapshot_adds_only_changed_content(reader: Path, store: SnapshotStore) -> None:
    store.take(reader)
    before = len(list(store.blobs.glob("*/*")))
    (reader / "history.lua").write_text("return {}\n")
    store.take(reader)
    assert len(list(store.blobs.glob("*/*"))) == before + 1


def test_keeps_the_newest_five_and_drops_unused_content(reader: Path, store: SnapshotStore) -> None:
    ids = []
    for n in range(7):
        (reader / "history.lua").write_text(f"return {{ {n} }}\n")
        ids.append(store.take(reader).id)
    assert [s.id for s in store.snapshots()] == ids[:1:-1]
    contents = {b.read_bytes() for b in store.blobs.glob("*/*")}
    assert b"return { 0 }\n" not in contents and b"return { 6 }\n" in contents


def test_restore_brings_back_every_byte(reader: Path, store: SnapshotStore) -> None:
    original = _tree(reader)
    taken = store.take(reader)
    (reader / "settings.reader.lua").write_text("return { broken }\n")
    (reader / "history.lua").unlink()
    new_plugin = reader / "plugins" / "intruder.koplugin"
    new_plugin.mkdir()
    (new_plugin / "main.lua").write_text("-- new\n")
    plan = store.plan_restore(reader, taken)
    assert plan == RestorePlan(
        write=("history.lua", "settings.reader.lua"),
        remove=("plugins/intruder.koplugin/main.lua",),
    )
    store.restore(reader, taken)
    assert _tree(reader) == original
    assert not new_plugin.exists()  # no empty plugin folder left for KOReader to trip over
    assert not store.plan_restore(reader, taken)


def test_never_touches_books_or_sdr(reader: Path, store: SnapshotStore) -> None:
    volume = reader.parent.parent
    sdr = volume / "Books" / "Alice.sdr" / "metadata.epub.lua"
    taken = store.take(reader)
    sdr.write_text("return { percent_finished = 0.9 }\n")
    store.restore(reader, taken)
    assert "0.9" in sdr.read_text()
    assert not any(".sdr" in e.path for e in taken.files)


def test_guarded_write_keeps_an_allowed_change(reader: Path, store: SnapshotStore) -> None:
    target = reader / "settings.reader.lua"

    def write() -> None:
        target.write_text(target.read_text().replace("22", "26"))

    guarded_write(store, reader, write, allowed={"settings.reader.lua"})
    assert '["copt_font_size"] = 26' in target.read_text()


def test_guarded_write_restores_on_a_protected_mismatch(reader: Path, store: SnapshotStore) -> None:
    original = _tree(reader)

    def careless_update() -> None:
        (reader / "defaults.lua").write_text("-- new release\n")  # shipped file: allowed
        (reader / "styletweaks" / "justify.css").write_text("")  # protected: not allowed
        (reader / "patches" / USER_PATCH).unlink()
        (reader / "plugins" / f"{THIRD_PARTY_PLUGIN}.koplugin" / "main.lua").write_text("")

    with pytest.raises(WriteRejectedError) as caught:
        guarded_write(store, reader, careless_update)
    assert caught.value.changed == (
        f"patches/{USER_PATCH}",
        f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/main.lua",
        "styletweaks/justify.css",
    )
    assert _tree(reader) == original


def test_guarded_write_restores_when_the_write_fails(reader: Path, store: SnapshotStore) -> None:
    original = _tree(reader)

    def half_done() -> None:
        (reader / "defaults.lua").write_text("-- half\n")
        raise OSError("reader unplugged")

    with pytest.raises(WriteRejectedError, match="reader unplugged"):
        guarded_write(store, reader, half_done)
    assert _tree(reader) == original


def test_tampered_manifest_cannot_reach_outside(
    reader: Path, store: SnapshotStore, tmp_path: Path
) -> None:
    taken = store.take(reader)
    manifest = store.manifests / f"{taken.id}.json"
    data = json.loads(manifest.read_text())
    data["files"].append(["../../../outside.txt", 1, "0" * 64])
    manifest.write_text(json.dumps(data))
    with pytest.raises(SnapshotError, match="unsafe path"):
        store.load(taken.id)
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "a/../../b", "C:/x", "a\\..\\b", "./a", "a//b", ""]
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    with pytest.raises(SnapshotError):
        snap._check_relative(path)  # pylint: disable=protected-access


def test_symlinks_are_refused(reader: Path, store: SnapshotStore) -> None:
    (reader / "settings" / "link").symlink_to("/etc/hostname")
    with pytest.raises(SnapshotError, match="symbolic link"):
        store.take(reader)


def test_interrupted_snapshot_leaves_no_manifest(
    reader: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unplugged(source: Path, target: Path) -> str:
        raise OSError(f"{source.name}: device gone")

    monkeypatch.setattr(snap, "_copy_hashing", unplugged)
    with pytest.raises(OSError, match="device gone"):
        store.take(reader)
    assert not store.snapshots()


def test_not_enough_free_space(
    reader: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.disk_usage", lambda _path: SimpleNamespace(free=1024))
    with pytest.raises(SnapshotError, match="not enough free space"):
        store.take(reader)


def test_protected_list() -> None:
    third_party = frozenset({"projecttitle"})
    for path in (
        "settings.reader.lua",
        "settings.reader.lua.old",
        "history.lua",
        "defaults.custom.lua",
        "settings/gestures.lua",
        "styletweaks/x.css",
        "patches/2-x.lua",
        "plugins/projecttitle.koplugin/main.lua",
    ):
        assert is_protected(path, third_party), path
    for path in ("defaults.lua", "plugins/statistics.koplugin/main.lua", "data/cr3.css"):
        assert not is_protected(path, third_party), path


def test_device_id_is_a_safe_folder_name() -> None:
    assert device_id("PBVerseProColor", "PB634K3") == "PBVerseProColor-PB634K3"
    assert device_id(None, "../My Reader") == "reader-_My_Reader"
