"""Snapshot, verify and restore a reader's KOReader folder (B-04). The way back for every write.

Layout on the PC (``base`` = ``<app data dir>/snapshots/<device id>``)::

    blobs/ab/ab12…        one file per distinct content, named by its SHA-256
    manifests/<id>.json   one per snapshot: every file's relative path, size and SHA-256

A file that did not change between snapshots is stored once, so keeping five costs about one
copy of the folder plus what changed. The manifest is written last (temp file + rename): a
snapshot interrupted by unplugging the reader simply does not exist.

A KOReader update (B-06) also changes the launcher beside the KOReader folder (PocketBook:
``applications/koreader.app``); a snapshot can keep such a file too, stored as ``../<name>``.
Only the names in :data:`OUTSIDE` are accepted, nothing else outside the folder.

The never-touch list (ARCHITECTURE-SECURITY §4) is :func:`is_protected`. :func:`guarded_write`
runs one write between a snapshot and a byte-for-byte check of the protected files; a mismatch
or a failed write restores the snapshot.

Taking a snapshot reads the whole folder (~380 MB on the reference reader), so it reports its
progress in bytes; the progress callback may raise :class:`SnapshotCancelledError` to stop it,
which is safe because nothing has been written to the reader yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from kodesk.core import healthcheck
from kodesk.core.inventory import Origin, list_plugins
from kodesk.core.luadump import SettingsDocument

KEEP = 5
EXCLUDED_DIRS = frozenset({"cache"})  # KOReader rebuilds it; 16 MB on the reference reader
# The log is not setup, and rolling it back would erase what the health check (B-10) reads.
EXCLUDED_FILES = frozenset({healthcheck.CRASH_LOG, healthcheck.CRASH_LOG + ".new"})
PROTECTED_FILES = frozenset(
    {"settings.reader.lua", "settings.reader.lua.old", "history.lua", "defaults.custom.lua"}
)
PROTECTED_DIRS = frozenset(
    {"settings", "history", "styletweaks", "patches", "icons", "clipboard", "screenshots", "news"}
)
FREE_SPACE_MARGIN = 64 * 1024 * 1024
CHUNK = 1 << 20
TEMP_SUFFIX = ".kodesk-tmp"
OUTSIDE = frozenset({"koreader.app"})  # files beside the KOReader folder a snapshot may keep
_SNAPSHOT_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_PATH = re.compile(r"[\\\0]|^[A-Za-z]:")  # backslash, NUL, drive letter


class SnapshotError(Exception):
    """A snapshot could not be taken, read or restored."""


class SnapshotCancelledError(SnapshotError):
    """The user stopped taking a snapshot; nothing was written to the reader."""

    def __init__(self) -> None:
        super().__init__("cancelled before anything was written; your reader is as before")


Progress = Callable[[int, int], None]  # bytes read so far, bytes in total


def no_progress(_done: int, _total: int) -> None:
    """The default progress callback: report nothing."""


class WriteRejectedError(SnapshotError):
    """A guarded write changed protected files (or failed); the snapshot was restored."""

    def __init__(self, message: str, changed: tuple[str, ...]) -> None:
        super().__init__(message)
        self.changed = changed


@dataclass(frozen=True)
class FileEntry:
    path: str  # relative POSIX path inside the KOReader folder
    size: int
    sha256: str


@dataclass(frozen=True)
class Snapshot:
    """One complete copy of the KOReader folder, as listed in its manifest."""

    id: str
    created: datetime
    files: tuple[FileEntry, ...]
    outside: tuple[FileEntry, ...] = ()  # "../koreader.app": beside the KOReader folder

    @property
    def size(self) -> int:
        return sum(entry.size for entry in (*self.files, *self.outside))

    def by_path(self) -> dict[str, FileEntry]:
        return {entry.path: entry for entry in self.files}


@dataclass(frozen=True)
class RestorePlan:
    """What a restore changes: files written back (changed or missing) and files removed."""

    write: tuple[str, ...]
    remove: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.write or self.remove)


def device_id(model: str | None, volume_label: str, serial: str | None = None) -> str:
    """A folder name for one reader: model, volume name and serial if known, filesystem-safe."""
    raw = f"{model or 'reader'}-{volume_label or 'volume'}" + (f"-{serial}" if serial else "")
    return re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("._") or "reader"


def third_party_plugins(koreader_dir: Path) -> frozenset[str]:
    """Plugins not shipped with KOReader; unknown origin counts as third-party (protected)."""
    settings = SettingsDocument.parse("return {}\n")
    return frozenset(
        plugin.name
        for plugin in list_plugins(koreader_dir, settings)
        if plugin.origin is not Origin.BUILT_IN
    )


def is_protected(path: str, third_party: frozenset[str]) -> bool:
    """True for the reader's own state, which no update, install or removal may change."""
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    if len(parts) == 1:
        return parts[0] in PROTECTED_FILES
    if parts[0] in PROTECTED_DIRS:
        return True
    return parts[0] == "plugins" and parts[1].removesuffix(".koplugin") in third_party


def _check_relative(path: str) -> str:
    """Reject manifest paths that would leave the KOReader folder (a tampered manifest)."""
    if (
        _UNSAFE_PATH.search(path)
        or PurePosixPath(path).is_absolute()
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise SnapshotError(f"unsafe path in snapshot: {path!r}")
    return path


def _check_outside(path: str) -> str:
    """``../<name>`` for a name in :data:`OUTSIDE`; anything else is refused."""
    if not path.startswith("../") or path.removeprefix("../") not in OUTSIDE:
        raise SnapshotError(f"unexpected file outside the KOReader folder: {path!r}")
    return path


def _outside_target(koreader_dir: Path, path: str) -> Path:
    return koreader_dir.parent / _check_outside(path).removeprefix("../")


def walk(koreader_dir: Path) -> list[str]:
    """Relative paths of all regular files to snapshot, sorted; symlinks are refused."""
    found: list[str] = []
    for current, dirs, files in os.walk(koreader_dir):
        here = Path(current)
        if here == koreader_dir:
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for name in dirs + files:
            if (here / name).is_symlink():
                raise SnapshotError(f"symbolic link in the KOReader folder: {here / name}")
        for name in files:
            if name.endswith(TEMP_SUFFIX) or (here == koreader_dir and name in EXCLUDED_FILES):
                continue
            found.append((here / name).relative_to(koreader_dir).as_posix())
    return sorted(found)


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            sha.update(chunk)
    return sha.hexdigest()


def _copy_hashing(source: Path, target: Path) -> str:
    sha = hashlib.sha256()
    with source.open("rb") as src, target.open("wb") as dst:
        while chunk := src.read(CHUNK):
            sha.update(chunk)
            dst.write(chunk)
    return sha.hexdigest()


class SnapshotStore:
    """The snapshots of one reader on the PC."""

    def __init__(self, base: Path, clock: Callable[[], datetime] | None = None) -> None:
        self.base = base
        self.blobs = base / "blobs"
        self.manifests = base / "manifests"
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- taking ---------------------------------------------------------------------------

    def take(
        self, koreader_dir: Path, outside: Iterable[str] = (), progress: Progress = no_progress
    ) -> Snapshot:
        """Copy every file (not cache/, not the log) to the store; the manifest is written last.

        ``outside`` names files beside the folder to keep too (only names in :data:`OUTSIDE`).
        ``progress`` is called before the first file and after each one.
        """
        if not (koreader_dir / "reader.lua").is_file():  # unplugged: never an empty snapshot
            raise SnapshotError(f"the reader is not connected (no KOReader at {koreader_dir})")
        paths = walk(koreader_dir)
        extra = [
            f"../{name}"
            for name in sorted(set(outside))
            if _outside_target(koreader_dir, f"../{name}").is_file()
        ]
        sizes = {p: (koreader_dir / p).stat().st_size for p in [*paths, *extra]}
        total = sum(sizes.values())
        self._check_free_space(total)
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        entries: list[FileEntry] = []
        beside: list[FileEntry] = []
        temp = self.blobs / f"incoming{TEMP_SUFFIX}"
        done = 0
        try:
            progress(done, total)
            for relative in [*paths, *extra]:
                source = (
                    _outside_target(koreader_dir, relative)
                    if relative.startswith("../")
                    else koreader_dir / relative
                )
                digest = _copy_hashing(source, temp)
                blob = self._blob(digest)
                if blob.exists():
                    temp.unlink()
                else:
                    blob.parent.mkdir(exist_ok=True)
                    temp.replace(blob)
                entry = FileEntry(relative, source.stat().st_size, digest)
                (beside if relative.startswith("../") else entries).append(entry)
                done += sizes[relative]
                progress(done, total)
        finally:
            temp.unlink(missing_ok=True)
        snapshot = Snapshot(self._new_id(), self._clock(), tuple(entries), tuple(beside))
        self._write_manifest(snapshot)
        self.prune()
        return snapshot

    def _check_free_space(self, size: int) -> None:
        needed = size + FREE_SPACE_MARGIN
        self.base.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.base).free
        if free < needed:
            raise SnapshotError(
                f"not enough free space for a snapshot: {needed // 2**20} MB needed, "
                f"{free // 2**20} MB free in {self.base}"
            )

    def _new_id(self) -> str:
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        candidate, n = stamp, 1
        while (self.manifests / f"{candidate}.json").exists():
            candidate, n = f"{stamp}-{n}", n + 1
        return candidate

    def _write_manifest(self, snapshot: Snapshot) -> None:
        data = {
            "format": 1,
            "id": snapshot.id,
            "created": snapshot.created.isoformat(),
            "files": [[e.path, e.size, e.sha256] for e in snapshot.files],
        }
        if snapshot.outside:
            data["outside"] = [[e.path, e.size, e.sha256] for e in snapshot.outside]
        target = self.manifests / f"{snapshot.id}.json"
        temp = target.with_name(target.name + TEMP_SUFFIX)
        temp.write_text(json.dumps(data, indent=0), encoding="utf-8")
        temp.replace(target)

    def _blob(self, digest: str) -> Path:
        if not _SHA256.match(digest):
            raise SnapshotError(f"invalid hash in snapshot: {digest!r}")
        return self.blobs / digest[:2] / digest

    # -- reading --------------------------------------------------------------------------

    def snapshots(self) -> list[Snapshot]:
        """All complete snapshots, newest first."""
        if not self.manifests.is_dir():
            return []
        ids = [p.stem for p in self.manifests.glob("*.json") if _SNAPSHOT_ID.match(p.stem)]
        return [self.load(i) for i in sorted(ids, reverse=True)]

    def load(self, snapshot_id: str) -> Snapshot:
        """Read one manifest; every path in it is checked before use."""
        if not _SNAPSHOT_ID.match(snapshot_id):
            raise SnapshotError(f"invalid snapshot id: {snapshot_id!r}")
        try:
            data = json.loads((self.manifests / f"{snapshot_id}.json").read_text("utf-8"))
            files = tuple(
                FileEntry(_check_relative(str(path)), int(size), str(digest))
                for path, size, digest in data["files"]
            )
            outside = tuple(
                FileEntry(_check_outside(str(path)), int(size), str(digest))
                for path, size, digest in data.get("outside", [])
            )
            created = datetime.fromisoformat(str(data["created"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise SnapshotError(f"snapshot {snapshot_id} is unreadable ({error})") from error
        return Snapshot(snapshot_id, created, files, outside)

    def prune(self, keep: int = KEEP) -> None:
        """Keep the newest ``keep`` snapshots and drop blobs no kept snapshot uses."""
        kept = self.snapshots()
        for old in kept[keep:]:
            (self.manifests / f"{old.id}.json").unlink()
        used = {e.sha256 for snap in kept[:keep] for e in (*snap.files, *snap.outside)}
        if not self.blobs.is_dir():
            return
        for blob in self.blobs.glob("*/*"):
            if blob.name not in used:
                blob.unlink()

    # -- restoring ------------------------------------------------------------------------

    def plan_restore(self, koreader_dir: Path, snapshot: Snapshot) -> RestorePlan:
        """Files that differ from the snapshot (written back) and files added since (removed)."""
        wanted = snapshot.by_path()
        present = set(walk(koreader_dir))
        write = [
            path
            for path, entry in wanted.items()
            if path not in present or file_sha256(koreader_dir / path) != entry.sha256
        ]
        remove = sorted(present - wanted.keys())
        for entry in snapshot.outside:  # written back if changed; never removed
            target = _outside_target(koreader_dir, entry.path)
            if not target.is_file() or file_sha256(target) != entry.sha256:
                write.append(entry.path)
        return RestorePlan(tuple(sorted(write)), tuple(remove))

    def restore(self, koreader_dir: Path, snapshot: Snapshot) -> RestorePlan:
        """Bring the folder back to the snapshot byte for byte, then check it."""
        plan = self.plan_restore(koreader_dir, snapshot)
        wanted = {**snapshot.by_path(), **{e.path: e for e in snapshot.outside}}
        root = koreader_dir.resolve()
        for path in plan.write:
            target = (
                _outside_target(root, path) if path.startswith("../") else self._inside(root, path)
            )
            blob = self._blob(wanted[path].sha256)
            if not blob.is_file():
                raise SnapshotError(f"snapshot {snapshot.id} is missing the content of {path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + TEMP_SUFFIX)
            shutil.copyfile(blob, temp)  # vfat: copy beside the target, then replace
            temp.replace(target)
        for path in plan.remove:
            target = self._inside(root, path)
            target.unlink()
            _remove_empty_parents(root, target.parent, wanted.keys())
        left = self.plan_restore(koreader_dir, snapshot)
        if left:
            raise SnapshotError(
                f"restore of {snapshot.id} incomplete: {', '.join((*left.write, *left.remove))}"
            )
        return plan

    @staticmethod
    def _inside(root: Path, path: str) -> Path:
        target = (root / _check_relative(path)).resolve()
        if not target.is_relative_to(root):
            raise SnapshotError(f"path leaves the KOReader folder: {path!r}")
        return target


def _remove_empty_parents(root: Path, folder: Path, kept: Iterable[str]) -> None:
    """Remove folders a removal left empty (e.g. a plugin installed after the snapshot)."""
    prefixes = {PurePosixPath(p).parent.as_posix() for p in kept}
    while folder != root and folder.is_relative_to(root) and not any(folder.iterdir()):
        relative = folder.relative_to(root).as_posix()
        if any(p == relative or p.startswith(relative + "/") for p in prefixes):
            return
        folder.rmdir()
        folder = folder.parent


def changed_protected(
    koreader_dir: Path, snapshot: Snapshot, third_party: frozenset[str], allowed: frozenset[str]
) -> tuple[str, ...]:
    """Protected files that differ from the snapshot, appeared or vanished (except ``allowed``)."""
    before = {e.path: e for e in snapshot.files if is_protected(e.path, third_party)}
    now = {p for p in walk(koreader_dir) if is_protected(p, third_party)}
    changed = set(before.keys() ^ now)
    for path in before.keys() & now:
        if file_sha256(koreader_dir / path) != before[path].sha256:
            changed.add(path)
    return tuple(sorted(p for p in changed if not _allowed(p, allowed)))


def _allowed(path: str, allowed: frozenset[str]) -> bool:
    """``allowed`` holds files, and folders written with a trailing slash."""
    return path in allowed or any(a.endswith("/") and path.startswith(a) for a in allowed)


def guarded_write(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    store: SnapshotStore,
    koreader_dir: Path,
    write: Callable[[], None],
    allowed: Iterable[str] = (),
    outside: Iterable[str] = (),
    progress: Progress = no_progress,
) -> Snapshot:
    """Snapshot, run ``write``, verify the protected files; restore on any failure.

    ``allowed`` names the protected files this write is meant to change (e.g. the Tuning-Garage
    and ``settings.reader.lua``; a folder ends with ``/``, e.g. ``plugins/x.koplugin/`` for an
    update of that plugin); every other protected file must stay byte-identical. ``outside``
    names files beside the folder the write changes (see :data:`OUTSIDE`). After a
    good write the next connect checks KOReader's log (:mod:`kodesk.core.healthcheck`).
    ``progress`` reports the snapshot (see :meth:`SnapshotStore.take`).
    """
    third_party = third_party_plugins(koreader_dir)
    snapshot = store.take(koreader_dir, outside, progress)
    try:
        write()
    except Exception as error:  # pylint: disable=broad-exception-caught  # any failure: undo
        store.restore(koreader_dir, snapshot)
        raise WriteRejectedError(f"write failed and was undone ({error})", ()) from error
    changed = changed_protected(koreader_dir, snapshot, third_party, frozenset(allowed))
    if changed:
        store.restore(koreader_dir, snapshot)
        raise WriteRejectedError(
            "the write changed protected files, so it was undone: " + ", ".join(changed), changed
        )
    healthcheck.remember(store.base, koreader_dir, snapshot.id)  # checked on the next connect
    return snapshot
