"""KOReader core update (B-06) on fake reader trees and a fake GitHub (never the real device)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from kodesk.core import installer, koversion, net, release, update
from kodesk.core.archive import ArchiveError, index_paths, stage_release
from kodesk.core.install import describe
from kodesk.core.net import CancelledError, NetError
from kodesk.core.snapshot import SnapshotStore, WriteRejectedError, file_sha256
from tests.conftest import (
    RELEASE,
    THIRD_PARTY_PLUGIN,
    USER_PATCH,
    FakeGitHub,
    make_reader,
    make_release_zip,
)

NEW = "v2026.08"
KO = "applications/koreader/"
CHANGED = KO + "frontend/ui/widget/button.lua"
ADDED = KO + "frontend/ui/widget/brandnew.lua"
DROPPED = KO + "frontend/ui/widget/textwidget.lua"
EDITED = KO + "data/cr3.css"  # the user changed it; the new release changes it too
LAUNCHER = "applications/koreader.app"
NEW_FILES = {
    CHANGED: b"-- button v2026.08\n",
    ADDED: b"-- new in v2026.08\n",
    EDITED: b"/* cr3.css v2026.08 */\n",
    LAUNCHER: b"#!/bin/sh\n# launcher v2026.08\n",
}
USER_STATE = (
    "settings.reader.lua",
    "history.lua",
    "settings/vocabulary_builder.sqlite3",
    "styletweaks/justify.css",
    f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/main.lua",
    f"patches/{USER_PATCH}",
)


def _volume(koreader: Path) -> Path:
    return koreader.parents[1]


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def koreader_github(tmp_path: Path, *, old: bool = True, **new: object) -> FakeGitHub:
    """GitHub with the installed release (unless ``old`` is False) and the newer one."""
    github = FakeGitHub()
    if old:
        github.koreader(RELEASE, make_release_zip(tmp_path / "old.zip", "pocketbook"), latest=False)
    archive = make_release_zip(
        tmp_path / "new.zip",
        "pocketbook",
        version=NEW,
        changed=new.get("changed", NEW_FILES),  # type: ignore[arg-type]
        dropped=new.get("dropped", (DROPPED,)),  # type: ignore[arg-type]
    )
    github.koreader(NEW, archive)
    return github


def _prepare(koreader: Path, github: FakeGitHub, tmp_path: Path) -> update.UpdatePlan:
    install = describe(koreader, "pocketbook")
    plan = update.prepare(install, github, github.save, tmp_path / "staging", lambda: False)
    assert isinstance(plan, update.UpdatePlan)
    return plan


@pytest.fixture(name="edited")
def _edited(reader: Path) -> Path:
    """The user's own change to a shipped file."""
    (_volume(reader) / EDITED).write_text("/* mine */\n")
    return reader


def test_update_writes_the_release_and_keeps_the_setup(
    edited: Path, store: SnapshotStore, tmp_path: Path
) -> None:
    volume = _volume(edited)
    user_before = {p: file_sha256(edited / p) for p in USER_STATE}
    books_before = _tree(volume / "Books")
    plan = _prepare(edited, koreader_github(tmp_path), tmp_path)

    assert (plan.installed, plan.release.tag) == (RELEASE, NEW)
    assert plan.add == (ADDED,)
    assert set(plan.replace) == {
        CHANGED,
        EDITED,
        LAUNCHER,
        KO + "git-rev",
        KO + "ota/package.index",
    }
    assert plan.remove == (DROPPED,)
    assert plan.edited == (EDITED,) and plan.edits_known
    assert not plan.conflicts and plan.unchanged > 900

    snapshot = update.apply_update(store, describe(edited, "pocketbook"), plan, frozenset())

    for path, data in NEW_FILES.items():
        assert (volume / path).read_bytes() == data
    assert not (volume / DROPPED).exists()
    assert (edited / "git-rev").read_text() == f"{NEW}\n"
    assert {p: file_sha256(edited / p) for p in USER_STATE} == user_before
    assert _tree(volume / "Books") == books_before
    assert [e.path for e in snapshot.outside] == ["../koreader.app"]
    assert update.pending(store.base) is None
    assert describe(edited, "pocketbook").version == NEW


def test_keep_my_version_of_an_edited_file(
    edited: Path, store: SnapshotStore, tmp_path: Path
) -> None:
    plan = _prepare(edited, koreader_github(tmp_path), tmp_path)
    update.apply_update(store, describe(edited, "pocketbook"), plan, frozenset({EDITED}))
    assert (_volume(edited) / EDITED).read_text() == "/* mine */\n"
    assert (_volume(edited) / CHANGED).read_bytes() == NEW_FILES[CHANGED]


def test_only_edited_files_can_be_kept(reader: Path, store: SnapshotStore, tmp_path: Path) -> None:
    plan = _prepare(reader, koreader_github(tmp_path), tmp_path)
    with pytest.raises(update.UpdateError):
        update.apply_update(store, describe(reader, "pocketbook"), plan, frozenset({CHANGED}))


def test_without_the_installed_release_edits_are_unknown(edited: Path, tmp_path: Path) -> None:
    github = koreader_github(tmp_path, old=False)  # e.g. v2026.07.2: a tag without a release
    plan = _prepare(edited, github, tmp_path)
    assert not plan.edits_known and not plan.edited
    assert f"https://api.github.com/repos/koreader/koreader/releases/tags/{RELEASE}" in (
        github.requests
    )


def test_files_kodesk_did_not_ship_are_never_removed(
    reader: Path, store: SnapshotStore, tmp_path: Path
) -> None:
    mine = reader / "frontend" / "ui" / "widget" / "mywidget.lua"
    mine.write_text("-- user file inside KOReader's folder\n")
    plan = _prepare(reader, koreader_github(tmp_path), tmp_path)
    update.apply_update(store, describe(reader, "pocketbook"), plan, frozenset())
    assert mine.read_text() == "-- user file inside KOReader's folder\n"


def test_stale_ota_base_is_removed(reader: Path, store: SnapshotStore, tmp_path: Path) -> None:
    tar = reader / update.INSTALLED_TAR
    tar.write_bytes(b"old release tar")
    plan = _prepare(reader, koreader_github(tmp_path), tmp_path)
    assert plan.stale == (KO + update.INSTALLED_TAR,)
    update.apply_update(store, describe(reader, "pocketbook"), plan, frozenset())
    assert not tar.exists()


def test_a_plugin_named_like_a_new_built_in_one_blocks(
    reader: Path, store: SnapshotStore, tmp_path: Path
) -> None:
    clash = KO + f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/main.lua"
    github = koreader_github(tmp_path, changed={**NEW_FILES, clash: b"-- now built in\n"})
    plan = _prepare(reader, github, tmp_path)
    assert plan.conflicts == (THIRD_PARTY_PLUGIN,)
    with pytest.raises(update.UpdateError, match=THIRD_PARTY_PLUGIN):
        update.apply_update(store, describe(reader, "pocketbook"), plan, frozenset())
    assert (reader / "git-rev").read_text() == f"{RELEASE}\n"


def test_a_failure_mid_write_restores_everything(
    reader: Path, store: SnapshotStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volume = _volume(reader)
    before = _tree(volume / "applications")
    plan = _prepare(reader, koreader_github(tmp_path), tmp_path)
    written: list[Path] = []

    def unplugged(target: Path, _data: bytes | Path) -> None:
        if len(written) == 2:
            raise OSError("cable pulled")
        written.append(target)
        target.write_bytes(b"half")

    monkeypatch.setattr(update, "write_file", unplugged)
    with pytest.raises(WriteRejectedError):
        update.apply_update(store, describe(reader, "pocketbook"), plan, frozenset())
    assert _tree(volume / "applications") == before
    assert update.pending(store.base) is None


def test_version_files_are_written_last(
    reader: Path, store: SnapshotStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real = installer.write_file

    def spy(target: Path, data: bytes | Path) -> None:
        order.append(target.relative_to(_volume(reader)).as_posix())
        real(target, data)

    monkeypatch.setattr(update, "write_file", spy)
    plan = _prepare(reader, koreader_github(tmp_path), tmp_path)
    update.apply_update(store, describe(reader, "pocketbook"), plan, frozenset())
    assert order[-2:] == [KO + "git-rev", KO + "ota/package.index"]


def test_up_to_date(reader: Path, tmp_path: Path) -> None:
    github = FakeGitHub()
    github.koreader(RELEASE, make_release_zip(tmp_path / "r.zip", "pocketbook"))
    install = describe(reader, "pocketbook")
    result = update.prepare(install, github, github.save, tmp_path / "s", lambda: False)
    assert result == update.UpToDate(RELEASE, RELEASE)
    assert update.check(install, github) == "Up to date"
    assert not any("download" in url for url in github.requests)


def test_check_names_a_newer_release(reader: Path, tmp_path: Path) -> None:
    install = describe(reader, "pocketbook")
    assert update.check(install, koreader_github(tmp_path)) == f"{NEW} available"
    assert update.check(install, FakeGitHub()).startswith("Not checked:")


def test_newer_than_the_latest_stable() -> None:
    assert "newer than" in update.UpToDate("v2026.07.2", RELEASE).text


def test_cancel_stops_the_download(reader: Path, tmp_path: Path) -> None:
    github = koreader_github(tmp_path)
    install = describe(reader, "pocketbook")
    with pytest.raises(CancelledError):
        update.prepare(install, github, github.save, tmp_path / "s", lambda: True)


def test_a_download_that_does_not_match_github_is_refused(reader: Path, tmp_path: Path) -> None:
    github = koreader_github(tmp_path)
    url = next(u for u in github.files if u.endswith(f"pocketbook-{NEW}.zip"))
    github.files[url] = github.files[url] + b"tampered"
    with pytest.raises(NetError, match="SHA-256"):
        _prepare(reader, github, tmp_path)
    assert not list((tmp_path / "staging").glob("*.zip"))


def test_only_pocketbook_for_now(tmp_path: Path) -> None:
    koreader = make_reader(tmp_path / "KOBO", "kobo")
    with pytest.raises(update.UpdateError, match="PocketBook"):
        update.layout_of(describe(koreader, "kobo"))


def test_without_a_file_list_nothing_is_updated(reader: Path, tmp_path: Path) -> None:
    (reader / "ota" / "package.index").unlink()
    with pytest.raises(update.UpdateError, match=r"package\.index"):
        _prepare(reader, koreader_github(tmp_path), tmp_path)


def test_pending_marker(tmp_path: Path) -> None:
    update.remember(tmp_path, "v2026.07.1", NEW, "20261126T130000Z")
    assert update.pending(tmp_path) == update.PendingUpdate("v2026.07.1", NEW, "20261126T130000Z")
    update.clear(tmp_path)
    assert update.pending(tmp_path) is None


@pytest.mark.parametrize(
    "content",
    ["{", "[]", json.dumps({"from": "a", "to": "b", "snapshot": "../../etc"}), '{"from": 1}'],
)
def test_a_damaged_marker_is_ignored(tmp_path: Path, content: str) -> None:
    (tmp_path / update.PENDING_FILE).write_text(content)
    assert update.pending(tmp_path) is None


# -- versions, release answers, release archives ---------------------------------------


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("v2026.07", "v2026.07.1"),
        ("v2026.07.1", "v2026.07.2"),
        ("v2026.07.2", "v2026.07.2-353-g25183e2"),
        ("v2025.10", "v2026.07"),
        (None, "v2025.10"),
    ],
)
def test_versions_compare_like_koreader(older: str | None, newer: str) -> None:
    assert koversion.is_newer(newer, older)
    assert not koversion.is_newer(older or "", newer)


def test_nightly_details() -> None:
    assert koversion.commit("v2026.07.2-353-g25183e2") == "25183e2"
    assert koversion.commit("v2026.07.2") is None
    assert koversion.is_stable("v2026.07.2") and not koversion.is_stable("v2026.07.2-353-g2518")
    assert koversion.normalized("garbage") == 0


def _answer(**changes: object) -> bytes:
    name = f"koreader-pocketbook-{NEW}.zip"
    asset = {
        "name": name,
        "browser_download_url": f"{release.DOWNLOADS}{NEW}/{name}",
        "digest": "sha256:" + "a" * 64,
        "size": 1000,
    }
    body: dict[str, object] = {"tag_name": NEW, "draft": False, "prerelease": False}
    asset.update({k: v for k, v in changes.items() if k in asset})
    body.update({k: v for k, v in changes.items() if k in body})
    body["assets"] = changes.get("assets", [asset])
    return json.dumps(body).encode()


def test_a_good_release_answer() -> None:
    found = release._parse(_answer(), "pocketbook")  # pylint: disable=protected-access
    assert (found.tag, found.sha256, found.size) == (NEW, "a" * 64, 1000)


@pytest.mark.parametrize(
    "changes",
    [
        {"draft": True},
        {"prerelease": True},
        {"tag_name": "nightly"},
        {"browser_download_url": "https://github.com/evil/koreader/releases/download/x.zip"},
        {"digest": None},
        {"digest": "md5:abc"},
        {"size": 0},
        {"size": 10**10},
        {"assets": []},
    ],
)
def test_release_answers_kodesk_refuses(changes: dict[str, object]) -> None:
    with pytest.raises(NetError):
        release._parse(_answer(**changes), "pocketbook")  # pylint: disable=protected-access


LAYOUT = update.LAYOUTS["pocketbook"]


def test_a_release_with_foreign_files_is_refused(tmp_path: Path) -> None:
    archive = make_release_zip(tmp_path / "r.zip", "pocketbook", {"Books/evil.epub": b"x"})
    with pytest.raises(ArchiveError, match="unexpected file"):
        stage_release(archive, tmp_path / "s", LAYOUT.accepted, LAYOUT.skipped)


def test_a_release_whose_index_lies_is_refused(tmp_path: Path) -> None:
    archive = make_release_zip(tmp_path / "r.zip", "pocketbook", {KO + "unlisted.lua": b"x"})
    with pytest.raises(ArchiveError, match="does not match"):
        stage_release(archive, tmp_path / "s", LAYOUT.accepted, LAYOUT.skipped)


def test_a_release_with_zip_slip_is_refused(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("applications/koreader/../../evil", b"x")
    (tmp_path / "r.zip").write_bytes(buffer.getvalue())
    with pytest.raises(ArchiveError):
        stage_release(tmp_path / "r.zip", tmp_path / "s", LAYOUT.accepted, LAYOUT.skipped)


@pytest.mark.parametrize("line", ["../../etc/passwd", "/etc/passwd", "a/./b", "a//b", "a/con:"])
def test_unsafe_index_lines_are_refused(line: str) -> None:
    with pytest.raises(ArchiveError):
        index_paths(f"applications/koreader/reader.lua\n{line}\n")


def test_old_index_lines_with_a_prefix() -> None:
    assert index_paths("../applications/koreader/reader.lua\n\n") == [
        "applications/koreader/reader.lua"
    ]


class _Answer(io.BytesIO):
    """What urllib's opener returns, without any network."""

    def __enter__(self) -> _Answer:
        return self


def test_https_save_streams_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(net, "_open", lambda _url, _headers: _Answer(b"x" * 3000))
    target = tmp_path / "a.zip"
    assert net.https_save(release.DOWNLOADS + "a.zip", target, 5000, lambda: False) == (
        hashlib.sha256(b"x" * 3000).hexdigest()
    )
    with pytest.raises(NetError, match="larger"):
        net.https_save(release.DOWNLOADS + "b.zip", tmp_path / "b.zip", 1000, lambda: False)
    with pytest.raises(CancelledError):
        net.https_save(release.DOWNLOADS + "c.zip", tmp_path / "c.zip", 5000, lambda: True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.zip"]
    with pytest.raises(FileExistsError):  # an existing file is never overwritten or removed
        net.https_save(release.DOWNLOADS + "a.zip", target, 5000, lambda: False)
    assert target.exists()
