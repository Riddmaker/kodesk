"""Wi-Fi (B-11): pair a reader once over USB, then reach it over SFTP; no Qt.

KOReader's SSH plugin starts a patched dropbear from the KOReader folder (SSH.koplugin/main.lua);
it reads keys from ``settings/SSH/authorized_keys`` and keeps its host keys beside them
(``settings/SSH/dropbear_*_host_key``, created on the first connection; koreader-base
thirdparty/dropbear; docs/research/wifi-transport). SFTP is OpenSSH's ``sftp-server``.

Pairing (over USB, inside a snapshot) appends one line, kodesk's own ed25519 key for this
reader, to ``authorized_keys``; every other byte stays. The private key stays on this computer
(0600, in kodesk's data folder, no passphrase, like ``~/.ssh``). The reader's host key is read
over USB when dropbear has made it, else pinned on the first connection (TOFU). A connection
then accepts only that host key and only a key login: a server that lets anyone in with an
empty password (KOReader's "no password" option) is refused, as is a host that is not on the
local network.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date
from io import StringIO
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kodesk.core.luadump import SettingsDocument
from kodesk.core.snapshot import Progress, Snapshot, SnapshotStore, guarded_write, no_progress

AUTHORIZED_KEYS = "settings/SSH/authorized_keys"
HOST_KEY_FILE = "settings/SSH/dropbear_ed25519_host_key"
PORT_KEY = "SSH_port"  # settings.reader.lua; KOReader's default below (main.lua SSH:init)
KEY_ONLY_KEY = "SSH_key_only_auth"
DEFAULT_PORT = 2222
DEFAULT_USER = "reader"  # PocketBook (KOReader wiki); Kobo and Kindle use root
KEY_TYPE = "ssh-ed25519"
TIMEOUT = 10.0
TEMP_SUFFIX = ".kodesk-tmp"
FORGOTTEN = "forgotten.pub"  # public line of a pairing forgotten without the reader
_READER_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_ED25519_LEN = 32


class WifiError(Exception):
    """A Wi-Fi step failed; the message is for the user."""


class HostKeyChangedError(WifiError):
    """The reader answered with another host key than the pinned one."""


@dataclass(frozen=True)
class Pairing:
    """What kodesk keeps about one paired reader (no secrets: the key is a separate file)."""

    reader_id: str
    name: str  # shown to the user, e.g. "PocketBook Verse Pro"
    port: int = DEFAULT_PORT
    user: str = DEFAULT_USER
    host_key: str | None = None  # base64 of the ed25519 public key blob; None: pin on contact
    host: str = ""  # the IP address used last
    koreader_path: str = ""  # the KOReader folder as the reader names it (B-27); "": unknown


def check_reader_id(reader_id: str) -> str:
    if not _READER_ID.match(reader_id) or reader_id.strip(".") == "":
        raise ValueError(f"not a reader id: {reader_id!r}")
    return reader_id


def check_host(host: str) -> str:
    """Only an IP address on the local network (the reader shows it when SSH starts)."""
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError as error:
        raise WifiError(f"“{host}” is not an IP address like 192.168.1.23.") from error
    if not (address.is_private or address.is_link_local or address.is_loopback):
        raise WifiError(f"{address} is not on your local network; kodesk connects only there.")
    return str(address)


def check_user(user: str) -> str:
    if not _USER.match(user):
        raise WifiError(f"“{user}” is not a user name like reader or root.")
    return user


def ssh_port(settings: SettingsDocument | None) -> int:
    """The port KOReader's SSH server listens on (it stores a string or a number)."""
    value = settings.data.get(PORT_KEY) if settings is not None else None
    try:
        port = int(value) if isinstance(value, (int, float, str)) else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT
    return port if 0 < port < 65536 else DEFAULT_PORT


def key_only(settings: SettingsDocument | None) -> bool:
    return settings is not None and settings.data.get(KEY_ONLY_KEY) is True


# -- keys -----------------------------------------------------------------------------------


def _blob(line: str) -> str | None:
    """The base64 key of an authorized_keys line (options and comment ignored), if ed25519."""
    parts = line.split()
    for index, part in enumerate(parts[:-1]):
        if part == KEY_TYPE:
            return parts[index + 1]
    return None


def with_key(text: bytes, line: str) -> bytes:
    """``text`` (an authorized_keys file) with ``line`` added once; other bytes unchanged."""
    blob = _blob(line)
    if any(_blob(old) == blob for old in text.decode(errors="replace").splitlines()):
        return text
    if text and not text.endswith(b"\n"):
        text += b"\n"
    return text + line.encode() + b"\n"


def without_key(text: bytes, line: str) -> bytes:
    """``text`` without the lines holding ``line``'s key; other bytes unchanged."""
    blob = _blob(line)
    kept = [
        old for old in text.splitlines(keepends=True) if _blob(old.decode(errors="replace")) != blob
    ]
    return b"".join(kept)


def host_key_from_dropbear(data: bytes) -> str:
    """The public half of dropbear's ed25519 host key file, as base64 (the private half is never
    kept). Format: string "ssh-ed25519", uint32 64, 32 bytes private, 32 bytes public
    (dropbear ed25519.c buf_put_ed25519_priv_key)."""
    name = KEY_TYPE.encode()
    header = struct.pack(">I", len(name)) + name + struct.pack(">I", 2 * _ED25519_LEN)
    if len(data) != len(header) + 2 * _ED25519_LEN or not data.startswith(header):
        raise WifiError("The reader's host key file has an unexpected format.")
    public = data[len(header) + _ED25519_LEN :]
    blob = struct.pack(">I", len(name)) + name + struct.pack(">I", _ED25519_LEN) + public
    return base64.b64encode(blob).decode()


class KeyStore:
    """Pairings and keys in ``base`` (kodesk's data folder), one folder per reader (0700)."""

    def __init__(self, base: Path) -> None:
        self.base = base

    def _folder(self, reader_id: str) -> Path:
        return self.base / check_reader_id(reader_id)

    def pairings(self) -> list[Pairing]:
        """Every paired reader, by name."""
        found = []
        if self.base.is_dir():
            for folder in sorted(self.base.iterdir()):
                pairing = self.load(folder.name) if _READER_ID.match(folder.name) else None
                if pairing is not None:
                    found.append(pairing)
        return sorted(found, key=lambda p: p.name)

    def load(self, reader_id: str) -> Pairing | None:
        """The pairing of this reader; None if not paired (or its files are damaged)."""
        path = self._folder(reader_id) / "pairing.json"
        if not path.is_file() or not (self._folder(reader_id) / "id_ed25519").is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pairing = Pairing(**{k: data[k] for k in asdict(Pairing("x", "x")) if k in data})
        except (ValueError, TypeError, KeyError):
            return None
        return pairing if pairing.reader_id == reader_id else None

    def save(self, pairing: Pairing) -> None:
        folder = self._folder(pairing.reader_id)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = folder / ("pairing.json" + TEMP_SUFFIX)
        temp.write_text(json.dumps(asdict(pairing), indent=2) + "\n", encoding="utf-8")
        temp.replace(folder / "pairing.json")

    def public_line(self, reader_id: str) -> str:
        """kodesk's key for this reader as an authorized_keys line; made on first use."""
        folder = self._folder(reader_id)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
        path = folder / "id_ed25519"
        if not path.exists():
            private = Ed25519PrivateKey.generate()
            data = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.OpenSSH,
                serialization.NoEncryption(),
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags, 0o600)  # never readable by others, not even briefly
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
        key = self.private_key(reader_id)
        return f"{KEY_TYPE} {key.get_base64()} kodesk-{date.today().isoformat()}"

    def private_key(self, reader_id: str) -> paramiko.Ed25519Key:
        path = self._folder(reader_id) / "id_ed25519"
        return paramiko.Ed25519Key(file_obj=StringIO(path.read_text(encoding="ascii")))

    def forget(self, reader_id: str, keep_line: bool = False) -> None:
        """Delete the key and the pin of this reader. ``keep_line`` keeps only the public line,
        so kodesk can take it off the reader the next time it is plugged in."""
        folder = self._folder(reader_id)
        if keep_line and self.load(reader_id) is not None:
            (folder / FORGOTTEN).write_text(self.public_line(reader_id) + "\n", encoding="ascii")
            for name in ("id_ed25519", "pairing.json"):
                (folder / name).unlink()
        elif folder.is_dir():
            shutil.rmtree(folder)

    def leftover(self, reader_id: str) -> str | None:
        """The public line of a forgotten pairing whose line may still be on the reader."""
        path = self._folder(reader_id) / FORGOTTEN
        return path.read_text(encoding="ascii").strip() if path.is_file() else None


# -- pairing over USB -------------------------------------------------------------------------


def pair_lines(koreader_dir: Path) -> list[str]:
    """What pairing changes on the reader, for the preview."""
    exists = (koreader_dir / AUTHORIZED_KEYS).is_file()
    lines = [f"{AUTHORIZED_KEYS}: add one line, kodesk's key for this reader"]
    if exists:
        lines.append("(the lines already there stay exactly as they are)")
    else:
        lines.append("(the file is new)")
    return lines


def _replace_keys(koreader_dir: Path, new: bytes) -> None:
    target = koreader_dir / AUTHORIZED_KEYS
    target.parent.mkdir(exist_ok=True)
    temp = target.with_name(target.name + TEMP_SUFFIX)
    temp.write_bytes(new)  # vfat: write beside the target, then replace
    temp.replace(target)
    if target.read_bytes() != new:
        raise OSError(f"{AUTHORIZED_KEYS} reads back differently after writing")


def pair(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: SnapshotStore,
    koreader_dir: Path,
    keys: KeyStore,
    pairing: Pairing,
    settings: SettingsDocument | None,
    progress: Progress = no_progress,
) -> tuple[Pairing, Snapshot | None]:
    """Add kodesk's key to the reader (snapshot first) and remember the reader."""
    line = keys.public_line(pairing.reader_id)
    target = koreader_dir / AUTHORIZED_KEYS
    old = target.read_bytes() if target.is_file() else b""
    new = with_key(old, line)
    snapshot = None
    if new != old:
        snapshot = guarded_write(
            store,
            koreader_dir,
            lambda: _replace_keys(koreader_dir, new),
            allowed={AUTHORIZED_KEYS},
            progress=progress,
        )
    host_file = koreader_dir / HOST_KEY_FILE
    host_key = host_key_from_dropbear(host_file.read_bytes()) if host_file.is_file() else None
    paired = Pairing(
        pairing.reader_id,
        pairing.name,
        ssh_port(settings),
        pairing.user,
        host_key or pairing.host_key,
        pairing.host,
        pairing.koreader_path,
    )
    keys.save(paired)
    return paired, snapshot


def unpair(
    store: SnapshotStore,
    koreader_dir: Path,
    keys: KeyStore,
    reader_id: str,
    progress: Progress = no_progress,
) -> Snapshot | None:
    """Remove kodesk's line from the reader (snapshot first), then forget key and pin."""
    snapshot = None
    line = keys.public_line(reader_id) if keys.load(reader_id) else keys.leftover(reader_id)
    if line is not None:
        target = koreader_dir / AUTHORIZED_KEYS
        old = target.read_bytes() if target.is_file() else b""
        new = without_key(old, line)
        if new != old:
            snapshot = guarded_write(
                store,
                koreader_dir,
                lambda: _replace_keys(koreader_dir, new),
                allowed={AUTHORIZED_KEYS},
                progress=progress,
            )
    keys.forget(reader_id)
    return snapshot


# -- connecting over Wi-Fi --------------------------------------------------------------------


@dataclass
class Connection:
    """An open, verified SFTP session; ``pinned`` is True when this contact pinned the key."""

    sftp: paramiko.SFTPClient
    pairing: Pairing
    pinned: bool


def _refuse_empty_password(transport: paramiko.Transport, user: str) -> None:
    """KOReader's "no password" option lets anyone log in; kodesk will not use such a server."""
    try:
        transport.auth_password(user, "", fallback=False)
    except paramiko.AuthenticationException:  # includes BadAuthenticationType: good
        return
    raise WifiError(
        "Your reader lets anyone log in over Wi-Fi without a password. On the reader, switch off "
        "“Login without password” and switch on “Login with key only” (KOReader: Network → "
        "SSH server), restart the SSH server, and try again."
    )


@contextmanager
def connect(
    keys: KeyStore, pairing: Pairing, host: str, timeout: float = TIMEOUT
) -> Iterator[Connection]:
    """Open SFTP to a paired reader: pinned host key, key login only. Pins on first contact."""
    address = check_host(host)
    user = check_user(pairing.user)
    key = keys.private_key(pairing.reader_id)
    try:
        sock = socket.create_connection((address, pairing.port), timeout=timeout)
    except OSError as error:
        raise WifiError(
            f"No answer from {address}:{pairing.port}. Is the reader on the same Wi-Fi and "
            "its SSH server started (KOReader: Network → SSH server)?"
        ) from error
    transport = paramiko.Transport(sock)
    try:
        transport.get_security_options().key_types = (KEY_TYPE,)
        transport.start_client(timeout=timeout)
        seen = transport.get_remote_server_key().get_base64()
        if pairing.host_key is not None and seen != pairing.host_key:
            raise HostKeyChangedError(
                f"The device at {address} is not the reader you paired (its key differs). "
                "If you reset the reader, pair it again over USB; otherwise do not connect."
            )
        _refuse_empty_password(transport, user)
        try:
            transport.auth_publickey(user, key)
        except paramiko.AuthenticationException as error:
            raise WifiError(
                "The reader does not accept kodesk's key. Pair it again over USB."
            ) from error
        pinned = pairing.host_key is None
        current = replace(pairing, user=user, host_key=seen, host=address)
        if current != pairing:  # pinned now, or another address, port or user name
            keys.save(current)
        sftp = paramiko.SFTPClient.from_transport(transport)
        if sftp is None:
            raise WifiError("The reader's SSH server offers no SFTP.")
        with sftp:
            yield Connection(sftp, current, pinned)
    except paramiko.SSHException as error:
        raise WifiError(f"The connection to {address} failed: {error}") from error
    finally:
        transport.close()
