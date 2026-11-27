"""Write the Tuning-Garage's changes into ``settings.reader.lua`` and, for hot corners (B-22),
``settings/gestures.lua`` (D-11, B-04 pipeline).

Only the keys the user visibly changed differ (``SettingsDocument.set`` edits the text in
place); everything else stays byte-identical. The write runs inside :func:`guarded_write`:
snapshot first, then a byte-for-byte check that no other protected file changed.

KOReader keeps its settings in memory and writes the whole file when it exits or goes to the
background (research R-01), so the file on the reader must still be exactly what kodesk read.
If it is not, KOReader (or something else) wrote it in between and kodesk refuses to write.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from kodesk.core.hotcorners import GESTURES_FILE
from kodesk.core.luadump import SettingsDocument
from kodesk.core.snapshot import Progress, Snapshot, SnapshotStore, guarded_write, no_progress

SETTINGS_FILE = "settings.reader.lua"
WRITABLE = frozenset({SETTINGS_FILE, GESTURES_FILE})  # least privilege: nothing else
TEMP_SUFFIX = ".kodesk-tmp"


class StaleSettingsError(Exception):
    """The settings file on the reader changed since kodesk read it."""


Changes = Mapping[str, tuple[SettingsDocument, SettingsDocument]]  # file → (before, after)


def write_settings(
    store: SnapshotStore,
    koreader_dir: Path,
    before: SettingsDocument,
    after: SettingsDocument,
    progress: Progress = no_progress,
) -> Snapshot:
    """Replace the settings file with ``after``; returns the snapshot taken first."""
    return write_documents(store, koreader_dir, {SETTINGS_FILE: (before, after)}, progress)


def write_documents(
    store: SnapshotStore,
    koreader_dir: Path,
    changes: Changes,
    progress: Progress = no_progress,
) -> Snapshot:
    """Write several settings files (only those in :data:`WRITABLE`) inside one snapshot."""
    unknown = set(changes) - WRITABLE
    if unknown:
        raise ValueError(f"kodesk does not write {sorted(unknown)}")
    for name, (before, _after) in changes.items():
        target = koreader_dir / name
        if target.is_file() and target.read_bytes() != before.to_bytes():
            raise StaleSettingsError(
                f"{name} on your reader changed since kodesk read it (is KOReader still "
                "open on the reader?). Nothing was written; reopen the reader in kodesk."
            )

    def write() -> None:
        for name, (_before, after) in changes.items():
            target = koreader_dir / name
            new = after.to_bytes()
            temp = target.with_name(target.name + TEMP_SUFFIX)
            temp.write_bytes(new)  # vfat: write beside the target, then replace
            temp.replace(target)
            if target.read_bytes() != new:
                raise OSError(f"{name} reads back differently after writing")

    return guarded_write(store, koreader_dir, write, allowed=set(changes), progress=progress)
