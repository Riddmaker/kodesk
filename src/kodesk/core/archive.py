"""Unpack downloaded archives safely on the PC (B-06, B-08). Nothing here touches the reader.

A plugin zip is third-party data. It is opened in memory and only the one plugin folder in it
is written, file by file, into a staging folder on the PC; :mod:`kodesk.core.installer` copies
from there. Refused: paths that leave the folder (zip-slip), symbolic links and device files,
names the reader's FAT file system cannot hold or would merge (case), more than
:data:`MAX_MEMBERS` entries or :data:`MAX_UNPACKED` bytes (zip bomb), and archives without
exactly one folder holding ``main.lua`` and ``_meta.lua``.

Layouts seen on GitHub (2026-11): a release asset holds ``x.koplugin/…``; a codeload archive
holds ``<repo>-<ref>/…`` with the plugin at its root, or ``<repo>-<ref>/x.koplugin/…``.

A KOReader release zip (:func:`stage_release`) is unpacked whole, with the same checks, but only
the paths its caller accepts (e.g. ``applications/koreader/`` on a PocketBook); its own file list
``ota/package.index`` must name exactly the files in the archive.
"""

from __future__ import annotations

import hashlib
import io
import re
import stat
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_MEMBERS = 5000
MAX_UNPACKED = 200 * 2**20
RELEASE_MAX_UNPACKED = 600 * 2**20  # the PocketBook release unpacks to 87 MB (v2026.07.1)
INDEX = "ota/package.index"
CHUNK = 1 << 20
PLUGIN_SUFFIX = ".koplugin"
_PLUGIN_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# Characters FAT (the reader's file system) cannot store, plus control characters.
_FAT_FORBIDDEN = re.compile(r'[<>:"|?*\\\x00-\x1f]')


class ArchiveError(Exception):
    """The archive is not what it should be (one safe plugin, or a KOReader release)."""


@dataclass(frozen=True)
class StagedPlugin:
    """One plugin folder unpacked on the PC, ready to copy to the reader."""

    name: str  # folder name on the reader without ``.koplugin``
    folder: Path  # the unpacked ``<name>.koplugin`` folder in the staging dir
    files: Mapping[str, str]  # path inside the folder -> SHA-256

    @property
    def folder_name(self) -> str:
        return self.name + PLUGIN_SUFFIX


@dataclass(frozen=True)
class StagedRelease:
    """A KOReader release unpacked on the PC, laid out as on the reader's volume."""

    folder: Path  # the staging folder; files at their volume-relative paths below it
    files: Mapping[str, str]  # volume-relative path -> SHA-256
    sizes: Mapping[str, int]  # volume-relative path -> bytes


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


def _checked_members(
    archive: zipfile.ZipFile, max_unpacked: int = MAX_UNPACKED
) -> dict[PurePosixPath, zipfile.ZipInfo]:
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
    if total > max_unpacked:
        raise ArchiveError(f"more than {max_unpacked // 2**20} MB unpacked")
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


class _Budget:
    """Bytes still allowed to be unpacked; the sizes in a zip's header can lie."""

    def __init__(self, limit: int) -> None:
        self.limit = self.left = limit

    def take(self, size: int) -> None:
        self.left -= size
        if self.left < 0:
            raise ArchiveError(f"more than {self.limit // 2**20} MB unpacked")


def _copy(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path | None, budget: _Budget
) -> str:
    """Unpack one member to the new file ``target`` (or only hash it); its SHA-256."""
    sha = hashlib.sha256()
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info) as source, target.open("xb") if target else io.BytesIO() as out:
        while chunk := source.read(CHUNK):
            budget.take(len(chunk))
            sha.update(chunk)
            if target is not None:
                out.write(chunk)
    return sha.hexdigest()


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
            budget = _Budget(MAX_UNPACKED)
            for path, info in sorted(files.items()):
                if root.parts and path.parts[: len(root.parts)] != root.parts:
                    continue
                relative = PurePosixPath(*path.parts[len(root.parts) :]).as_posix()
                hashes[relative] = _copy(archive, info, folder / relative, budget)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as error:
        raise ArchiveError(f"the download is not a readable zip archive ({error})") from error
    except (FileExistsError, NotADirectoryError) as error:  # a file and a folder of one name
        raise ArchiveError(f"conflicting names in the archive ({error.filename})") from error
    return StagedPlugin(name, folder, hashes)


def index_paths(text: str) -> list[str]:
    """The paths in a KOReader ``ota/package.index``, relative to the volume.

    Older indexes start each line with ``../``; KOReader's own update strips it the same way
    (``sed 's,^../,,'`` in ``platform/pocketbook/koreader.app``).
    """
    paths = []
    for line in text.splitlines():
        path = line.strip().removeprefix("../")
        if not path:
            continue
        if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ArchiveError(f"unsafe path in {INDEX}: {line[:80]!r}")
        for part in path.split("/"):
            safe_name(part)
        paths.append(path)
    return paths


def _release_members(
    archive: zipfile.ZipFile, accepted: tuple[str, ...], skipped: tuple[str, ...]
) -> Iterator[tuple[str, zipfile.ZipInfo]]:
    """Members a caller accepts (prefix ``x/`` or exact name); skipped ones are left out;
    anything else in the archive refuses it."""
    for path, info in sorted(_checked_members(archive, RELEASE_MAX_UNPACKED).items()):
        name = path.as_posix()
        if name.startswith(skipped):
            continue
        if not any(name == a or (a.endswith("/") and name.startswith(a)) for a in accepted):
            raise ArchiveError(f"unexpected file in the KOReader release: {name!r}")
        yield name, info


def stage_release(
    archive_path: Path, staging: Path, accepted: tuple[str, ...], skipped: tuple[str, ...]
) -> StagedRelease:
    """Unpack the release at ``archive_path`` into the new folder ``staging`` and check that
    its ``ota/package.index`` names exactly its files."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            staging.mkdir(parents=True)
            budget = _Budget(RELEASE_MAX_UNPACKED)
            hashes: dict[str, str] = {}
            sizes: dict[str, int] = {}
            for name, info in _release_members(archive, accepted, skipped):
                hashes[name] = _copy(archive, info, staging / name, budget)
                sizes[name] = (staging / name).stat().st_size
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as error:
        raise ArchiveError(f"the download is not a readable zip archive ({error})") from error
    except (FileExistsError, NotADirectoryError) as error:
        raise ArchiveError(f"conflicting names in the archive ({error.filename})") from error
    indexes = [n for n in hashes if n.endswith("/" + INDEX)]
    if len(indexes) != 1:
        raise ArchiveError(f"the release does not hold exactly one {INDEX}")
    listed = index_paths((staging / indexes[0]).read_text(encoding="utf-8", errors="replace"))
    if sorted(listed) != sorted(hashes):
        differ = sorted(set(listed) ^ set(hashes))
        raise ArchiveError(
            f"{INDEX} in the release does not match its files: {', '.join(differ[:5])}"
        )
    return StagedRelease(staging, hashes, sizes)


def release_hashes(
    archive_path: Path, accepted: tuple[str, ...], skipped: tuple[str, ...]
) -> dict[str, str]:
    """SHA-256 of every accepted file in a release zip, without unpacking anything."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            budget = _Budget(RELEASE_MAX_UNPACKED)
            return {
                name: _copy(archive, info, None, budget)
                for name, info in _release_members(archive, accepted, skipped)
            }
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as error:
        raise ArchiveError(f"the download is not a readable zip archive ({error})") from error
