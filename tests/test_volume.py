"""Volume serial per platform (never the real reader; fake links and a fake diskutil)."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from kodesk.core import volume


def test_linux_finds_the_uuid_link(tmp_path: Path) -> None:
    device = tmp_path / "sde"
    device.write_text("")
    links = tmp_path / "by-uuid"
    links.mkdir()
    (links / "AAAA-1111").symlink_to(tmp_path / "other")
    (links / "1234-ABCD").symlink_to(device)
    assert volume.linux_serial(str(device), links) == "1234-ABCD"
    assert volume.linux_serial(str(tmp_path / "none"), links) is None
    assert volume.linux_serial(str(device), tmp_path / "missing") is None


def test_odd_link_names_are_ignored(tmp_path: Path) -> None:
    device = tmp_path / "sde"
    device.write_text("")
    links = tmp_path / "by-uuid"
    links.mkdir()
    (links / "a b;rm").symlink_to(device)
    assert volume.linux_serial(str(device), links) is None


def test_windows_uses_the_volume_guid() -> None:
    guid = "0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0"
    assert volume.volume_serial(f"\\\\?\\Volume{{{guid}}}\\", "win32") == guid.lower()
    assert volume.volume_serial("E:\\", "win32") is None


def test_macos_reads_diskutil(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        body = plistlib.dumps({"VolumeUUID": "0E1F2A3B-0000-0000-0000-000000000000"})
        return subprocess.CompletedProcess(command, 0, body, b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert volume.volume_serial("/dev/disk4s1", "darwin") == "0E1F2A3B-0000-0000-0000-000000000000"
    assert calls[0] == ["/usr/sbin/diskutil", "info", "-plist", "/dev/disk4s1"]
    assert volume.volume_serial("-rf", "darwin") is None and len(calls) == 1  # never an option


def test_macos_without_diskutil(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("diskutil")

    monkeypatch.setattr(subprocess, "run", run)
    assert volume.volume_serial("/dev/disk4s1", "darwin") is None
    assert volume.volume_serial("/dev/sde", "freebsd") is None
