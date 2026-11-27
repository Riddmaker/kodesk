"""A copy of a reader's KOReader folder on this PC, kept in step over SFTP (Wi-Fi, B-27); no Qt.

Everything kodesk does with a reader works on a folder: the inventory, the Garage, snapshots,
installs. Over Wi-Fi kodesk therefore keeps a local copy (the *mirror*): :meth:`Mirror.sync`
fetches the files whose size or time changed on the reader and drops what the reader no longer
has. kodesk shows and snapshots the mirror, runs every change on it exactly as over USB
(:func:`kodesk.core.snapshot.guarded_write`: snapshot, write, protected-file check) and then
sends the files that now differ to the reader (:meth:`Mirror.push`).

Rules for sending:

* Only below ``plugins/``, ``patches/`` and ``kodesk/``. While Wi-Fi is on, KOReader is running
  and saves its settings, history and book metadata itself; a file kodesk wrote there would be
  overwritten on exit, or would overwrite the user's reading. Settings changes therefore go to
  ``kodesk/pending.json``, which a patch applies when KOReader starts again (B-26,
  :mod:`kodesk.core.remote_garage`).
* A file is replaced only if the reader still has the version kodesk read (size and time);
  otherwise nothing is sent.
* In two steps: each new file is uploaded beside its target (``.kodesk-tmp``), the old one is set
  aside (``.kodesk-old``) and the new one takes its name. Once every file has been read back and
  its SHA-256 matches, the old ones are deleted. A failure on the way puts the old files back.
  If the connection is lost during that, the next :meth:`Mirror.sync` finishes the job: a set-aside
  file whose target is missing is put back, any other ``.kodesk-old`` or ``.kodesk-tmp`` is
  deleted (the snapshot taken before the write still holds the old version).

Two exceptions, new files outside the mirror: a KOReader update is sent as
``ota/koreader.updated.tar`` (:meth:`Mirror.send_new`), which KOReader's launcher unpacks on the
next start (B-27b); a PocketBook sleep picture goes into ``Photo/`` beside the KOReader folder
(:meth:`Mirror.send_picture`, B-14). Neither ever replaces a file, and each is renamed into place
only after it read back right.

Names coming from the reader are checked before they become local paths (like zip entries:
no ``..``, no separators); symbolic links are skipped, ``cache/`` is left out as in snapshots,
and of ``ota/`` (KOReader's update archives, tens of MB) only ``package.index`` is copied.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO

import paramiko

from kodesk.core.archive import INDEX as PACKAGE_INDEX
from kodesk.core.install import READER_VOLUMES, KOReaderInstall, find_install, home_folder
from kodesk.core.sleepscreen import MAX_BYTES as MAX_PICTURE_BYTES
from kodesk.core.sleepscreen import PHOTO_DIR, PICTURE_NAME, SLEEP_LOGO
from kodesk.core.snapshot import (
    CHUNK,
    EXCLUDED_DIRS,
    TEMP_SUFFIX,
    Progress,
    file_sha256,
    no_progress,
)
from kodesk.core.update import LAYOUTS, OTA_UPDATE
from kodesk.core.wifi import KeyStore, Pairing, check_host, check_reader_id, connect

OLD_SUFFIX = ".kodesk-old"
# Inside the KOReader folder (see the module text); kodesk/ holds the Garage change file (B-26).
WIFI_WRITABLE = ("plugins/", "patches/", "kodesk/")
WIFI_NEW = (OTA_UPDATE,)  # new files sent beside the mirror (Mirror.send_new)
MAX_FILE_BYTES = 1 << 30  # larger than anything KOReader keeps; a bigger one is not fetched
INDEX = "index.json"
FILES = "files"
_BAD_NAME = frozenset({"", ".", ".."})


class MirrorError(Exception):
    """The mirror could not be brought in step, or a change could not be sent."""


Sftp = paramiko.SFTPClient
Attributes = paramiko.SFTPAttributes


@dataclass(frozen=True)
class Known:
    """A file as kodesk last saw it on the reader, and its copy in the mirror."""

    size: int
    mtime: int  # on the reader (SFTP: whole seconds)
    sha256: str
    local_ns: int  # mtime of the mirror's copy, to notice a change made on this PC


def split_reader_path(koreader_path: str) -> tuple[str, str]:
    """``/mnt/ext1/applications/koreader`` → (``/mnt/ext1``, ``applications/koreader``)."""
    for volume in READER_VOLUMES.values():
        if koreader_path.startswith(volume + "/"):
            relative = koreader_path.removeprefix(volume + "/")
            if all(part not in _BAD_NAME for part in relative.split("/")):
                return volume, relative
    raise MirrorError(f"not a KOReader folder kodesk knows: {koreader_path!r}")


def _check_name(name: str) -> str:
    if name in _BAD_NAME or "/" in name or "\\" in name or "\0" in name:
        raise MirrorError(f"the reader sent a file name kodesk will not use: {name!r}")
    return name


def remote_entries(sftp: Sftp, folder: str) -> list[tuple[str, bool]]:
    """Names in one folder on the reader with "is a folder"; [] if it cannot be listed."""
    try:
        listing = sftp.listdir_attr(folder)
    except OSError:
        return []
    return [
        (item.filename, stat.S_ISDIR(item.st_mode or 0))
        for item in listing
        if item.filename not in _BAD_NAME and not stat.S_ISLNK(item.st_mode or 0)
    ]


class Mirror:
    """The local copy of one reader: ``<base>/files`` holds the reader's volume layout."""

    def __init__(self, base: Path, koreader_path: str, extra: Iterable[str] = ()) -> None:
        self.base = base
        self.koreader_path = koreader_path
        self.volume, self.relative = split_reader_path(koreader_path)
        self.files = base / FILES
        self.extra = tuple(extra)  # single files beside the KOReader folder, volume-relative
        self.known: dict[str, Known] = self._load_index()

    @property
    def koreader_dir(self) -> Path:
        return self.files / self.relative

    def remote(self, relative: str) -> str:
        return f"{self.volume}/{relative}"

    def local(self, relative: str) -> Path:
        parts = relative.split("/")
        if any(part in _BAD_NAME or "\\" in part or "\0" in part for part in parts):
            raise MirrorError(f"unsafe path: {relative!r}")
        return self.files.joinpath(*parts)

    # -- index ----------------------------------------------------------------------------

    def _load_index(self) -> dict[str, Known]:
        try:
            data = json.loads((self.base / INDEX).read_text(encoding="utf-8"))
            if data.get("format") != 1 or data.get("koreader") != self.koreader_path:
                return {}
            return {
                str(path): Known(int(size), int(mtime), str(sha), int(local_ns))
                for path, (size, mtime, sha, local_ns) in data["files"].items()
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}  # no index or a damaged one: everything is fetched again

    def _save_index(self) -> None:
        self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = {
            "format": 1,
            "koreader": self.koreader_path,
            "files": {
                path: [k.size, k.mtime, k.sha256, k.local_ns]
                for path, k in sorted(self.known.items())
            },
        }
        temp = self.base / (INDEX + TEMP_SUFFIX)
        temp.write_text(json.dumps(data, indent=0), encoding="utf-8")
        temp.replace(self.base / INDEX)

    # -- reading --------------------------------------------------------------------------

    def listing(self, sftp: Sftp) -> dict[str, Attributes]:
        """Every regular file of the KOReader folder (not ``cache/``) plus :attr:`extra`."""
        return self._walk(sftp)[0]

    def _walk(self, sftp: Sftp) -> tuple[dict[str, Attributes], list[str]]:
        """:meth:`listing`, and kodesk's own leftovers from an interrupted send."""
        found: dict[str, Attributes] = {}
        leftovers: list[str] = []
        todo = [self.relative]
        while todo:
            folder = todo.pop()
            try:
                items = sftp.listdir_attr(self.remote(folder))
            except OSError as error:
                if folder == self.relative:
                    raise MirrorError(
                        f"No KOReader folder at {self.koreader_path} on the reader. "
                        "Pair it again over USB."
                    ) from error
                raise
            for item in items:
                relative = f"{folder}/{_check_name(item.filename)}"
                mode = item.st_mode or 0
                if stat.S_ISDIR(mode):
                    if not (folder == self.relative and item.filename in EXCLUDED_DIRS):
                        todo.append(relative)
                elif stat.S_ISREG(mode) and self._mirrored(relative):
                    found[relative] = item
                elif stat.S_ISREG(mode) and self._leftover(relative):
                    leftovers.append(relative)
        for relative in self.extra:
            with suppress(OSError):
                item = sftp.stat(self.remote(relative))
                if stat.S_ISREG(item.st_mode or 0):
                    found[relative] = item
        return found, leftovers

    def _leftover(self, relative: str) -> bool:
        """A temp or set-aside file kodesk itself may have left (only where it sends to)."""
        inside = relative.removeprefix(self.relative + "/")
        if inside == OTA_UPDATE + TEMP_SUFFIX:
            return True
        return relative.endswith((TEMP_SUFFIX, OLD_SUFFIX)) and self.writable(relative)

    def _tidy(self, sftp: Sftp, leftovers: Iterable[str], present: set[str]) -> None:
        """Finish a send that a lost connection interrupted (see the module text)."""
        for relative in leftovers:
            path = self.remote(relative)
            target = relative.removesuffix(OLD_SUFFIX)
            with suppress(OSError):
                if target != relative and target not in present:
                    sftp.rename(path, self.remote(target))  # never replaces a file
                else:
                    sftp.remove(path)

    def _mirrored(self, relative: str) -> bool:
        if relative.endswith((TEMP_SUFFIX, OLD_SUFFIX)):
            return False
        inside = relative.removeprefix(self.relative + "/")
        return not inside.startswith("ota/") or inside == PACKAGE_INDEX

    def _fresh(self, relative: str, item: Attributes) -> bool:
        known = self.known.get(relative)
        if known is None or (known.size, known.mtime) != (item.st_size, item.st_mtime):
            return False
        try:
            here = self.local(relative).stat()
        except OSError:
            return False
        return here.st_size == known.size and here.st_mtime_ns == known.local_ns

    def _fetch(self, sftp: Sftp, relative: str, item: Attributes) -> None:
        size = item.st_size or 0
        if size > MAX_FILE_BYTES:
            raise MirrorError(f"{relative} on the reader is too large ({size} bytes)")
        target = self.local(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + TEMP_SUFFIX)
        sha = hashlib.sha256()
        try:
            with sftp.open(self.remote(relative), "rb") as source, temp.open("wb") as copy:
                while chunk := source.read(CHUNK):
                    sha.update(chunk)
                    copy.write(chunk)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        here = target.stat()
        self.known[relative] = Known(
            here.st_size, item.st_mtime or 0, sha.hexdigest(), here.st_mtime_ns
        )

    def sync(self, sftp: Sftp, progress: Progress = no_progress) -> None:
        """Make the mirror match the reader; ``progress`` gets bytes fetched and to fetch."""
        self.files.mkdir(parents=True, exist_ok=True)
        self.base.chmod(0o700)  # the copy holds the reader's settings, as snapshots do
        remote, leftovers = self._walk(sftp)
        if leftovers:
            self._tidy(sftp, leftovers, set(remote))
            remote = self.listing(sftp)
        fetch = [(p, item) for p, item in sorted(remote.items()) if not self._fresh(p, item)]
        total = sum(item.st_size or 0 for _p, item in fetch)
        done = 0
        try:
            progress(done, total)
            for relative, item in fetch:
                self._fetch(sftp, relative, item)
                done += item.st_size or 0
                progress(done, total)
            self._drop_others(set(remote))
        finally:
            self._save_index()

    def _local_files(self) -> dict[str, Path]:
        found = {}
        for current, _dirs, names in os.walk(self.files):
            here = Path(current)
            for name in names:
                if not name.endswith(TEMP_SUFFIX):
                    found[(here / name).relative_to(self.files).as_posix()] = here / name
        return found

    def _drop_others(self, keep: set[str]) -> None:
        """Delete copies of files the reader no longer has (and folders left empty)."""
        for relative, path in self._local_files().items():
            if relative not in keep:
                path.unlink()
        for relative in set(self.known) - keep:
            del self.known[relative]
        for current, dirs, names in os.walk(self.files, topdown=False):
            if not dirs and not names and Path(current) != self.files:
                with suppress(OSError):
                    Path(current).rmdir()

    # -- sending --------------------------------------------------------------------------

    def changes(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Files changed or added in the mirror since the reader was read, and files removed."""
        present = self._local_files()
        write = []
        for relative, path in sorted(present.items()):
            known = self.known.get(relative)
            here = path.stat()
            if known is not None and (here.st_size, here.st_mtime_ns) == (
                known.size,
                known.local_ns,
            ):
                continue
            if known is None or file_sha256(path) != known.sha256:
                write.append(relative)
        remove = sorted(set(self.known) - set(present))
        return tuple(write), tuple(remove)

    def writable(self, relative: str) -> bool:
        inside = relative.removeprefix(self.relative + "/")
        return inside != relative and inside.startswith(WIFI_WRITABLE)

    def push(self, sftp: Sftp) -> tuple[str, ...]:
        """Send what changed in the mirror; on any failure the mirror matches the reader again."""
        write, remove = self.changes()
        paths = (*write, *remove)
        if not paths:
            return ()
        try:
            self._check(sftp, paths)
            self._apply(sftp, write, remove)
        except Exception as error:
            self.revert(sftp, paths)
            if isinstance(error, MirrorError):
                raise
            raise MirrorError(f"sending to the reader failed: {error}") from error
        return paths

    def _check(self, sftp: Sftp, paths: Iterable[str]) -> None:
        refused = [p.removeprefix(self.relative + "/") for p in paths if not self.writable(p)]
        if refused:
            raise MirrorError(
                "Over Wi-Fi kodesk changes only plugins, patches and its change file: KOReader "
                "is running and saves its settings itself. Do this over USB with KOReader closed. "
                f"(Would change: {', '.join(refused[:5])}{' …' if len(refused) > 5 else ''})"
            )
        for relative in paths:
            known = self.known.get(relative)
            try:
                item: Attributes | None = sftp.stat(self.remote(relative))
            except FileNotFoundError:
                item = None
            same = (
                item is None
                if known is None
                else item is not None and (item.st_size, item.st_mtime) == (known.size, known.mtime)
            )
            if not same:
                raise MirrorError(
                    f"{relative} changed on the reader since kodesk read it; nothing was sent. "
                    "Connect again and retry."
                )

    def _apply(self, sftp: Sftp, write: tuple[str, ...], remove: tuple[str, ...]) -> None:
        made: list[str] = []
        aside: list[str] = []  # targets whose old file is now <name>.kodesk-old
        placed: list[str] = []  # targets that now hold the new file
        try:
            for relative in write:
                target = self.remote(relative)
                self._makedirs(sftp, PurePosixPath(relative).parent.as_posix(), made)
                with self.local(relative).open("rb") as handle:
                    sftp.putfo(handle, target + TEMP_SUFFIX)
                if relative in self.known:
                    sftp.rename(target, target + OLD_SUFFIX)
                    aside.append(relative)
                sftp.rename(target + TEMP_SUFFIX, target)
                placed.append(relative)
            for relative in remove:
                sftp.rename(self.remote(relative), self.remote(relative) + OLD_SUFFIX)
                aside.append(relative)
            for relative in write:
                if self._remote_sha(sftp, relative) != file_sha256(self.local(relative)):
                    raise MirrorError(f"{relative} reads back differently from the reader")
        except Exception:
            self._roll_back(sftp, write, placed, aside, made)
            raise
        for relative in aside:
            with suppress(OSError):
                sftp.remove(self.remote(relative) + OLD_SUFFIX)
        for relative in remove:
            self._remove_empty_parents(sftp, relative)
            self.known.pop(relative, None)
        for relative in write:
            item = sftp.stat(self.remote(relative))
            here = self.local(relative).stat()
            sha = file_sha256(self.local(relative))
            self.known[relative] = Known(here.st_size, item.st_mtime or 0, sha, here.st_mtime_ns)
        self._save_index()

    def _makedirs(self, sftp: Sftp, folder: str, made: list[str]) -> None:
        """Create the missing folders of ``folder`` (volume-relative): inside the KOReader
        folder, or ``Photo/`` itself (a reset reader may not have it yet)."""
        parts = folder.split("/")
        first = 1 if folder == PHOTO_DIR else len(self.relative.split("/")) + 1
        for end in range(first, len(parts) + 1):
            path = self.remote("/".join(parts[:end]))
            try:
                sftp.stat(path)
            except FileNotFoundError:
                sftp.mkdir(path)
                made.append(path)

    def _roll_back(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        sftp: Sftp,
        write: Iterable[str],
        placed: list[str],
        aside: list[str],
        made: list[str],
    ) -> None:
        for relative in write:
            with suppress(OSError):
                sftp.remove(self.remote(relative) + TEMP_SUFFIX)
        for relative in reversed(placed):
            with suppress(OSError):
                sftp.remove(self.remote(relative))
        for relative in reversed(aside):
            with suppress(OSError):
                sftp.rename(self.remote(relative) + OLD_SUFFIX, self.remote(relative))
        for folder in reversed(made):
            with suppress(OSError):
                sftp.rmdir(folder)

    def _remove_empty_parents(self, sftp: Sftp, relative: str) -> None:
        """Remove folders a removal left empty, e.g. a removed plugin's, never the top ones."""
        folder = PurePosixPath(relative).parent
        top = len(self.relative.split("/")) + 1  # keep plugins/ and patches/ themselves
        while len(folder.parts) > top:
            try:
                if sftp.listdir_attr(self.remote(folder.as_posix())):
                    return
                sftp.rmdir(self.remote(folder.as_posix()))
            except OSError:
                return
            folder = folder.parent

    def send_new(
        self, sftp: Sftp, inside: str, source: Path, progress: Progress = no_progress
    ) -> None:
        """Upload ``source`` as a new file ``inside`` the KOReader folder (only :data:`WIFI_NEW`).

        Refused if the reader already has that file; uploaded beside it, read back, then renamed.
        ``progress`` gets bytes sent and in total; raising from it stops the upload cleanly.
        """
        if inside not in WIFI_NEW:
            raise MirrorError(f"kodesk does not send {inside} over Wi-Fi")
        waiting = (
            "A KOReader update is already waiting on the reader. Restart KOReader to "
            "install it (Exit → Restart KOReader), then connect again."
        )
        with source.open("rb") as handle:
            self._send_new_file(
                sftp,
                f"{self.relative}/{inside}",
                (handle, source.stat().st_size, file_sha256(source)),
                (waiting, "the update was not sent"),
                progress,
            )

    def send_picture(self, sftp: Sftp, name: str, data: bytes) -> str:
        """Upload a sleep picture as the new file ``Photo/<name>`` on the reader's volume (B-14);
        return its volume-relative path. Never replaces a file."""
        if not PICTURE_NAME.fullmatch(name):
            raise MirrorError(f"kodesk does not send {name!r} over Wi-Fi")
        if not data or len(data) > MAX_PICTURE_BYTES:
            raise MirrorError(f"the picture has an unexpected size ({len(data)} bytes)")
        relative = f"{PHOTO_DIR}/{name}"
        self._send_new_file(
            sftp,
            relative,
            (io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest()),
            (f"{relative} is already on the reader", "the picture was not sent"),
            no_progress,
        )
        return relative

    def _send_new_file(
        self,
        sftp: Sftp,
        relative: str,
        content: tuple[IO[bytes], int, str],
        texts: tuple[str, str],
        progress: Progress,
    ) -> None:
        """Upload ``content`` (handle, size, SHA-256) as the new file ``relative`` (to the
        volume): beside it first, read back, then renamed; ``texts`` = (exists, failed)."""
        handle, total, sha = content
        target = self.remote(relative)
        with suppress(FileNotFoundError):
            sftp.stat(target)
            raise MirrorError(texts[0])
        made: list[str] = []
        try:
            self._makedirs(sftp, PurePosixPath(relative).parent.as_posix(), made)
            progress(0, total)
            sftp.putfo(
                handle, target + TEMP_SUFFIX, total, lambda done, _all: progress(done, total)
            )
            if self._remote_sha(sftp, relative + TEMP_SUFFIX) != sha:
                raise MirrorError(f"{relative} reads back differently from the reader")
            sftp.rename(target + TEMP_SUFFIX, target)
        except Exception as error:
            with suppress(OSError):
                sftp.remove(target + TEMP_SUFFIX)
            for folder in reversed(made):
                with suppress(OSError):
                    sftp.rmdir(folder)
            if isinstance(error, (MirrorError, OSError, paramiko.SSHException)):
                raise MirrorError(f"{texts[1]}: {error}") from error
            raise

    def _remote_sha(self, sftp: Sftp, relative: str) -> str:
        sha = hashlib.sha256()
        with sftp.open(self.remote(relative), "rb") as source:
            while chunk := source.read(CHUNK):
                sha.update(chunk)
        return sha.hexdigest()

    def revert(self, sftp: Sftp, paths: Iterable[str]) -> None:
        """Make these files in the mirror match the reader again (after a refused change)."""
        for relative in paths:
            try:
                self.local(relative).unlink(missing_ok=True)
                item = sftp.stat(self.remote(relative))
                self._fetch(sftp, relative, item)
            except OSError:
                self.known.pop(relative, None)  # gone on the reader, or unreachable: refetch later
        self._drop_others(set(self.known))
        self._save_index()


@dataclass
class WifiReader:
    """A paired reader reached over Wi-Fi: its mirror, and how to reach it again."""

    keys: KeyStore
    pairing: Pairing
    host: str
    mirror: Mirror
    books: tuple[tuple[str, bool], ...] = ()  # the books folder: (name, is a folder)

    @classmethod
    def open(cls, keys: KeyStore, pairing: Pairing, host: str, base: Path) -> WifiReader:
        """The reader's mirror below ``base``; nothing is contacted yet."""
        if not pairing.koreader_path:
            raise MirrorError(
                "Pair this reader again over USB: kodesk now also needs to know where KOReader "
                "is on it."
            )
        folder = base / check_reader_id(pairing.reader_id)
        launchers = tuple(layout.launcher for layout in LAYOUTS.values() if layout.launcher)
        mirror = Mirror(folder, pairing.koreader_path, (SLEEP_LOGO, *launchers))
        return cls(keys, pairing, check_host(host), mirror)

    def install(self) -> KOReaderInstall:
        return find_install(self.mirror.koreader_dir)

    def sync(self, progress: Progress = no_progress) -> KOReaderInstall:
        """Connect, bring the mirror in step and list the books folder (names only)."""
        with connect(self.keys, self.pairing, self.host) as connection:
            self.pairing = connection.pairing
            self.mirror.sync(connection.sftp, progress)
            install = self.install()
            home = home_folder(install.platform, install.read_settings())
            self.books = tuple(remote_entries(connection.sftp, self.mirror.remote(home)))
        return install

    def send_update(self, archive: Path, progress: Progress = no_progress) -> None:
        """Send a KOReader update (:func:`kodesk.core.update.ota_archive`) to the reader."""
        with connect(self.keys, self.pairing, self.host) as connection:
            self.pairing = connection.pairing
            self.mirror.send_new(connection.sftp, OTA_UPDATE, archive, progress)

    def send_picture(self, name: str, data: bytes) -> str:
        """Put a sleep picture into the reader's ``Photo/`` (B-14); its volume-relative path."""
        with connect(self.keys, self.pairing, self.host) as connection:
            self.pairing = connection.pairing
            return self.mirror.send_picture(connection.sftp, name, data)

    def publish(self) -> None:
        """Send the mirror's changes to the reader (the snapshot store's ``publish``)."""
        with connect(self.keys, self.pairing, self.host) as connection:
            self.mirror.push(connection.sftp)
