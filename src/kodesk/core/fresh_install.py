"""Put KOReader on a supported reader that has none yet (B-28); no Qt.

A PocketBook names its USB volume after its model code (``PB634K3``) and has a ``system/``
folder. Without KOReader nothing else on it names the model (KOReader writes ``version.log`` on
its first start), so kodesk offers the install only for the models it knows by that label
(:attr:`kodesk.core.devices.DeviceModel.volume_labels`; owner 2026-11-27).

The install is the official one: the release zip unpacked at the top of the volume
(``applications/koreader/``, the launcher ``applications/koreader.app`` and the stub
``system/bin/koreader.app``), downloaded and checked exactly as for an update
(:mod:`kodesk.core.release`, :mod:`kodesk.core.archive`). Rules:

* Nothing is ever replaced: a file with the same content is left as it is; any other file in
  the way refuses the install before anything is written.
* KOReader's folder first, then ``git-rev`` and ``ota/package.index``, the launcher and the stub
  last, so the reader shows no KOReader until all of it is there; every file is read back.
* On any failure (or Cancel), and when the user undoes it, exactly the files and folders it
  wrote are removed again. No snapshot is needed: nothing of the user's is where it writes.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from kodesk.core import release
from kodesk.core.archive import StagedRelease, stage_release
from kodesk.core.devices import DeviceModel, find_by_volume_label
from kodesk.core.installer import write_file
from kodesk.core.net import Get, Save
from kodesk.core.snapshot import TEMP_SUFFIX, Progress, file_sha256, no_progress
from kodesk.core.update import LAYOUTS, Layout, UpdateError, check_space, write_order

PLATFORM = "pocketbook"  # the only platform kodesk installs on so far
SYSTEM_DIR = "system"  # every PocketBook volume has it


class FreshInstallError(Exception):
    """KOReader is not installed; nothing was written (or all of it was removed again)."""


@dataclass(frozen=True)
class BareReader:
    """A reader kodesk knows, plugged in over USB, without KOReader."""

    volume: Path
    label: str
    device: DeviceModel
    platform: str = PLATFORM

    @property
    def name(self) -> str:
        """For messages and the menu, e.g. "PocketBook Verse Pro Color (PB634K3)"."""
        return f"{self.device.name} ({self.label})"


@dataclass(frozen=True)
class FreshPlan:
    """What the install writes. Paths are relative to the reader's volume."""

    reader: BareReader
    release: release.Release
    staged: StagedRelease
    write: tuple[str, ...]
    present: tuple[str, ...]  # already there with the same content: left as they are

    @property
    def bytes(self) -> int:
        return sum(self.staged.sizes[p] for p in self.write)


@dataclass(frozen=True)
class Installed:
    """What :func:`install` wrote, so that :func:`remove` can take exactly that away again."""

    files: tuple[str, ...]
    folders: tuple[str, ...]  # created by the install, deepest last


def find_bare(volume: Path, label: str) -> BareReader | None:
    """The reader at ``volume`` if it is a model kodesk knows and has no KOReader folder."""
    device = find_by_volume_label(label)
    if device is None:
        return None
    layout = LAYOUTS[PLATFORM]
    try:
        if not (volume / SYSTEM_DIR).is_dir() or (volume / layout.prefix).exists():
            return None
    except OSError:
        return None
    return BareReader(volume, label, device)


def _layout(reader: BareReader) -> Layout:
    return LAYOUTS[reader.platform]


def prepare(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    reader: BareReader,
    get: Get,
    save: Save,
    staging: Path,
    cancelled: Callable[[], bool],
    progress: Callable[[str], None] = lambda _text: None,
) -> FreshPlan:
    """Find, download, check and unpack the latest release on the PC, then plan the install."""
    layout = _layout(reader)
    latest = release.latest(reader.platform, get)
    progress(f"Downloading KOReader {latest.tag} ({latest.megabytes} MB)…")
    staging.mkdir(parents=True, exist_ok=True)
    archive = release.fetch(latest, save, staging, cancelled)
    progress("Checking and unpacking it on this PC…")
    staged = stage_release(
        archive,
        staging / "new",
        (*layout.accepted, *layout.first_install),
        (),  # a first install writes the stub too; anything else in system/ refuses
        layout.first_install,
    )
    archive.unlink()
    return plan_install(reader, latest, staged)


def plan_install(reader: BareReader, latest: release.Release, staged: StagedRelease) -> FreshPlan:
    """Sort the release's files into new and already there; refuse if one is in the way."""
    write, present, blocked = [], [], []
    for path in sorted(staged.files):
        target = reader.volume / path
        if not target.exists() and not target.is_symlink():
            write.append(path)
        elif target.is_file() and file_sha256(target) == staged.files[path]:
            present.append(path)
        else:
            blocked.append(path)
    if blocked:
        raise FreshInstallError(
            f"{blocked[0]} is already on the reader"
            + (f" (and {len(blocked) - 1} more files)" if len(blocked) > 1 else "")
            + ". kodesk never replaces files there; remove them or install KOReader by hand."
        )
    result = FreshPlan(reader, latest, staged, tuple(write), tuple(present))
    largest = max((staged.sizes[p] for p in write), default=0)
    try:
        check_space(reader.volume, result.bytes, largest)
    except UpdateError as error:
        raise FreshInstallError(str(error)) from error
    return result


def _order(plan: FreshPlan) -> list[str]:
    layout = _layout(plan.reader)
    visible = (layout.launcher or "", *layout.first_install)
    inside = write_order([p for p in plan.write if p not in visible], layout)
    return inside + [p for p in visible if p in plan.write]


def install(plan: FreshPlan, progress: Progress = no_progress) -> Installed:
    """Write the release; on any failure remove what was written and re-raise."""
    volume = plan.reader.volume
    files: list[str] = []
    folders: list[str] = []
    total, done = plan.bytes, 0
    try:
        progress(done, total)
        for path in _order(plan):
            target = volume / path
            _make_folders(volume, path, folders)
            if target.exists() or target.is_symlink():
                raise FreshInstallError(f"{path} appeared on the reader meanwhile")
            files.append(path)
            write_file(target, plan.staged.folder / path)
            if file_sha256(target) != plan.staged.files[path]:
                raise OSError(f"{path} reads back differently after writing")
            done += plan.staged.sizes[path]
            progress(done, total)
    except BaseException:
        remove(volume, Installed(tuple(files), tuple(folders)))
        raise
    return Installed(tuple(files), tuple(folders))


def _make_folders(volume: Path, path: str, made: list[str]) -> None:
    parts = path.split("/")[:-1]
    for end in range(1, len(parts) + 1):
        folder = "/".join(parts[:end])
        if not (volume / folder).is_dir():
            (volume / folder).mkdir()
            made.append(folder)


def remove(volume: Path, installed: Installed) -> None:
    """Take away exactly what :func:`install` wrote (folders only if they are empty)."""
    for path in reversed(installed.files):
        with suppress(FileNotFoundError):
            (volume / path).unlink()
        with suppress(FileNotFoundError):
            (volume / (path + TEMP_SUFFIX)).unlink()
    for folder in reversed(installed.folders):
        with suppress(OSError):
            (volume / folder).rmdir()
