"""Write the Tuning-Garage's changes into ``settings.reader.lua`` (D-11, B-04 pipeline).

Only the keys the user visibly changed differ (``SettingsDocument.set`` edits the text in
place); everything else stays byte-identical. The write runs inside :func:`guarded_write`:
snapshot first, then a byte-for-byte check that no other protected file changed.

KOReader keeps its settings in memory and writes the whole file when it exits or goes to the
background (research R-01), so the file on the reader must still be exactly what kodesk read.
If it is not, KOReader (or something else) wrote it in between and kodesk refuses to write.
"""

from __future__ import annotations

from pathlib import Path

from kodesk.core.luadump import SettingsDocument
from kodesk.core.snapshot import Snapshot, SnapshotStore, guarded_write

SETTINGS_FILE = "settings.reader.lua"
TEMP_SUFFIX = ".kodesk-tmp"


class StaleSettingsError(Exception):
    """The settings file on the reader changed since kodesk read it."""


def write_settings(
    store: SnapshotStore, koreader_dir: Path, before: SettingsDocument, after: SettingsDocument
) -> Snapshot:
    """Replace the settings file with ``after``; returns the snapshot taken first."""
    target = koreader_dir / SETTINGS_FILE
    if target.is_file() and target.read_bytes() != before.to_bytes():
        raise StaleSettingsError(
            "The settings on your reader changed since kodesk read them (is KOReader still "
            "open on the reader?). Nothing was written; reopen the reader in kodesk."
        )
    new = after.to_bytes()

    def write() -> None:
        temp = target.with_name(target.name + TEMP_SUFFIX)
        temp.write_bytes(new)  # vfat: write beside the target, then replace
        temp.replace(target)
        if target.read_bytes() != new:
            raise OSError(f"{SETTINGS_FILE} reads back differently after writing")

    return guarded_write(store, koreader_dir, write, allowed={SETTINGS_FILE})
