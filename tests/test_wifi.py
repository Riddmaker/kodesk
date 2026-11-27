"""Wi-Fi pairing and connection (B-11) against a local test SFTP server, never the reader."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from kodesk.core.luadump import SettingsDocument
from kodesk.core.snapshot import SnapshotStore
from kodesk.core.wifi import (
    AUTHORIZED_KEYS,
    HOST_KEY_FILE,
    HostKeyChangedError,
    KeyStore,
    Pairing,
    WifiError,
    check_host,
    connect,
    host_key_from_dropbear,
    pair,
    pair_lines,
    ssh_port,
    unpair,
    with_key,
    without_key,
)
from tests.sftp_server import FakeReader, new_host_key, serve

OTHER = b"# my own laptop\nssh-rsa AAAAB3Nza... me@laptop\r\nssh-ed25519 AAAAC3Nzaother other\n"
ID = "PBVerseProColor-PB634K3"


@pytest.fixture(name="keys")
def fixture_keys(tmp_path: Path) -> KeyStore:
    return KeyStore(tmp_path / "data" / "wifi")


def _paired(reader: Path, store: SnapshotStore, keys: KeyStore, port: int = 2222) -> Pairing:
    settings = SettingsDocument.parse(f'return {{\n    ["SSH_port"] = "{port}",\n}}\n')
    paired, _snapshot = pair(store, reader, keys, Pairing(ID, "PocketBook Verse Pro"), settings)
    return paired


def test_the_private_key_is_only_readable_by_the_user(keys: KeyStore) -> None:
    line = keys.public_line(ID)
    assert line.startswith("ssh-ed25519 AAAAC3Nza") and " kodesk-" in line
    key_file = keys.base / ID / "id_ed25519"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700
    assert keys.public_line(ID).split()[1] == line.split()[1]  # made once, then reused
    with pytest.raises(ValueError):
        keys.public_line("../escape")


def test_adding_and_removing_the_line_keeps_every_other_byte() -> None:
    line = "ssh-ed25519 AAAAC3Nzakodesk kodesk-2026-11-27"
    added = with_key(OTHER, line)
    assert added == OTHER + line.encode() + b"\n"
    assert with_key(added, line) == added  # never twice
    assert without_key(added, line) == OTHER
    assert with_key(b"no newline", line) == b"no newline\n" + line.encode() + b"\n"


def test_the_host_key_is_read_from_dropbears_file() -> None:
    key, dropbear = new_host_key()
    assert host_key_from_dropbear(dropbear) == key.get_base64()
    with pytest.raises(WifiError):
        host_key_from_dropbear(dropbear[:-1])
    with pytest.raises(WifiError):
        host_key_from_dropbear(dropbear.replace(b"ssh-ed25519", b"ssh-ed25518"))


def test_port_from_koreaders_settings() -> None:
    assert ssh_port(None) == 2222
    for value, port in (('"2223"', 2223), ("22", 22), ('"x"', 2222), ("70000", 2222)):
        assert ssh_port(SettingsDocument.parse(f"return {{ SSH_port = {value} }}")) == port


@pytest.mark.parametrize("host", ["8.8.8.8", "reader.local", "192.168.1.300", ""])
def test_only_local_ip_addresses(host: str) -> None:
    with pytest.raises(WifiError):
        check_host(host)
    assert check_host(" 192.168.1.23 ") == "192.168.1.23"
    assert check_host("fe80::1") == "fe80::1"


def test_pairing_adds_one_line_inside_a_snapshot(
    reader: Path, store: SnapshotStore, keys: KeyStore
) -> None:
    keys_file = reader / AUTHORIZED_KEYS
    keys_file.parent.mkdir(exist_ok=True)
    keys_file.write_bytes(OTHER)
    host_key, dropbear = new_host_key()
    (reader / HOST_KEY_FILE).write_bytes(dropbear)
    assert "stay exactly" in " ".join(pair_lines(reader))
    paired = _paired(reader, store, keys, 2223)
    assert paired.port == 2223 and paired.host_key == host_key.get_base64()
    assert keys.load(ID) == paired and keys.pairings() == [paired]
    assert keys_file.read_bytes().startswith(OTHER)
    assert keys_file.read_bytes().count(b"\n") == OTHER.count(b"\n") + 1
    assert len(store.snapshots()) == 1
    _paired(reader, store, keys)  # pairing again changes nothing on the reader
    assert len(store.snapshots()) == 1
    unpair(store, reader, keys, ID)
    assert keys_file.read_bytes() == OTHER
    assert keys.load(ID) is None and not (keys.base / ID).exists()


def test_forgotten_without_the_reader_the_line_comes_off_later(
    reader: Path, store: SnapshotStore, keys: KeyStore
) -> None:
    """Forget while the reader is away: only the public line stays, for the next USB connect."""
    _paired(reader, store, keys)
    keys.forget(ID, keep_line=True)
    assert keys.load(ID) is None and keys.pairings() == []
    assert not (keys.base / ID / "id_ed25519").exists() and keys.leftover(ID) is not None
    assert b"kodesk-" in (reader / AUTHORIZED_KEYS).read_bytes()
    unpair(store, reader, keys, ID)
    assert (reader / AUTHORIZED_KEYS).read_bytes() == b""
    assert keys.leftover(ID) is None


def test_connect_pins_the_host_key_on_first_contact(
    reader: Path, store: SnapshotStore, keys: KeyStore, tmp_path: Path
) -> None:
    host_key, _dropbear = new_host_key()
    paired = _paired(reader, store, keys)
    assert paired.host_key is None  # dropbear has not made its key yet
    fake = FakeReader(tmp_path / "served", reader / AUTHORIZED_KEYS, host_key)
    (fake.root / "koreader").mkdir(parents=True)
    with serve(fake) as port:
        paired = Pairing(ID, paired.name, port, paired.user)
        with connect(keys, paired, "127.0.0.1") as first:
            assert first.pinned and first.sftp.listdir("/") == ["koreader"]
        saved = keys.load(ID)
        assert saved is not None and saved.host_key == host_key.get_base64()
        assert saved.host == "127.0.0.1"
        with connect(keys, saved, "127.0.0.1") as second:
            assert not second.pinned
    assert fake.logins == ["publickey", "publickey"]


def test_another_host_key_is_refused(
    reader: Path, store: SnapshotStore, keys: KeyStore, tmp_path: Path
) -> None:
    host_key, _dropbear = new_host_key()
    other, _ = new_host_key()
    _paired(reader, store, keys)
    fake = FakeReader(tmp_path, reader / AUTHORIZED_KEYS, other)
    with serve(fake) as port:
        pinned = Pairing(ID, "PB", port, host_key=host_key.get_base64())
        with (
            pytest.raises(HostKeyChangedError, match="pair it again over USB"),
            connect(keys, pinned, "127.0.0.1"),
        ):
            pass
    assert not fake.logins  # nothing was sent before the key was checked


def test_a_server_without_password_is_refused(
    reader: Path, store: SnapshotStore, keys: KeyStore, tmp_path: Path
) -> None:
    host_key, _dropbear = new_host_key()
    _paired(reader, store, keys)
    fake = FakeReader(tmp_path, reader / AUTHORIZED_KEYS, host_key, no_password=True)
    with (
        serve(fake) as port,
        pytest.raises(WifiError, match="Login with key only"),
        connect(keys, Pairing(ID, "PB", port), "127.0.0.1"),
    ):
        pass
    assert keys.load(ID) is not None and keys.load(ID).host_key is None  # type: ignore[union-attr]


def test_a_reader_without_our_key_is_refused(
    reader: Path, store: SnapshotStore, keys: KeyStore, tmp_path: Path
) -> None:
    host_key, _dropbear = new_host_key()
    _paired(reader, store, keys)
    fake = FakeReader(tmp_path, tmp_path / "empty_authorized_keys", host_key)
    with (
        serve(fake) as port,
        pytest.raises(WifiError, match="Pair it again over USB"),
        connect(keys, Pairing(ID, "PB", port), "127.0.0.1"),
    ):
        pass


def test_no_answer_says_what_to_check(reader: Path, store: SnapshotStore, keys: KeyStore) -> None:
    paired = _paired(reader, store, keys)
    with serve(FakeReader(reader, reader / AUTHORIZED_KEYS, new_host_key()[0])) as port:
        closed = port
    with (
        pytest.raises(WifiError, match="SSH server started"),
        connect(keys, Pairing(ID, paired.name, closed), "127.0.0.1", timeout=2),
    ):
        pass
