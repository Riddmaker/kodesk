"""The Tuning-Garage over Wi-Fi (B-26): send a change file, and show what KOReader did with it.

The Garage and the Library panel call :meth:`WifiGarage.send_settings` and
:meth:`WifiGarage.send_library` in place of the USB writers. Both write ``kodesk/pending.json``
(and, after the user agreed, the patch that applies it) into the reader's mirror inside a
snapshot; the snapshot store then sends the files to the reader (B-27). KOReader applies the
changes when it starts again; the next connect shows the outcome once (:meth:`show_result`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QMessageBox, QWidget

from kodesk import APP_NAME, __version__
from kodesk.core.install import KOReaderInstall, read_settings_file
from kodesk.core.librarymode import FILEMANAGER_KEY, LibraryLook, LibraryModeError, read_config
from kodesk.core.luadump import LuaParseError, LuaTable
from kodesk.core.remote_garage import (
    FILES,
    PATCH,
    PATCHES_DISABLED,
    PENDING,
    RESULT,
    Change,
    PatchState,
    RemoteGarageError,
    Result,
    library_changes,
    merge,
    new_id,
    not_as_sent,
    patch_source,
    patch_state,
    pending_document,
    read_pending,
    read_result,
    settings_changes,
)
from kodesk.core.settings_write import TEMP_SUFFIX, Changes
from kodesk.core.snapshot import SnapshotCancelledError, SnapshotStore, guarded_write
from kodesk.ui.snapshots import with_snapshot_progress

SOURCE = "https://github.com/Riddmaker/kodesk"
HINT = (
    "KOReader keeps running: it applies the changes when it starts again. On the reader: "
    "Exit → Restart KOReader."
)
SEEN = "result-seen"  # beside the mirror: the result.json already shown (its SHA-256)
MAX_LISTED = 30


def _write(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + TEMP_SUFFIX)
    temp.write_bytes(data)
    temp.replace(path)


class WifiGarage:
    """Garage changes for one reader reached over Wi-Fi (its mirror is ``install.root``)."""

    def __init__(
        self, parent: QWidget, install: KOReaderInstall, store: SnapshotStore, seen: Path
    ) -> None:
        self.parent = parent
        self.install = install
        self.store = store
        self.seen = seen

    @property
    def koreader(self) -> Path:
        return self.install.root

    # -- sending ---------------------------------------------------------------------------

    def send_settings(self, changes: Changes) -> str:
        """The Garage's writer over Wi-Fi."""
        added = [
            change
            for name, (before, after) in changes.items()
            for change in settings_changes(name, before.data, after.data)
        ]
        return self.send(added)

    def send_library(
        self, look: LibraryLook, rows: dict[str, str | None]
    ) -> tuple[str, LibraryLook]:
        """The Library panel's writer over Wi-Fi; the look returned is the one sent."""
        try:
            before = read_config(self.koreader / look.drawer.cache)
        except LibraryModeError as error:
            raise RemoteGarageError(str(error)) from error
        report = self.send(library_changes(look.drawer.cache, before, rows))
        return report, replace(look, mode=rows.get(FILEMANAGER_KEY, look.mode))

    def waiting(self) -> tuple[Change, ...]:
        """Changes sent earlier that KOReader has not applied yet."""
        path = self.koreader / PENDING
        if not path.is_file():
            return ()
        with suppress(RemoteGarageError, OSError):
            return tuple(read_pending(path.read_bytes()))
        return ()  # not ours to read: the patch will refuse it, and a new send replaces it

    def send(self, added: Sequence[Change]) -> str:
        """Merge ``added`` into the waiting changes and send them (after a snapshot)."""
        if (self.koreader / PATCHES_DISABLED).exists():
            raise RemoteGarageError(
                "Patches are switched off on your reader (patches/.patches_disabled), so "
                "KOReader would not apply changes sent over Wi-Fi. Write them over USB."
            )
        pending = self.koreader / PENDING
        changes = merge(self.waiting(), added)
        if not changes and not pending.exists():
            return "Nothing to send: the settings are as on your reader."
        state = patch_state(self.koreader, __version__)
        if state != "current" and not self.consent(state):
            raise SnapshotCancelledError
        source = patch_source(__version__)
        data = pending_document(new_id(), changes) if changes else None

        def write() -> None:
            if state != "current":
                (self.koreader / PATCH).parent.mkdir(exist_ok=True)
                _write(self.koreader / PATCH, source)
            if data is None:
                pending.unlink(missing_ok=True)
            else:
                pending.parent.mkdir(exist_ok=True)
                _write(pending, data)

        with_snapshot_progress(
            self.parent,
            lambda progress: guarded_write(self.store, self.koreader, write, {PATCH}, (), progress),
        )
        if data is None:
            return "Sent: nothing waits for KOReader any more (the changes cancelled out)."
        count = f"{len(changes)} change{'s' if len(changes) != 1 else ''}"
        return f"Sent: {count} wait for KOReader. On the reader: Exit → Restart KOReader."

    def consent(self, state: PatchState) -> bool:
        """Third-party code on the reader: name it, its source and version, then ask."""
        box = QMessageBox(self.parent)
        box.setObjectName("patch-consent")
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Changes over Wi-Fi")
        if state == "missing":
            box.setText(
                "To change settings over Wi-Fi, kodesk puts a small patch on your reader: "
                f"{PATCH} (kodesk {__version__}, source {SOURCE})."
            )
        else:
            box.setText(
                f"Your reader has another version of kodesk's patch ({PATCH}). kodesk replaces "
                f"it with the one of kodesk {__version__} (source {SOURCE})."
            )
        box.setInformativeText(
            "KOReader runs it at every start. It reads only kodesk/pending.json, changes only "
            "the Garage settings listed there, and only those that still have the value kodesk "
            "saw. It keeps a backup, uses no network and runs no commands. You can remove it "
            "like any patch."
        )
        box.setDetailedText(patch_source(__version__).decode())
        action = box.addButton("&Put the patch on the reader", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is action

    # -- the outcome -----------------------------------------------------------------------

    def _files(self) -> dict[str, LuaTable]:
        files = {}
        for name in FILES.values():
            with suppress(OSError, LuaParseError):
                files[name] = read_settings_file(self.koreader / name).data
        return files

    def show_result(self) -> QMessageBox | None:
        """Once per result: what KOReader did with the changes; offers to undo them."""
        try:
            data = (self.koreader / RESULT).read_bytes()
        except OSError:
            return None
        digest = hashlib.sha256(data).hexdigest()
        with suppress(OSError):
            if self.seen.read_text(encoding="ascii").strip() == digest:
                return None
        try:
            result = read_result(data)
        except RemoteGarageError as error:
            result = Result("?", error=f"kodesk cannot read {RESULT} ({error})")
        with suppress(OSError):
            self.seen.write_text(digest, encoding="ascii")
        return self._result_box(result)

    def _result_box(self, result: Result) -> QMessageBox:
        box = QMessageBox(self.parent)
        box.setObjectName("wifi-result")
        box.setWindowTitle(APP_NAME)
        lines = [f"Applied: {c.label()}" for c in result.applied]
        lines += [f"Left alone ({reason}): {c.label()}" for c, reason in result.skipped]
        if result.error is not None:
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText("KOReader did not apply the settings sent over Wi-Fi.")
            box.setInformativeText(
                "Your settings are as they were. Reason: "
                + result.error
                + " (the change file is kept on the reader as kodesk/pending.rejected.json)"
            )
        else:
            box.setIcon(QMessageBox.Icon.Information)
            applied = len(result.applied)
            box.setText(
                f"KOReader applied {applied} setting{'s' if applied != 1 else ''} sent over Wi-Fi."
            )
            notes = []
            skipped = len(result.skipped)
            if skipped:
                notes.append(
                    f"{skipped} {'was' if skipped == 1 else 'were'} left alone: changed on the "
                    "reader in the meantime."
                )
            later = not_as_sent(result.applied, self._files())
            if later:
                notes.append(
                    f"{len(later)} {'has' if len(later) == 1 else 'have'} another value by now "
                    "(changed on the reader)."
                )
                lines += [f"Changed since: {c.label()}" for c in later]
            box.setInformativeText(" ".join(notes))
        if lines:
            box.setDetailedText("\n".join(lines[:MAX_LISTED]))
        undo = None
        if result.applied:
            undo = box.addButton("&Undo these changes…", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.finished.connect(lambda _r: box.clickedButton() is undo and self.undo(result))
        box.open()
        return box

    def undo(self, result: Result) -> str | None:
        """Send the applied changes back (each only where it still has the value sent)."""
        try:
            report = self.send([c.inverse() for c in reversed(result.applied)])
        except SnapshotCancelledError:
            return None
        except Exception as error:  # pylint: disable=broad-exception-caught  # shown, not raised
            QMessageBox.warning(self.parent, APP_NAME, f"Nothing was sent: {error}")
            return None
        QMessageBox.information(self.parent, APP_NAME, report)
        return report
