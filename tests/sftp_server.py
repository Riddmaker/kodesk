"""A local SSH/SFTP server for the Wi-Fi tests (B-11): paramiko on 127.0.0.1, never the reader.

It behaves like KOReader's dropbear as far as kodesk cares: an ed25519 host key, key login
against an authorized_keys file, optionally KOReader's "no password" mode (any password, even
an empty one, logs in), and read-only SFTP of one folder.
"""

from __future__ import annotations

import base64
import os
import socket
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from paramiko.common import (
    AUTH_FAILED,
    AUTH_SUCCESSFUL,
    OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
    OPEN_SUCCEEDED,
)


def new_host_key() -> tuple[paramiko.Ed25519Key, bytes]:
    """A host key, and the same key in dropbear's file format (as on the reader)."""
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    seed = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    name = b"ssh-ed25519"
    dropbear = struct.pack(">I", len(name)) + name + struct.pack(">I", 64) + seed + public
    return paramiko.Ed25519Key(file_obj=StringIO(pem)), dropbear


@dataclass
class FakeReader:
    """The server's state; tests change it between connections."""

    root: Path  # what SFTP serves
    authorized_keys: Path
    host_key: paramiko.Ed25519Key
    user: str = "reader"
    no_password: bool = False  # KOReader's "allow no password" (-n)
    logins: list[str] = field(default_factory=list)

    def allowed(self) -> set[str]:
        if not self.authorized_keys.is_file():
            return set()
        lines = self.authorized_keys.read_text(encoding="utf-8").splitlines()
        return {line.split()[1] for line in lines if line.startswith("ssh-ed25519 ")}


class _Server(paramiko.ServerInterface):
    def __init__(self, reader: FakeReader) -> None:
        self.reader = reader

    def get_allowed_auths(self, username: str) -> str:
        return "publickey,password" if self.reader.no_password else "publickey"

    def check_auth_password(self, username: str, password: str) -> int:
        if self.reader.no_password and username == self.reader.user:
            self.reader.logins.append("password")
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        if username == self.reader.user and key.get_base64() in self.reader.allowed():
            self.reader.logins.append("publickey")
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return OPEN_SUCCEEDED
        return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _ReadOnlySftp(paramiko.SFTPServerInterface):
    def __init__(self, server: _Server, *args: object, **kwargs: object) -> None:
        super().__init__(server, *args, **kwargs)
        self.root = server.reader.root

    def _local(self, path: str) -> Path:
        local = (self.root / path.lstrip("/")).resolve()
        if not local.is_relative_to(self.root.resolve()):
            raise PermissionError(path)
        return local

    def canonicalize(self, path: str) -> str:
        return "/" + os.path.normpath("/" + path).lstrip("/")

    def list_folder(self, path: str) -> list[paramiko.SFTPAttributes] | int:
        try:
            folder = self._local(path)
            return [
                paramiko.SFTPAttributes.from_stat(child.stat(), child.name)
                for child in sorted(folder.iterdir())
            ]
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno or 2)

    def stat(self, path: str) -> paramiko.SFTPAttributes | int:
        try:
            return paramiko.SFTPAttributes.from_stat(self._local(path).stat())
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno or 2)

    lstat = stat


@contextmanager
def serve(reader: FakeReader) -> Iterator[int]:
    """Accept connections on a free port of 127.0.0.1 until the block ends; yields the port."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    transports: list[paramiko.Transport] = []

    def accept() -> None:
        while not stop.is_set():
            try:
                client, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            transport = paramiko.Transport(client)
            transport.add_server_key(reader.host_key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, _ReadOnlySftp)
            transports.append(transport)
            with suppress(paramiko.SSHException, EOFError, OSError):
                transport.start_server(server=_Server(reader))

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        listener.close()
        for transport in transports:
            transport.close()
        thread.join(timeout=5)


def public_base64(key: paramiko.PKey) -> str:
    return base64.b64encode(key.asbytes()).decode()
