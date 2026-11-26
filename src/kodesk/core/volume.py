"""A stable serial for the reader's USB volume, so two identical readers never share snapshots.

PocketBooks name their volume after the model (e.g. ``PB634K3``), so the label alone does not
tell two readers apart. The FAT volume serial (``1234-ABCD``, shown as the UUID) does; it only
changes when the reader is formatted. Read-only; nothing is written to the reader.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess  # fixed argument list, never a shell
import sys
from pathlib import Path

BY_UUID = Path("/dev/disk/by-uuid")
_SERIAL = re.compile(r"^[A-Za-z0-9-]{4,64}$")
_WINDOWS_VOLUME = re.compile(r"Volume\{(?P<guid>[0-9A-Fa-f-]{36})\}")
DISKUTIL = "/usr/sbin/diskutil"  # absolute: never whatever PATH finds first
DISKUTIL_TIMEOUT = 5


def _clean(value: object) -> str | None:
    return value if isinstance(value, str) and _SERIAL.match(value) else None


def linux_serial(device: str, by_uuid: Path = BY_UUID) -> str | None:
    """The UUID whose ``/dev/disk/by-uuid`` link points at ``device`` (e.g. ``/dev/sde``)."""
    try:
        target = os.path.realpath(device)
        for link in by_uuid.iterdir():
            if os.path.realpath(link) == target:
                return _clean(link.name)
    except OSError:
        return None
    return None


def windows_serial(device: str) -> str | None:
    """Qt names a Windows volume ``\\\\?\\Volume{GUID}\\``; the GUID is stable per volume."""
    match = _WINDOWS_VOLUME.search(device)
    return match["guid"].lower() if match else None


def macos_serial(device: str) -> str | None:
    """``diskutil info -plist`` → ``VolumeUUID`` (derived from the FAT serial on vfat)."""
    if not device.startswith("/dev/disk"):
        return None
    try:
        done = subprocess.run(  # noqa: S603  # fixed program, device path as one argument
            [DISKUTIL, "info", "-plist", device],
            capture_output=True,
            timeout=DISKUTIL_TIMEOUT,
            check=True,
        )
        info = plistlib.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return None
    return _clean(info.get("VolumeUUID")) if isinstance(info, dict) else None


def volume_serial(device: str, platform: str = sys.platform) -> str | None:
    """Serial of the volume on ``device`` (as Qt's ``QStorageInfo.device()`` names it)."""
    if platform.startswith("linux"):
        return linux_serial(device)
    if platform == "win32":
        return windows_serial(device)
    if platform == "darwin":
        return macos_serial(device)
    return None
