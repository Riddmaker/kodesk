"""Unpack a downloaded plugin archive safely on the PC (B-08). Nothing here touches the reader.

A plugin zip is third-party data. It is opened in memory and only the one plugin folder in it
is written, file by file, into a staging folder on the PC; :mod:`kodesk.core.installer` copies
from there. Refused: paths that leave the folder (zip-slip), symbolic links and device files,
names the reader's FAT file system cannot hold or would merge (case), more than
:data:`MAX_MEMBERS` entries or :data:`MAX_UNPACKED` bytes (zip bomb), and archives without
exactly one folder holding ``main.lua`` and ``_meta.lua``.

Layouts seen on GitHub (2026-11): a release asset holds ``x.koplugin/…``; a codeload archive
holds ``<repo>-<ref>/…`` with the plugin at its root, or ``<repo>-<ref>/x.koplugin/…``.
"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_MEMBERS = 5000
MAX_UNPACKED = 200 * 2**20
CHUNK = 1 << 20
PLUGIN_SUFFIX = ".koplugin"
_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# Characters FAT (the reader's file system) cannot store, plus control characters.
_FAT_FORBIDDEN = re.compile(r'[<>:"|?*\\\x00-\x1f]')


class ArchiveError(Exception):
    """The archive is not a single, safe KOReader plugin."""


@dataclass(frozen=True)
class StagedPlugin:
    """One plugin folder unpacked on the PC, ready to copy to the reader."""

    name: str  # folder name on the reader without ``.koplugin``
    folder: Path  # the unpacked ``<name>.koplugin`` folder in the staging dir
    files: Mapping[str, str]  # path inside the folder -> SHA-256

    @property
    def folder_name(self) -> str:
        return self.name + PLUGIN_SUFFIX


def safe_name(name: str) -> str:
    """A single file or folder name the reader can store; raises otherwise."""
    problems = (
        name in {"", ".", ".."},
        bool(_FAT_FORBIDDEN.search(name)) or "/" in name,
        name != name.strip() or name.endswith("."),
        len(name.encode()) > 255,
    )
    if any(problems):
        raise ArchiveError(f"unsafe name in the archive: {name!r}")
    return name


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename.rstrip("/") if info.is_dir() else info.filename
    path = PurePosixPath(name)
    if not name or path.is_absolute() or re.match(r"^[A-Za-z]:", name):
        raise ArchiveError(f"absolute path in the archive: {info.filename!r}")
    for part in name.split("/"):
        safe_name(part)
    return path


def _check_type(info: zipfile.ZipInfo) -> None:
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ArchiveError(f"symbolic link in the archive: {info.filename!r}")
    if mode and stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ArchiveError(f"special file in the archive: {info.filename!r}")


def _checked_members(archive: zipfile.ZipFile) -> dict[PurePosixPath, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ArchiveError(f"more than {MAX_MEMBERS} files in the archive")
    files: dict[PurePosixPath, zipfile.ZipInfo] = {}
    seen: set[str] = set()
    total = 0
    for info in infos:
        path = _member_path(info)
        _check_type(info)
        folded = path.as_posix().casefold()
        if not info.is_dir():
            if folded in seen:
                raise ArchiveError(f"two files differ only in case: {info.filename!r}")
            seen.add(folded)
            files[path] = info
            total += info.file_size
    if total > MAX_UNPACKED:
        raise ArchiveError(f"more than {MAX_UNPACKED // 2**20} MB unpacked")
    return files


def _plugin_root(files: Mapping[PurePosixPath, zipfile.ZipInfo]) -> PurePosixPath:
    names = set(files)
    roots = sorted(
        path.parent
        for path in names
        if path.name == "main.lua" and path.parent / "_meta.lua" in names
    )
    if not roots:
        raise ArchiveError("this is not a KOReader plugin (no folder with main.lua and _meta.lua)")
    if len(roots) > 1:
        found = ", ".join(r.as_posix() or "(top)" for r in roots)
        raise ArchiveError(f"several plugins in one archive: {found}")
    return roots[0]


def plugin_name(root: PurePosixPath, repo: str) -> str:
    """``x`` for a ``x.koplugin`` folder; else the repo name (``x.koplugin`` or ``x``)."""
    name = root.name if root.name.endswith(PLUGIN_SUFFIX) else repo
    name = name.removesuffix(PLUGIN_SUFFIX)
    if not _PLUGIN_NAME.match(name) or name.startswith("."):
        raise ArchiveError(f"unusable plugin folder name: {name!r}")
    return name


def stage_plugin(data: bytes, repo: str, staging: Path) -> StagedPlugin:
    """Unpack the one plugin in ``data`` into ``staging/<name>.koplugin`` (a new folder)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = _checked_members(archive)
            root = _plugin_root(files)
            name = plugin_name(root, repo)
            folder = staging / (name + PLUGIN_SUFFIX)
            folder.mkdir(parents=True)
            hashes: dict[str, str] = {}
            budget = MAX_UNPACKED
            for path, info in sorted(files.items()):
                if root.parts and path.parts[: len(root.parts)] != root.parts:
                    continue
                relative = PurePosixPath(*path.parts[len(root.parts) :]).as_posix()
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                sha = hashlib.sha256()
                with archive.open(info) as source, target.open("xb") as out:
                    while chunk := source.read(CHUNK):
                        budget -= len(chunk)
                        if budget < 0:  # sizes in the header can lie
                            raise ArchiveError(f"more than {MAX_UNPACKED // 2**20} MB unpacked")
                        sha.update(chunk)
                        out.write(chunk)
                hashes[relative] = sha.hexdigest()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as error:
        raise ArchiveError(f"the download is not a readable zip archive ({error})") from error
    except (FileExistsError, NotADirectoryError) as error:  # a file and a folder of one name
        raise ArchiveError(f"conflicting names in the archive ({error.filename})") from error
    return StagedPlugin(name, folder, hashes)
