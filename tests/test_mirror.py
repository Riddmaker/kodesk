"""The Wi-Fi mirror (B-27) against the local test SFTP server, never the reader."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import paramiko
import pytest

from kodesk.core.mirror import (
    OLD_SUFFIX,
    Mirror,
    MirrorError,
    WifiReader,
    remote_entries,
    split_reader_path,
)
from kodesk.core.snapshot import TEMP_SUFFIX, SnapshotStore, WriteRejectedError, guarded_write
from kodesk.core.wifi import AUTHORIZED_KEYS, KeyStore, Pairing, connect, pair
from tests.conftest import THIRD_PARTY_PLUGIN, USER_PATCH, make_reader
from tests.sftp_server import FakeReader, new_host_key, serve

ID = "PBVerseProColor-PB634K3"
KOREADER = "/mnt/ext1/applications/koreader"
PLUGIN = f"applications/koreader/plugins/{THIRD_PARTY_PLUGIN}.koplugin"


@pytest.fixture(name="served")
def fixture_served(tmp_path: Path) -> Path:
    """What the fake reader serves: ``<served>/mnt/ext1`` is the reader's volume."""
    koreader = make_reader(tmp_path / "served" / "mnt" / "ext1", "pocketbook")
    (koreader / "cache").mkdir(exist_ok=True)
    (koreader / "cache" / "big.bin").write_bytes(b"x" * 10)
    logo = tmp_path / "served/mnt/ext1/system/logo/sleeplogo/pb_sleeplogo.bmp"
    logo.parent.mkdir(parents=True)
    logo.write_bytes(b"BM")
    return tmp_path / "served"


@pytest.fixture(name="volume")
def fixture_volume(served: Path) -> Path:
    return served / "mnt" / "ext1"


@pytest.fixture(name="session")
def fixture_session(
    served: Path, volume: Path, tmp_path: Path, store: SnapshotStore
) -> Iterator[tuple[KeyStore, Pairing, FakeReader]]:
    """kodesk paired with the served reader (over "USB", i.e. its folder), server running."""
    keys = KeyStore(tmp_path / "data" / "wifi")
    koreader = volume / "applications/koreader"
    start = Pairing(ID, "PocketBook Verse Pro", koreader_path=KOREADER)
    paired, _snapshot = pair(store, koreader, keys, start, None)
    fake = FakeReader(served, koreader / AUTHORIZED_KEYS, new_host_key()[0], writable=True)
    with serve(fake) as port:
        pairing = Pairing(ID, paired.name, port, koreader_path=KOREADER)
        keys.save(pairing)
        yield keys, pairing, fake


@contextmanager
def _sftp(session: tuple[KeyStore, Pairing, FakeReader]) -> Iterator[paramiko.SFTPClient]:
    keys, pairing, _fake = session
    with connect(keys, keys.load(pairing.reader_id) or pairing, "127.0.0.1") as connection:
        yield connection.sftp


def _touch_later(path: Path) -> None:
    """A change the mirror notices: SFTP times are whole seconds, so move it one on."""
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))


def test_reader_paths_are_checked() -> None:
    assert split_reader_path(KOREADER) == ("/mnt/ext1", "applications/koreader")
    for bad in ("/etc/koreader", "/mnt/ext1/../etc", "/mnt/ext1x/koreader", "/mnt/ext1/"):
        with pytest.raises(MirrorError):
            split_reader_path(bad)


def test_sync_fetches_what_changed_and_drops_what_went(
    session: tuple[KeyStore, Pairing, FakeReader], volume: Path, tmp_path: Path
) -> None:
    mirror = Mirror(tmp_path / "mirror", KOREADER, ("system/logo/sleeplogo/pb_sleeplogo.bmp",))
    reports: list[tuple[int, int]] = []
    linked = volume / "applications/koreader/settings/linked"
    linked.symlink_to(volume / "applications/koreader/history.lua")
    with _sftp(session) as sftp:
        mirror.sync(sftp, lambda done, total: reports.append((done, total)))
        koreader = volume / "applications/koreader"
        local = mirror.koreader_dir
        assert (local / "settings.reader.lua").read_bytes() == (
            koreader / "settings.reader.lua"
        ).read_bytes()
        assert (local / f"patches/{USER_PATCH}").is_file()
        assert not (local / "cache").exists()  # left out, as in snapshots
        assert not (local / "settings" / "linked").exists()  # links are skipped
        assert (mirror.files / "system/logo/sleeplogo/pb_sleeplogo.bmp").read_bytes() == b"BM"
        assert not (mirror.files / "Books").exists()  # books are never copied
        assert reports[-1][0] == reports[-1][1] > 0
        assert (tmp_path / "mirror").stat().st_mode & 0o077 == 0
        assert mirror.changes() == ((), ())

        reports.clear()
        mirror.sync(sftp, lambda done, total: reports.append((done, total)))
        assert reports == [(0, 0)]  # nothing changed: nothing fetched

        (koreader / "history.lua").write_text("return {}\n", encoding="utf-8")
        _touch_later(koreader / "history.lua")
        (koreader / f"patches/{USER_PATCH}").unlink()
        mirror.sync(sftp)
        assert (local / "history.lua").read_text(encoding="utf-8") == "return {}\n"
        assert not (local / f"patches/{USER_PATCH}").exists()
        assert sorted(n for n, folder in remote_entries(sftp, "/mnt/ext1/Books")) == [
            "Alice.epub",
            "Alice.sdr",
        ]
    assert Mirror(tmp_path / "mirror", KOREADER).known == mirror.known  # kept across restarts


def test_a_plugin_installed_in_the_mirror_reaches_the_reader(
    session: tuple[KeyStore, Pairing, FakeReader], volume: Path, tmp_path: Path
) -> None:
    mirror = Mirror(tmp_path / "mirror", KOREADER)
    koreader = volume / "applications/koreader"
    with _sftp(session) as sftp:
        mirror.sync(sftp)
        new = mirror.koreader_dir / "plugins/wifitest.koplugin"
        new.mkdir()
        (new / "main.lua").write_text("-- wifitest\n", encoding="utf-8")
        (new / "sub").mkdir()
        (new / "sub" / "x.lua").write_text("-- x\n", encoding="utf-8")
        (mirror.koreader_dir / f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/main.lua").write_text(
            "-- third-party 2\n", encoding="utf-8"
        )
        sent = mirror.push(sftp)
        assert len(sent) == 3
        assert (koreader / "plugins/wifitest.koplugin/sub/x.lua").read_text() == "-- x\n"
        assert (
            koreader / f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/main.lua"
        ).read_text() == "-- third-party 2\n"
        leftovers = [p for p in koreader.rglob("*") if p.name.endswith((TEMP_SUFFIX, OLD_SUFFIX))]
        assert leftovers == []
        assert mirror.changes() == ((), ())

        shutil.rmtree(new)
        assert len(mirror.push(sftp)) == 2
        assert not (koreader / "plugins/wifitest.koplugin").exists()  # emptied folders go too
        assert (koreader / "plugins").is_dir()


def test_settings_are_never_sent_over_wifi(
    session: tuple[KeyStore, Pairing, FakeReader], volume: Path, tmp_path: Path
) -> None:
    mirror = Mirror(tmp_path / "mirror", KOREADER)
    settings = volume / "applications/koreader/settings.reader.lua"
    before = settings.read_bytes()
    with _sftp(session) as sftp:
        mirror.sync(sftp)
        (mirror.koreader_dir / "settings.reader.lua").write_text("return {}\n", encoding="utf-8")
        (mirror.koreader_dir / "patches/2-new.lua").write_text("-- new\n", encoding="utf-8")
        with pytest.raises(MirrorError, match="only plugins, patches"):
            mirror.push(sftp)
    assert settings.read_bytes() == before
    assert not (volume / "applications/koreader/patches/2-new.lua").exists()  # all or nothing
    assert (mirror.koreader_dir / "settings.reader.lua").read_bytes() == before  # in step again
    assert not (mirror.koreader_dir / "patches/2-new.lua").exists()
    assert mirror.changes() == ((), ())


def test_a_file_changed_on_the_reader_meanwhile_is_not_overwritten(
    session: tuple[KeyStore, Pairing, FakeReader], volume: Path, tmp_path: Path
) -> None:
    mirror = Mirror(tmp_path / "mirror", KOREADER)
    target = volume / f"{PLUGIN}/main.lua"
    with _sftp(session) as sftp:
        mirror.sync(sftp)
        target.write_text("-- changed on the reader\n", encoding="utf-8")
        _touch_later(target)
        (mirror.files / PLUGIN / "main.lua").write_text("-- kodesk\n", encoding="utf-8")
        with pytest.raises(MirrorError, match="changed on the reader"):
            mirror.push(sftp)
    assert target.read_text() == "-- changed on the reader\n"
    assert (mirror.files / PLUGIN / "main.lua").read_text() == "-- changed on the reader\n"


def test_a_failure_halfway_puts_the_old_files_back(
    session: tuple[KeyStore, Pairing, FakeReader],
    volume: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mirror = Mirror(tmp_path / "mirror", KOREADER)
    plugin = volume / PLUGIN
    before = {p.name: p.read_bytes() for p in plugin.iterdir()}
    with _sftp(session) as sftp:
        mirror.sync(sftp)
        for name in before:
            (mirror.files / PLUGIN / name).write_text(f"-- new {name}\n", encoding="utf-8")
        calls = {"n": 0}
        rename = paramiko.SFTPClient.rename

        def flaky(self: paramiko.SFTPClient, old: str, new: str) -> None:
            calls["n"] += 1
            if calls["n"] == 3:  # the second file's old copy is set aside, then the link drops
                raise OSError("connection lost")
            rename(self, old, new)

        monkeypatch.setattr(paramiko.SFTPClient, "rename", flaky)
        with pytest.raises(MirrorError, match="connection lost"):
            mirror.push(sftp)
        monkeypatch.setattr(paramiko.SFTPClient, "rename", rename)
    assert {p.name: p.read_bytes() for p in plugin.iterdir()} == before  # no temp, no old
    assert mirror.changes() == ((), ())


def test_guarded_write_on_the_mirror_publishes_and_undoes_a_refusal(
    session: tuple[KeyStore, Pairing, FakeReader], volume: Path, tmp_path: Path
) -> None:
    keys, pairing, _fake = session
    wifi = WifiReader.open(keys, pairing, "127.0.0.1", tmp_path / "mirrors")
    install = wifi.sync()
    assert install.root == wifi.mirror.koreader_dir and install.platform == "pocketbook"
    assert ("Alice.epub", False) in wifi.books
    store = SnapshotStore(tmp_path / "data" / "snapshots" / ID)
    store.publish = wifi.publish
    koreader = volume / "applications/koreader"

    files = {
        "wifitest.koplugin/main.lua": b"-- wifitest\n",
        "wifitest.koplugin/_meta.lua": b"return {}\n",
    }

    def add_plugin() -> None:
        folder = install.root / "plugins/wifitest.koplugin"
        folder.mkdir()
        for name, data in files.items():
            (install.root / "plugins" / name).write_bytes(data)

    snapshot = guarded_write(store, install.root, add_plugin)
    assert (koreader / "plugins/wifitest.koplugin/main.lua").read_bytes() == b"-- wifitest\n"

    def touch_settings() -> None:
        settings = install.root / "settings.reader.lua"
        settings.write_text(settings.read_text() + "-- x\n")

    before = (koreader / "settings.reader.lua").read_bytes()
    with pytest.raises(WriteRejectedError, match="only plugins, patches"):
        guarded_write(store, install.root, touch_settings, allowed={"settings.reader.lua"})
    assert (koreader / "settings.reader.lua").read_bytes() == before
    assert (install.root / "settings.reader.lua").read_bytes() == before

    store.restore(install.root, snapshot)  # the way back works over Wi-Fi too
    assert not (koreader / "plugins/wifitest.koplugin").exists()


def test_connect_finishes_a_send_the_lost_connection_interrupted(
    session: tuple[KeyStore, Pairing, FakeReader], volume: Path, tmp_path: Path
) -> None:
    koreader = volume / "applications/koreader"
    plugin = volume / PLUGIN
    first, second = sorted(p for p in plugin.iterdir() if p.is_file())[:2]
    old_first = first.read_bytes()
    first.rename(first.with_name(first.name + ".kodesk-old"))  # set aside, new one not placed
    second.with_name(second.name + ".kodesk-old").write_bytes(b"-- old\n")  # new one placed
    (koreader / f"patches/{USER_PATCH}{TEMP_SUFFIX}").write_bytes(b"half")
    (koreader / "ota").mkdir(exist_ok=True)
    (koreader / f"ota/koreader.updated.tar{TEMP_SUFFIX}").write_bytes(b"half")
    foreign = koreader / "settings.reader.lua.kodesk-old"  # not where kodesk sends: left alone
    foreign.write_bytes(b"x")
    mirror = Mirror(tmp_path / "mirror", KOREADER)
    with _sftp(session) as sftp:
        mirror.sync(sftp)
    assert first.read_bytes() == old_first
    assert not list(plugin.glob("*.kodesk-*"))
    assert not list(koreader.glob(f"patches/*{TEMP_SUFFIX}"))
    assert not list(koreader.glob(f"ota/*{TEMP_SUFFIX}"))
    assert foreign.read_bytes() == b"x"
    assert (mirror.files / PLUGIN / first.name).read_bytes() == old_first
    assert mirror.changes() == ((), ())


def test_sync_leaves_koreaders_update_archives_out(
    session: tuple[KeyStore, Pairing, FakeReader], volume: Path, tmp_path: Path
) -> None:
    ota = volume / "applications/koreader/ota"
    ota.mkdir(exist_ok=True)
    (ota / "koreader.installed.tar").write_bytes(b"t" * 100)
    (ota / "package.index").write_text("applications/koreader/reader.lua\n")
    mirror = Mirror(tmp_path / "mirror", KOREADER)
    with _sftp(session) as sftp:
        mirror.sync(sftp)
    assert (mirror.koreader_dir / "ota/package.index").is_file()
    assert not (mirror.koreader_dir / "ota/koreader.installed.tar").exists()


def test_a_koreader_update_is_sent_once_and_read_back(
    session: tuple[KeyStore, Pairing, FakeReader], volume: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "koreader.updated.tar"
    archive.write_bytes(b"u" * 70000)
    target = volume / "applications/koreader/ota/koreader.updated.tar"
    mirror = Mirror(tmp_path / "mirror", KOREADER)
    reports: list[tuple[int, int]] = []
    with _sftp(session) as sftp:
        with pytest.raises(MirrorError):
            mirror.send_new(sftp, "settings.reader.lua", archive)

        def stop(done: int, _total: int) -> None:
            if done:
                raise RuntimeError("cancelled")

        with pytest.raises(RuntimeError):
            mirror.send_new(sftp, "ota/koreader.updated.tar", archive, stop)
        assert not target.exists() and not target.with_name(target.name + TEMP_SUFFIX).exists()

        mirror.send_new(sftp, "ota/koreader.updated.tar", archive, lambda *r: reports.append(r))
        assert target.read_bytes() == archive.read_bytes()
        assert reports[0] == (0, 70000) and reports[-1] == (70000, 70000)
        with pytest.raises(MirrorError, match="already waiting"):
            mirror.send_new(sftp, "ota/koreader.updated.tar", archive)
    assert not list(target.parent.glob("*" + TEMP_SUFFIX))


def test_an_old_pairing_must_be_paired_again(tmp_path: Path) -> None:
    keys = KeyStore(tmp_path / "wifi")
    with pytest.raises(MirrorError, match="Pair this reader again"):
        WifiReader.open(keys, Pairing(ID, "PocketBook"), "127.0.0.1", tmp_path / "m")


def test_a_sleep_picture_is_sent_as_a_new_file_in_photo(
    session: tuple[KeyStore, Pairing, FakeReader], volume: Path, tmp_path: Path
) -> None:
    """B-14 over Wi-Fi: a new file in Photo/ (made if missing), never a replacement; only names
    kodesk makes; read back before it gets its name."""
    mirror = Mirror(tmp_path / "mirror", KOREADER)
    name = "kodesk-sleep-20261127-120000.png"
    shutil.rmtree(volume / "Photo", ignore_errors=True)
    with _sftp(session) as sftp:
        assert mirror.send_picture(sftp, name, b"png") == f"Photo/{name}"
        assert (volume / "Photo" / name).read_bytes() == b"png"
        with pytest.raises(MirrorError, match="already on the reader"):
            mirror.send_picture(sftp, name, b"other")
        assert (volume / "Photo" / name).read_bytes() == b"png"
        for bad in ("../x.png", "pb_sleeplogo.bmp", "kodesk-sleep-1.png"):
            with pytest.raises(MirrorError, match="does not send"):
                mirror.send_picture(sftp, bad, b"png")
        with pytest.raises(MirrorError, match="unexpected size"):
            mirror.send_picture(sftp, "kodesk-sleep-20261127-120001.png", b"")
    assert sorted(p.name for p in (volume / "Photo").iterdir()) == [name]
    assert not list(volume.rglob("*" + TEMP_SUFFIX))
