"""Update KOReader itself on the reader (B-06), safer than KOReader's own update.

KOReader's own update (``platform/pocketbook/koreader.app``, ``ko_update_check``) unpacks the new
release over the live install and then removes every file named in the old
``ota/package.index`` but not in the new one; there is no way back. kodesk does the same steps
with the same removal rule (so files the user added are never removed), plus:

* the release is the latest stable one on GitHub, checked against GitHub's SHA-256 and
  unpacked on the PC first (:mod:`kodesk.core.release`, :mod:`kodesk.core.archive`);
* the preview lists what is added, replaced and removed; shipped files the user changed (found
  by comparing with the installed version's own release, when GitHub has it) can be kept;
* everything runs inside :func:`kodesk.core.snapshot.guarded_write`, together with the launcher
  beside the KOReader folder; ``git-rev``, ``ota/package.index`` and ``update_once.marker`` are
  written last, so an interrupted run still names the old version;
* ``pending-update.json`` beside the snapshots marks a running update: if it is still there on
  the next connect (cable pulled), kodesk offers the snapshot taken before.

Over Wi-Fi (B-27b) KOReader is running, so kodesk cannot write its files. :func:`ota_archive`
instead packs the same files (without the ones kept) into ``ota/koreader.updated.tar``, the
archive KOReader's own launcher unpacks the next time KOReader starts; the launcher also does
the removals (same rule as above). kodesk sends it only when the installed launcher is one whose
update step it knows (:func:`check_launcher`).

Owner decisions 2026-11-26: GitHub releases only (A); PocketBook only for now (B); edited files
can be kept, default the new version (C); the launcher is updated, its old copy is in the
snapshot (D); a third-party plugin named like a new built-in one blocks the update (E).
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from kodesk.core import koversion, release
from kodesk.core.archive import (
    INDEX,
    PLUGIN_SUFFIX,
    ArchiveError,
    StagedRelease,
    index_paths,
    release_hashes,
    stage_release,
)
from kodesk.core.install import KOReaderInstall, volume_root
from kodesk.core.installer import write_file
from kodesk.core.net import Get, NetError, Save
from kodesk.core.snapshot import (
    Progress,
    Snapshot,
    SnapshotStore,
    WriteRejectedError,
    file_sha256,
    guarded_write,
    is_protected,
    no_progress,
)

PENDING_FILE = "pending-update.json"
INSTALLED_TAR = "ota/koreader.installed.tar"  # KOReader's own update base; rebuilt when missing
WRITTEN_LAST = ("git-rev", INDEX, "update_once.marker")
SPACE_MARGIN = 16 * 2**20
MAX_INDEX = 4 * 2**20
OTA_UPDATE = "ota/koreader.updated.tar"  # unpacked by the launcher on KOReader's next start
# The update step of the PocketBook launcher v2025.10 … v2026.07.2 (platform/pocketbook/
# koreader.app): GNU tar -C /mnt/ext1 -xf, then the package.index rule. KOReader's master branch
# moved to ota/update.tar.xz and its own unpack tool; not sent until checked on a reader.
_OTA_STEPS = (
    'NEWUPDATE="${KOREADER_DIR}/ota/koreader.updated.tar"',
    '-C "/mnt/ext1" -xf "${NEWUPDATE}"',
    "grep -xvFf /tmp/package.index.new /tmp/package.index.old",
)
MAX_LAUNCHER = 256 * 1024
_PLUGIN = re.compile(r"plugins/(?P<name>[^/]+)" + re.escape(PLUGIN_SUFFIX) + "/")
_SNAPSHOT_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?$")


class UpdateError(Exception):
    """The update is refused before anything is written."""


@dataclass(frozen=True)
class Layout:
    """Where a platform's release goes on the reader's volume (research: koreader-releases §2)."""

    prefix: str  # the KOReader folder, volume-relative, with a trailing slash
    launcher: str | None  # a file beside it that the release also ships
    skipped: tuple[str, ...]  # in the archive, never written (KOReader's update skips them too)

    @property
    def accepted(self) -> tuple[str, ...]:
        return (self.prefix, self.launcher) if self.launcher else (self.prefix,)


# PocketBook (v2026.07.1 zip): applications/koreader/…, applications/koreader.app (in the
# index), system/bin/koreader.app (a stub; not in the index, KOReader's update skips it).
LAYOUTS = {
    "pocketbook": Layout("applications/koreader/", "applications/koreader.app", ("system/",)),
}


def layout_of(install: KOReaderInstall) -> Layout:
    layout = LAYOUTS.get(install.platform)
    if layout is None:
        raise UpdateError(
            "kodesk updates KOReader on PocketBook readers so far; on this reader, use "
            "KOReader's own update (Tools → Update)."
        )
    return layout


@dataclass(frozen=True)
class UpToDate:
    """The reader already runs the latest stable release (or a newer build)."""

    installed: str | None
    latest: str

    @property
    def text(self) -> str:
        if koversion.is_newer(self.installed or "", self.latest):
            return f"Up to date (newer than the latest stable release {self.latest})"
        return "Up to date"


@dataclass(frozen=True)
class UpdatePlan:  # pylint: disable=too-many-instance-attributes  # what the preview shows
    """What an update writes. Paths are relative to the reader's volume."""

    installed: str | None
    release: release.Release
    staged: StagedRelease
    add: tuple[str, ...] = ()
    replace: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()  # of replace + remove: files the user changed
    edits_known: bool = False  # False: the installed version's release was not available
    conflicts: tuple[str, ...] = ()  # third-party plugins named like a new built-in one
    stale: tuple[str, ...] = ()  # left-overs removed too (KOReader's own update base)
    unchanged: int = field(default=0)

    def __bool__(self) -> bool:
        return bool(self.add or self.replace or self.remove)


def _read_index(install: KOReaderInstall) -> list[str]:
    path = install.root / INDEX
    try:
        if not path.is_file() or path.stat().st_size > MAX_INDEX:
            raise UpdateError(
                f"The reader has no {INDEX} (the file list of the installed KOReader), so "
                "kodesk cannot tell KOReader's files from yours and does not update it."
            )
        return index_paths(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ArchiveError) as error:
        raise UpdateError(f"{INDEX} on the reader is unreadable: {error}") from error


def _plugins(paths: set[str] | list[str], prefix: str) -> set[str]:
    return {
        match["name"]
        for path in paths
        if path.startswith(prefix) and (match := _PLUGIN.match(path.removeprefix(prefix)))
    }


def _accepted(path: str, layout: Layout) -> bool:
    return path.startswith(layout.prefix) or path == layout.launcher


def plan_update(
    install: KOReaderInstall,
    latest: release.Release,
    staged: StagedRelease,
    reference: Mapping[str, str] | None,
) -> UpdatePlan:
    """Compare the unpacked release with the reader. ``reference``: the installed version's own
    files (path → SHA-256), to tell the user's edits from changes of the new version."""
    layout = layout_of(install)
    volume = volume_root(install)
    old = set(_read_index(install))
    new = set(staged.files)
    for path in sorted(new):
        inside = path.removeprefix(layout.prefix)
        if path.startswith(layout.prefix) and is_protected(inside, frozenset()):
            raise UpdateError(f"The release would write {inside}, which kodesk never changes.")
    new_built_in = _plugins(new, layout.prefix) - _plugins(old, layout.prefix)
    conflicts = sorted(
        name
        for name in new_built_in
        if (volume / layout.prefix / "plugins" / (name + PLUGIN_SUFFIX)).exists()
    )
    add, replace, unchanged = [], [], 0
    for path in sorted(new):
        target = volume / path
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise UpdateError(f"{path} on the reader is not a plain file")
        if not target.exists():
            add.append(path)
        elif file_sha256(target) != staged.files[path]:
            replace.append(path)
        else:
            unchanged += 1
    remove = sorted(
        path
        for path in old - new
        if _accepted(path, layout)
        and (volume / path).is_file()
        and not (volume / path).is_symlink()
    )
    edited: list[str] = []
    if reference is not None:
        edited = [
            path
            for path in sorted((*replace, *remove))
            if reference.get(path) != file_sha256(volume / path)
        ]
    stale = layout.prefix + INSTALLED_TAR
    plan = UpdatePlan(
        installed=install.version,
        release=latest,
        staged=staged,
        add=tuple(add),
        replace=tuple(replace),
        remove=tuple(remove),
        edited=tuple(edited),
        edits_known=reference is not None,
        conflicts=tuple(conflicts),
        stale=(stale,) if (volume / stale).is_file() else (),
        unchanged=unchanged,
    )
    _check_space(volume, plan)
    return plan


def _check_space(volume: Path, plan: UpdatePlan) -> None:
    sizes = plan.staged.sizes
    grow = sum(sizes[p] for p in plan.add) + sum(
        max(0, sizes[p] - (volume / p).stat().st_size) for p in plan.replace
    )
    largest = max((sizes[p] for p in (*plan.add, *plan.replace)), default=0)
    needed = grow + largest + SPACE_MARGIN  # each file is written beside its target first
    free = shutil.disk_usage(volume).free
    if free < needed:
        raise UpdateError(
            f"Not enough free space on the reader: {needed // 2**20} MB needed, "
            f"{free // 2**20} MB free."
        )


def _order(paths: list[str], layout: Layout) -> list[str]:
    last = [layout.prefix + name for name in WRITTEN_LAST]
    return [p for p in paths if p not in last] + [p for p in last if p in paths]


def _remove_empty_dirs(folder: Path, stop: Path) -> None:
    while folder != stop and folder.is_relative_to(stop) and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


def apply_update(
    store: SnapshotStore,
    install: KOReaderInstall,
    plan: UpdatePlan,
    keep: frozenset[str],
    progress: Progress = no_progress,
) -> Snapshot:
    """Snapshot, write the new release (except the ``keep`` files the user changed), verify.

    The snapshot is restored on any failure; :data:`PENDING_FILE` marks the run meanwhile.
    """
    if plan.conflicts:
        raise UpdateError("remove the conflicting plugins first: " + ", ".join(plan.conflicts))
    if not keep <= set(plan.edited):
        raise UpdateError("only files you changed can be kept")
    layout = layout_of(install)
    volume = volume_root(install)
    root = volume.resolve()
    writes = _order([p for p in (*plan.add, *plan.replace) if p not in keep], layout)
    removes = [p for p in plan.remove if p not in keep] + list(plan.stale)

    def target(path: str) -> Path:
        resolved = (volume / path).resolve()
        if not _accepted(path, layout) or not resolved.is_relative_to(root):
            raise UpdateError(f"refusing to write outside KOReader's folder: {path}")
        return volume / path

    def write() -> None:
        remember(store.base, plan.installed, plan.release.tag, store.snapshots()[0].id)
        for path in writes:
            written = target(path)
            write_file(written, plan.staged.folder / path)
            if file_sha256(written) != plan.staged.files[path]:
                raise OSError(f"{path} reads back differently after writing")
        for path in removes:
            removed = target(path)
            removed.unlink()
            _remove_empty_dirs(removed.parent, install.root)

    launcher = layout.launcher
    outside = (PurePosixPath(launcher).name,) if launcher and launcher in writes else ()
    try:
        snapshot = guarded_write(store, install.root, write, (), outside, progress)
    except WriteRejectedError:
        clear(store.base)  # restored: nothing is pending
        raise
    clear(store.base)
    return snapshot


def check_launcher(install: KOReaderInstall) -> None:
    """Refuse unless the reader's launcher installs ``ota/koreader.updated.tar`` as kodesk knows."""
    layout = layout_of(install)
    text = ""
    if layout.launcher is not None:
        path = volume_root(install) / layout.launcher
        try:
            if path.is_file() and path.stat().st_size <= MAX_LAUNCHER:
                text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if not all(step in text for step in _OTA_STEPS):
        raise UpdateError(
            "KOReader on this reader installs updates in a way kodesk does not know yet, so it "
            "cannot be updated over Wi-Fi. Update it over USB."
        )


def ota_archive(
    install: KOReaderInstall, plan: UpdatePlan, keep: frozenset[str], folder: Path
) -> Path:
    """The update as one tar for KOReader's launcher (over Wi-Fi): the files to write, paths
    relative to the reader's volume, ``git-rev`` and ``ota/package.index`` last."""
    if plan.conflicts:
        raise UpdateError("remove the conflicting plugins first: " + ", ".join(plan.conflicts))
    if not keep <= set(plan.edited):
        raise UpdateError("only files you changed can be kept")
    dropped = sorted(keep & set(plan.remove))
    if dropped:
        raise UpdateError(
            "Over Wi-Fi KOReader itself removes the files its new version no longer has, so "
            f"these cannot be kept: {', '.join(dropped[:5])}. Update over USB to keep them."
        )
    layout = layout_of(install)
    check_launcher(install)
    writes = _order([p for p in (*plan.add, *plan.replace) if p not in keep], layout)
    path = folder / PurePosixPath(OTA_UPDATE).name
    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as archive:
        for relative in writes:
            source = plan.staged.folder / relative
            if not _accepted(relative, layout) or source.is_symlink():
                raise UpdateError(f"refusing to pack {relative}")
            if file_sha256(source) != plan.staged.files[relative]:
                raise UpdateError(f"{relative} changed on this PC since it was checked")
            info = archive.gettarinfo(source, arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if info.mode & 0o111 else 0o644
            with source.open("rb") as handle:
                archive.addfile(info, handle)
    return path


def prepare(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    install: KOReaderInstall,
    get: Get,
    save: Save,
    staging: Path,
    cancelled: Callable[[], bool],
    progress: Callable[[str], None] = lambda _text: None,
) -> UpdatePlan | UpToDate:
    """Find, download, check and unpack the latest release on the PC, then plan the update."""
    layout = layout_of(install)
    latest = release.latest(install.platform, get)
    if install.version and not koversion.is_newer(latest.tag, install.version):
        return UpToDate(install.version, latest.tag)
    progress(f"Downloading KOReader {latest.tag} ({latest.megabytes} MB)…")
    staging.mkdir(parents=True, exist_ok=True)
    archive = release.fetch(latest, save, staging, cancelled)
    progress("Checking and unpacking it on this PC…")
    staged = stage_release(archive, staging / "new", layout.accepted, layout.skipped)
    archive.unlink()
    reference = None
    old = release.for_tag(install.version, install.platform, get) if install.version else None
    if old is not None:
        progress(f"Downloading KOReader {old.tag}, to find files you changed ({old.megabytes} MB)…")
        old_archive = release.fetch(old, save, staging, cancelled)
        reference = release_hashes(old_archive, layout.accepted, layout.skipped)
        old_archive.unlink()
    progress("Comparing with your reader…")
    return plan_update(install, latest, staged, reference)


def check(install: KOReaderInstall, get: Get) -> str:
    """One line for the Installed tab: a newer stable release, up to date, or why unknown."""
    try:
        layout_of(install)
        latest = release.latest(install.platform, get)
    except (UpdateError, NetError) as error:
        return f"Not checked: {error}"
    if install.version and not koversion.is_newer(latest.tag, install.version):
        return UpToDate(install.version, latest.tag).text
    return f"{latest.tag} available"


# -- the marker of a running update -------------------------------------------------------


@dataclass(frozen=True)
class PendingUpdate:
    """An update kodesk started and did not finish; the snapshot taken before it."""

    installed: str
    target: str
    snapshot_id: str


def remember(base: Path, installed: str | None, target: str, snapshot_id: str) -> None:
    """Mark an update as running (written before the first file on the reader changes)."""
    base.mkdir(parents=True, exist_ok=True)
    data = {"from": installed or "", "to": target, "snapshot": snapshot_id}
    temp = base / (PENDING_FILE + ".kodesk-tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    temp.replace(base / PENDING_FILE)


def pending(base: Path) -> PendingUpdate | None:
    """An update that did not finish (e.g. the cable was pulled), or None."""
    try:
        data = json.loads((base / PENDING_FILE).read_text(encoding="utf-8"))
        found = PendingUpdate(str(data["from"])[:40], str(data["to"])[:40], str(data["snapshot"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None  # missing or damaged: it only ever offers help
    return found if _SNAPSHOT_ID.match(found.snapshot_id) else None


def clear(base: Path) -> None:
    (base / PENDING_FILE).unlink(missing_ok=True)
