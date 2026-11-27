"""B-28: KOReader put on a known PocketBook that has none yet (fake volume, fake GitHub)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kodesk.core import fresh_install, installer
from kodesk.core.archive import ArchiveError
from kodesk.core.fresh_install import BareReader, FreshInstallError, find_bare
from kodesk.core.install import find_install
from kodesk.core.snapshot import file_sha256
from tests.conftest import RELEASE, FakeGitHub, make_pocketbook, make_release_zip, shipped_files

LABEL = "PB634K3"
STUB = "system/bin/koreader.app"
LAUNCHER = "applications/koreader.app"


def bare_volume(root: Path) -> Path:
    """A Verse Pro Color's volume as it comes: system files, a book, another app; no KOReader."""
    (root / "system" / "bin").mkdir(parents=True)
    (root / "system" / "config.txt").write_text("firmware\n")
    (root / "Books").mkdir()
    (root / "Books" / "Alice.epub").write_bytes(b"PK book")
    (root / "applications").mkdir()
    (root / "applications" / "other.app").write_text("#!/bin/sh\n")
    return root


def github_with_release(tmp_path: Path, extra: dict[str, bytes] | None = None) -> FakeGitHub:
    github = FakeGitHub()
    archive = make_release_zip(tmp_path / "release.zip", "pocketbook", extra)
    github.koreader(RELEASE, archive)
    return github


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _reader(tmp_path: Path) -> BareReader:
    reader = find_bare(bare_volume(tmp_path / LABEL), LABEL)
    assert reader is not None
    return reader


def _prepare(reader: BareReader, github: FakeGitHub, tmp_path: Path) -> fresh_install.FreshPlan:
    return fresh_install.prepare(reader, github, github.save, tmp_path / "staging", lambda: False)


def test_only_known_models_without_koreader_are_offered(tmp_path: Path) -> None:
    volume = bare_volume(tmp_path / LABEL)
    reader = find_bare(volume, "pb634k3")
    assert reader is not None and reader.name == "PocketBook Verse Pro Color (pb634k3)"
    assert find_bare(volume, "PB700") is None  # a model kodesk does not know
    assert find_bare(tmp_path / "usb-stick", LABEL) is None  # no system/: not a PocketBook
    assert find_bare(tmp_path, LABEL) is None
    with_koreader = tmp_path / "other" / LABEL
    make_pocketbook(with_koreader)
    (with_koreader / "system").mkdir()
    assert find_bare(with_koreader, LABEL) is None


def test_install_writes_the_release_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(tmp_path)
    before = _files(reader.volume)
    plan = _prepare(reader, github_with_release(tmp_path), tmp_path)
    shipped = shipped_files("pocketbook")
    assert sorted(plan.write) == sorted(shipped) and not plan.present
    order: list[str] = []
    write_file = installer.write_file

    def logged(target: Path, data: bytes | Path) -> None:
        order.append(target.relative_to(reader.volume).as_posix())
        write_file(target, data)

    monkeypatch.setattr(fresh_install, "write_file", logged)
    reports: list[tuple[int, int]] = []
    done = fresh_install.install(plan, lambda d, t: reports.append((d, t)))
    assert order[-2:] == [LAUNCHER, STUB]  # the reader shows KOReader only once all is there
    koreader = "applications/koreader/"
    last = ["git-rev", "ota/package.index", "update_once.marker"]
    assert order[-5:-2] == [koreader + name for name in last]  # name the version last
    assert reports[-1][0] == reports[-1][1] > 0
    assert _files(reader.volume) == {**before, **shipped}
    assert not list(reader.volume.rglob("*.kodesk-tmp"))
    assert find_install(reader.volume).version == RELEASE

    fresh_install.remove(reader.volume, done)
    assert _files(reader.volume) == before
    assert not (reader.volume / "applications" / "koreader").exists()
    assert (reader.volume / "system" / "bin").is_dir()  # folders kodesk did not make stay


def test_a_file_in_the_way_refuses_but_the_same_file_is_left_alone(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    github = github_with_release(tmp_path)
    stub = reader.volume / STUB
    stub.write_bytes(shipped_files("pocketbook")[STUB])  # left from an earlier KOReader
    plan = _prepare(reader, github, tmp_path)
    assert plan.present == (STUB,) and STUB not in plan.write
    done = fresh_install.install(plan)
    fresh_install.remove(reader.volume, done)
    assert stub.is_file()  # was there before: stays

    (reader.volume / LAUNCHER).write_text("#!/bin/sh\n# mine\n")
    with pytest.raises(FreshInstallError, match="never replaces"):
        _prepare(reader, github, tmp_path / "again")
    assert (reader.volume / LAUNCHER).read_text() == "#!/bin/sh\n# mine\n"


def test_a_failure_halfway_leaves_the_reader_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(tmp_path)
    before = _files(reader.volume)
    plan = _prepare(reader, github_with_release(tmp_path), tmp_path)
    calls = {"n": 0}
    write_file = installer.write_file

    def flaky(target: Path, data: bytes | Path) -> None:
        calls["n"] += 1
        if calls["n"] == 40:
            target.with_name(target.name + ".kodesk-tmp").write_bytes(b"half")
            raise OSError("cable pulled")
        write_file(target, data)

    monkeypatch.setattr(fresh_install, "write_file", flaky)
    with pytest.raises(OSError, match="cable pulled"):
        fresh_install.install(plan)
    assert _files(reader.volume) == before
    assert not (reader.volume / "applications" / "koreader").exists()
    assert (reader.volume / "applications" / "other.app").is_file()


def test_a_hostile_release_or_a_full_reader_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(tmp_path)
    before = _files(reader.volume)
    hostile = github_with_release(tmp_path, extra={"../../evil.sh": b"rm -rf /"})
    with pytest.raises(ArchiveError):
        _prepare(reader, hostile, tmp_path / "a")
    stray = github_with_release(tmp_path, extra={"system/bin/other.app": b"x"})
    with pytest.raises(ArchiveError, match="unexpected file"):
        _prepare(reader, stray, tmp_path / "b")
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: type("U", (), {"free": 1})())
    with pytest.raises(FreshInstallError, match="Not enough free space"):
        _prepare(reader, github_with_release(tmp_path), tmp_path / "c")
    assert _files(reader.volume) == before


def test_the_plan_is_the_whole_release(tmp_path: Path) -> None:
    reader = _reader(tmp_path)
    plan = _prepare(reader, github_with_release(tmp_path), tmp_path)
    for path in plan.write:
        assert file_sha256(plan.staged.folder / path) == plan.staged.files[path]
    assert plan.release.tag == RELEASE and plan.bytes == sum(plan.staged.sizes.values())
