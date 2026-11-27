"""Install, update and remove plugins and user patches on the reader (B-08, B-09).

Every operation is planned first (:class:`Change`, shown as the preview) and then applied by
:func:`apply` inside :func:`kodesk.core.snapshot.guarded_write`: snapshot, write, check that no
protected file changed except the one target (``plugins/<name>.koplugin/`` or
``patches/<file>``), restore on any failure.

Rules (owner decisions 2026-11-26):

* An update writes the new version's files and removes only files the previous version shipped
  (known from kodesk's record) and that are unchanged; everything else the plugin created
  (caches, its own data) is kept.
* Removing a plugin deletes only its folder; its settings in ``settings/`` stay.
* KOReader's own plugins are never replaced or removed. Without ``ota/package.index`` kodesk
  cannot tell, so it changes an existing plugin only if it installed it itself.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from kodesk.core.archive import PLUGIN_SUFFIX, ArchiveError, StagedPlugin, safe_name
from kodesk.core.catalogue import Entry, Kind
from kodesk.core.inventory import RUN_PRIORITIES, built_in_plugins, patch_priority
from kodesk.core.records import Record, Records
from kodesk.core.remote_garage import FOLDER as KODESK_FOLDER
from kodesk.core.remote_garage import PATCH_NAME as KODESK_PATCH
from kodesk.core.remote_garage import PENDING
from kodesk.core.snapshot import (
    Progress,
    Snapshot,
    SnapshotStore,
    file_sha256,
    guarded_write,
    no_progress,
)
from kodesk.core.sources import Download

TEMP_SUFFIX = ".kodesk-tmp"
PATCHES = "patches"
PLUGINS = "plugins"


class InstallerError(Exception):
    """The operation is refused before anything is written."""


class Action(StrEnum):
    INSTALL = "install"
    UPDATE = "update"
    REMOVE = "remove"


@dataclass(frozen=True)
class Change:
    """What one operation writes, as the preview lists it. Paths are relative to KOReader's
    folder on the reader."""

    action: Action
    kind: Kind
    name: str
    target: str  # "plugins/x.koplugin" or "patches/2-x.lua"
    add: tuple[str, ...] = ()
    replace: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()  # files the plugin created itself; an update leaves them
    notes: tuple[str, ...] = ()
    record: Record | None = None  # stored after success; None drops the record
    source: Path | None = None  # staged plugin folder on the PC
    data: bytes | None = None  # patch file content
    unchanged: tuple[str, ...] = field(default=(), repr=False)

    def __bool__(self) -> bool:
        return bool(self.add or self.replace or self.remove)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _files(folder: Path) -> dict[str, Path]:
    """Regular files under ``folder`` by relative path; symbolic links are refused."""
    found: dict[str, Path] = {}
    if not folder.exists():
        return found
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise InstallerError(f"symbolic link on the reader: {path}")
        if path.is_file():
            found[path.relative_to(folder).as_posix()] = path
    return found


def _check_changeable(koreader_dir: Path, name: str, record: Record | None) -> None:
    """Existing plugins: only third-party ones, or ones kodesk installed."""
    built_in = built_in_plugins(koreader_dir)
    if built_in is not None and name in built_in:
        raise InstallerError(f"{name} is part of KOReader; kodesk does not change it.")
    if built_in is None and record is None:
        raise InstallerError(
            f"kodesk cannot tell whether {name} came with KOReader (the reader has no "
            "ota/package.index), so it does not change it."
        )


def plan_plugin(
    koreader_dir: Path,
    staged: StagedPlugin,
    entry: Entry,
    download: Download,
    previous: Record | None,
) -> Change:
    """Install ``staged`` as ``plugins/<name>.koplugin``, or update the plugin there."""
    target = f"{PLUGINS}/{staged.folder_name}"
    folder = koreader_dir / target
    existing = _files(folder)
    if folder.exists():
        _check_changeable(koreader_dir, staged.name, previous)
    elif staged.name in (built_in_plugins(koreader_dir) or ()):
        raise InstallerError(f"{staged.name} is part of KOReader; kodesk does not change it.")
    add, replace, unchanged, remove, keep = [], [], [], [], []
    for path, sha in staged.files.items():
        if path not in existing:
            add.append(path)
        elif file_sha256(existing[path]) != sha:
            replace.append(path)
        else:
            unchanged.append(path)
    shipped = previous.files if previous else {}
    for path, file in existing.items():
        if path in staged.files:
            continue
        if path in shipped and file_sha256(file) == shipped[path]:
            remove.append(path)  # the old version had it, the new one does not
        else:
            keep.append(path)
    notes = []
    if previous is None and existing:
        notes.append(
            "kodesk did not install the version on the reader, so it keeps every file the new "
            "version does not bring."
        )
    record = Record(
        kind=Kind.PLUGIN,
        name=staged.name,
        owner=entry.owner,
        repo=entry.repo,
        version=download.version,
        tag=download.tag,
        commit=download.commit,
        files=dict(staged.files),
        installed_at=_now(),
    )

    def full(paths: list[str]) -> tuple[str, ...]:
        return tuple(f"{target}/{p}" for p in sorted(paths))

    return Change(
        action=Action.UPDATE if existing else Action.INSTALL,
        kind=Kind.PLUGIN,
        name=staged.name,
        target=target,
        add=full(add),
        replace=full(replace),
        remove=full(remove),
        keep=full(keep),
        unchanged=full(unchanged),
        notes=tuple(notes),
        record=record,
        source=staged.folder,
    )


def patch_notes(name: str) -> tuple[str, ...]:
    """Why KOReader would not run a patch of this name, if it would not."""
    priority = patch_priority(name)
    if priority is None:
        return (
            "KOReader runs a patch only if its name starts with a priority digit and a dash "
            "(e.g. 2-name.lua); this one will not run.",
        )
    if priority not in RUN_PRIORITIES:
        return (f"KOReader never runs patches of priority {priority}; this one will not run.",)
    if priority == "0":
        return ("Patches of priority 0 run only after the next KOReader update.",)
    return ()


def plan_patch(koreader_dir: Path, entry: Entry, data: bytes, matches_index: bool | None) -> Change:
    """Write ``data`` as ``patches/<name>`` (new, or replacing the file there)."""
    try:
        name = safe_name(entry.name)
    except ArchiveError as error:
        raise InstallerError(str(error)) from error
    if not name.endswith(".lua") or name.startswith("."):
        raise InstallerError(f"{name} is not a Lua patch file")
    target = f"{PATCHES}/{name}"
    path = koreader_dir / target
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallerError(f"{target} on the reader is not a plain file")
    notes = list(patch_notes(name))
    if matches_index is False:
        notes.append(
            "The file changed in its repository after the catalogue was built; the code "
            "shown is the newer version."
        )
    exists = path.is_file()
    same = exists and path.read_bytes() == data
    record = Record(
        kind=Kind.PATCH,
        name=name,
        owner=entry.owner,
        repo=entry.repo,
        version=f"file {hashlib.sha256(data).hexdigest()[:7]}",
        path=entry.id.split(":", 1)[-1],
        files={name: hashlib.sha256(data).hexdigest()},
        installed_at=_now(),
    )
    return Change(
        action=Action.UPDATE if exists else Action.INSTALL,
        kind=Kind.PATCH,
        name=name,
        target=target,
        add=() if exists else (target,),
        replace=(target,) if exists and not same else (),
        unchanged=(target,) if same else (),
        notes=tuple(notes),
        record=record,
        data=data,
    )


def plan_remove(koreader_dir: Path, kind: Kind, name: str, record: Record | None) -> Change:
    """Remove a plugin folder or one patch file; nothing else."""
    try:
        safe_name(name)
    except ArchiveError as error:
        raise InstallerError(str(error)) from error
    if kind is Kind.PATCH:
        target = f"{PATCHES}/{name}"
        if not (koreader_dir / target).is_file() or (koreader_dir / target).is_symlink():
            raise InstallerError(f"{target} is not on the reader")
        if name == KODESK_PATCH:
            return _remove_kodesk_patch(koreader_dir, target)
        return Change(Action.REMOVE, kind, name, target, remove=(target,))
    target = f"{PLUGINS}/{name}{PLUGIN_SUFFIX}"
    folder = koreader_dir / target
    if not folder.is_dir() or folder.is_symlink():
        raise InstallerError(f"{target} is not on the reader")
    _check_changeable(koreader_dir, name, record)
    files = tuple(f"{target}/{p}" for p in _files(folder))
    notes = ("Its settings (in settings/ on the reader) stay, in case you install it again.",)
    return Change(Action.REMOVE, kind, name, target, remove=files, notes=notes)


def _remove_kodesk_patch(koreader_dir: Path, target: str) -> Change:
    """kodesk's own patch (B-26) goes with its folder: change file, result and backups."""
    folder = koreader_dir / KODESK_FOLDER
    if folder.is_symlink():
        raise InstallerError(f"symbolic link on the reader: {folder}")
    files = tuple(f"{KODESK_FOLDER}/{p}" for p in _files(folder))
    notes = [
        "This is kodesk's patch for Garage changes over Wi-Fi. Its folder kodesk/ goes too "
        "(the change file, KOReader's last result and the backups of earlier changes)."
    ]
    if (koreader_dir / PENDING).is_file():
        notes.append("Changes sent over Wi-Fi that KOReader has not applied yet are dropped.")
    notes.append("kodesk asks before it puts the patch back with the next change over Wi-Fi.")
    return Change(
        Action.REMOVE, Kind.PATCH, KODESK_PATCH, target, remove=(target, *files), notes=tuple(notes)
    )


def write_file(target: Path, data: bytes | Path) -> None:
    """Write ``data`` (bytes, or a file to copy) beside ``target``, then replace it (vfat)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + TEMP_SUFFIX)
    if isinstance(data, Path):
        shutil.copyfile(data, temp)
    else:
        temp.write_bytes(data)
    temp.replace(target)  # vfat: write beside the target, then replace


def _remove_empty_dirs(folder: Path, including_self: bool) -> None:
    if not folder.is_dir():
        return
    for path in sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    if including_self and not any(folder.iterdir()):
        folder.rmdir()


def apply(
    store: SnapshotStore,
    koreader_dir: Path,
    change: Change,
    records: Records,
    progress: Progress = no_progress,
) -> Snapshot:
    """Snapshot, write ``change``, verify; the snapshot is restored if anything goes wrong."""
    root = koreader_dir.resolve()

    folders = [(PLUGINS,), (PATCHES,)]
    if change.action is Action.REMOVE and change.kind is Kind.PATCH and change.name == KODESK_PATCH:
        folders.append((KODESK_FOLDER,))

    def inside(path: str) -> Path:
        target = (koreader_dir / path).resolve()
        if not target.is_relative_to(root) or PurePosixPath(path).parts[:1] not in folders:
            raise InstallerError(f"refusing to write outside plugins/ and patches/: {path}")
        return koreader_dir / path

    def write() -> None:
        for path in (*change.add, *change.replace):
            target = inside(path)
            if change.kind is Kind.PATCH:
                if change.data is None:
                    raise InstallerError("no patch content")
                write_file(target, change.data)
                expected = hashlib.sha256(change.data).hexdigest()
            else:
                if change.source is None or change.record is None:
                    raise InstallerError("no staged plugin")
                relative = path.removeprefix(change.target + "/")
                write_file(target, change.source / relative)
                expected = change.record.files[relative]
            if file_sha256(target) != expected:
                raise OSError(f"{path} reads back differently after writing")
        for path in change.remove:
            inside(path).unlink()
        if change.kind is Kind.PLUGIN:
            _remove_empty_dirs(koreader_dir / change.target, change.action is Action.REMOVE)
        elif (KODESK_FOLDER,) in folders:
            _remove_empty_dirs(koreader_dir / KODESK_FOLDER, True)

    allowed = {change.target + "/" if change.kind is Kind.PLUGIN else change.target}
    snapshot = guarded_write(store, koreader_dir, write, allowed, progress=progress)
    if change.action is Action.REMOVE:
        records.drop(change.kind, change.name)
    elif change.record is not None:
        records.put(change.record)
    return snapshot
