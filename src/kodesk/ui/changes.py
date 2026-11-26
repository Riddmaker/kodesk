"""Install, update and remove from the GUI (B-08, B-09): prepare, preview, write.

Downloading and unpacking run in the background; the preview is a dialog whose accept button
names the action; the write (snapshot, copy, check) runs with the wait cursor, like a restore.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QInputDialog, QMessageBox, QWidget

from kodesk import APP_NAME
from kodesk.core import installer, sources
from kodesk.core.archive import ArchiveError, stage_plugin
from kodesk.core.catalogue import Catalogue, Entry, Kind
from kodesk.core.install import KOReaderInstall
from kodesk.core.installer import Action, Change, InstallerError
from kodesk.core.net import Get, NetError
from kodesk.core.records import Record, Records, RecordsError
from kodesk.core.snapshot import SnapshotError, SnapshotStore
from kodesk.ui.snapshots import MAX_LISTED, busy
from kodesk.ui.store import Results, run_in_background

ACTION_WORD = {Action.INSTALL: "Install", Action.UPDATE: "Update", Action.REMOVE: "Remove"}
DONE_WORD = {Action.INSTALL: "Installed", Action.UPDATE: "Updated", Action.REMOVE: "Removed"}
CATALOGUE_LOADING = "The catalogue is still loading; try again in a moment."
FAILURES = (NetError, ArchiveError, InstallerError, RecordsError, SnapshotError, OSError)


@dataclass(frozen=True)
class Prepared:
    """A planned change and where it comes from, ready for the preview."""

    change: Change
    entry: Entry | None = None
    download: sources.Download | None = None
    guessed: bool = False  # source found by name only
    staging: Path | None = None


def preview_text(prepared: Prepared) -> tuple[str, str, str]:
    """(question, explanation, details) for the preview dialog."""
    change, entry, download = prepared.change, prepared.entry, prepared.download
    what = f"patch {change.name}" if change.kind is Kind.PATCH else change.name
    version = f" {download.version}" if download else ""
    question = f"{ACTION_WORD[change.action]} {what}{version}?"
    lines = []
    if entry is not None:
        lines.append(
            f"From {entry.url} ({entry.license or 'no licence given'}). "
            "This is code by a third party; it will run on your reader."
        )
    if download is not None:
        lines.append(f"The download is {download.check}.")
    if prepared.guessed:
        lines.append(
            "The source was found by the name alone. If the plugin came from elsewhere, cancel "
            "and use Set source… first."
        )
    counts = [
        f"{len(paths)} {label}"
        for paths, label in (
            (change.add, "added"),
            (change.replace, "replaced"),
            (change.remove, "removed"),
            (change.keep, "kept (made by the plugin)"),
        )
        if paths
    ]
    if counts:
        lines.append("Files: " + ", ".join(counts) + ".")
    lines += change.notes
    lines.append("kodesk takes a snapshot first; Reader → Snapshots… brings everything back.")
    details = []
    for title, paths in (
        ("Added", change.add),
        ("Replaced", change.replace),
        ("Removed", change.remove),
        ("Kept", change.keep),
    ):
        if paths:
            details.append(f"{title} ({len(paths)}):")
            details += [f"  {p}" for p in paths[:MAX_LISTED]]
            if len(paths) > MAX_LISTED:
                details.append(f"  … and {len(paths) - MAX_LISTED} more")
    if change.data is not None:
        details += ["", "Code:", change.data.decode("utf-8", errors="replace")]
    return question, "\n\n".join(lines), "\n".join(details)  # one paragraph per point


def prepare_entry(
    koreader_dir: Path, entry: Entry, records: Records, get: Get, guessed: bool = False
) -> Prepared:
    """Download and plan an install or update of ``entry`` (runs in the background)."""
    if entry.kind is Kind.PATCH:
        patch = sources.fetch_patch(entry, get)
        change = installer.plan_patch(koreader_dir, entry, patch.data, patch.matches_index)
        return Prepared(change, entry, guessed=guessed)
    download = sources.latest(entry, get)
    data = sources.fetch(download, get)
    staging = Path(tempfile.mkdtemp(prefix="kodesk-"))  # on the PC, removed after the write
    try:
        staged = stage_plugin(data, entry.repo, staging)
        previous = records.get(Kind.PLUGIN, staged.name)
        change = installer.plan_plugin(koreader_dir, staged, entry, download, previous)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return Prepared(change, entry, download, guessed, staging)


def check_updates(
    koreader_dir: Path,
    items: list[tuple[tuple[Kind, str], Entry, Record | None]],
    get: Get,
) -> dict[tuple[Kind, str], str]:
    """One line per item: newer version available, up to date, or why it is unknown."""
    result: dict[tuple[Kind, str], str] = {}
    for key, entry, record in items:
        try:
            if entry.kind is Kind.PATCH:
                patch = sources.fetch_patch(entry, get)
                current = (koreader_dir / "patches" / key[1]).read_bytes()
                result[key] = "Up to date" if patch.data == current else "Newer file available"
                continue
            download = sources.latest(entry, get)
            if record is None or not record.version:
                result[key] = f"Latest {download.version}; installed version unknown"
            elif sources.is_newer(record.tag, record.commit, download):
                result[key] = f"{download.version} available"
            else:
                result[key] = "Up to date"
        except (NetError, OSError) as error:
            result[key] = f"Not checked: {error}"
            if getattr(error, "status", None) in (403, 429):
                break  # rate limit: the next requests would fail too
    return result


class ChangeRunner(QObject):
    """Runs installs, updates and removals for the main window."""

    def __init__(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        parent: QWidget,
        get: Get | None,
        catalogue: Callable[[], Catalogue | None],
        store_for: Callable[[KOReaderInstall], SnapshotStore],
        changed: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.window = parent
        self.get = get
        self.catalogue = catalogue
        self.store_for = store_for
        self.changed = changed
        self.install: KOReaderInstall | None = None
        self.busy = False
        self._results = Results(self)
        self._results.done.connect(self._prepared)
        self._checked: Callable[[dict[tuple[Kind, str], str]], None] | None = None

    # -- state -------------------------------------------------------------------------

    def records(self) -> Records | None:
        if self.install is None:
            return None
        try:
            return Records(self.store_for(self.install).base)
        except RecordsError as error:
            self._warn(str(error))
            return None

    def why_not(self) -> str | None:
        """Why nothing can be installed right now, or None."""
        if self.install is None:
            return "Connect a reader first (File → Open reader…)."
        if self.get is None:
            return "kodesk runs offline (--offline); installing needs the network."
        if self.busy:
            return "kodesk is still preparing the last request."
        return None

    def source_of(self, kind: Kind, name: str) -> tuple[Entry | None, Record | None, bool]:
        """(catalogue entry, record, guessed) for an installed item."""
        records = self.records()
        record = records.get(kind, name) if records else None
        catalogue = self.catalogue()
        entries = catalogue.entries if catalogue else ()
        if record is not None:
            wanted = f"{record.owner}/{record.repo}"
            if kind is Kind.PATCH and record.path:
                wanted += f":{record.path}"
            match = [e for e in entries if e.kind is kind and e.id == wanted]
            return (match[0] if match else None), record, False
        guesses = sources.probable_sources(entries, kind, name)
        return (guesses[0] if len(guesses) == 1 else None), None, True

    # -- install and update ------------------------------------------------------------

    def install_entry(self, entry: Entry) -> None:
        """From the Store: download, preview, install (or update if already there)."""
        self._start(entry, guessed=False)

    def update(self, key: tuple[Kind, str]) -> None:
        """From the Installed tab: the latest version from the item's source."""
        if self.catalogue() is None:
            self._warn(CATALOGUE_LOADING)
            return
        entry, _record, guessed = self.source_of(*key)
        if entry is None:
            self._warn(
                f"kodesk does not know where {key[1]} came from. Use Set source… to name it."
            )
            return
        self._start(entry, guessed)

    def _start(self, entry: Entry, guessed: bool) -> None:
        reason = self.why_not()
        records = self.records()
        if reason is not None or self.install is None or self.get is None or records is None:
            self._warn(reason or "No reader.")
            return
        root, get = self.install.root, self.get
        self.busy = True
        run_in_background(
            self._results, "prepare", lambda: prepare_entry(root, entry, records, get, guessed)
        )

    def _prepared(self, job: str, result: object) -> None:
        if job == "check":
            if isinstance(result, dict) and self._checked is not None:
                self._checked(result)
            elif not isinstance(result, dict):
                self._warn(f"Could not check for updates: {result}")
            self.busy = False
            return
        self.busy = False
        if isinstance(result, Prepared):
            try:
                self.offer(result)
            finally:
                if result.staging is not None:
                    shutil.rmtree(result.staging, ignore_errors=True)
        else:
            self._warn(f"Nothing was changed: {result}")

    def offer(self, prepared: Prepared) -> bool:
        """Preview; write if confirmed. Returns whether it was written."""
        change = prepared.change
        if not change:
            self._info(f"{change.name} on your reader is already this version.")
            return False
        if not self.confirm(prepared):
            return False
        return self.write(change)

    def confirm(self, prepared: Prepared) -> bool:
        """The preview: what is added, replaced, removed and kept; a button naming the action."""
        question, text, details = preview_text(prepared)
        box = QMessageBox(self.window)
        box.setObjectName("change-preview")
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(question)
        box.setInformativeText(text)
        box.setDetailedText(details)
        change = prepared.change
        what = f"patch {change.name}" if change.kind is Kind.PATCH else change.name
        accept = box.addButton(
            f"{ACTION_WORD[change.action]} {what}", QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is accept

    def write(self, change: Change) -> bool:
        """Snapshot, write, verify (restored on any problem)."""
        records = self.records()
        if self.install is None or records is None:
            return False
        store, root = self.store_for(self.install), self.install.root
        try:
            busy(lambda: installer.apply(store, root, change, records))
        except FAILURES as error:
            self._warn(f"{ACTION_WORD[change.action]} failed, your reader is as before: {error}")
            self.changed()
            return False
        self.changed()
        self._info(
            f"{DONE_WORD[change.action]} {change.name}. The snapshot taken before is kept "
            "(Reader → Snapshots…); after the next start of KOReader, kodesk checks that it "
            "loaded."
        )
        return True

    # -- remove, source, check -----------------------------------------------------------

    def remove(self, key: tuple[Kind, str]) -> None:
        """Preview and remove a plugin folder or a patch file."""
        records = self.records()
        if self.install is None or records is None:
            return
        try:
            change = installer.plan_remove(self.install.root, *key, records.get(*key))
        except (InstallerError, OSError) as error:
            self._warn(str(error))
            return
        if self.confirm(Prepared(change)):
            self.write(change)

    def set_source(self, key: tuple[Kind, str]) -> None:
        """Name the repo an installed item came from (stored on the PC)."""
        catalogue, records = self.catalogue(), self.records()
        if catalogue is None:
            self._warn(CATALOGUE_LOADING)
            return
        if records is None:
            return
        kind, name = key
        guesses = [e.id for e in sources.probable_sources(catalogue.entries, kind, name)]
        others = sorted(e.id for e in catalogue.entries if e.kind is kind and e.id not in guesses)
        chosen = self.ask_source(name, guesses + others)
        if not chosen:
            return
        entry = next((e for e in catalogue.entries if e.kind is kind and e.id == chosen), None)
        if entry is None:
            self._warn(f"{chosen} is not in the catalogue.")
            return
        path = entry.id.split(":", 1)[1] if kind is Kind.PATCH else None
        records.put(Record(kind, name, entry.owner, entry.repo, path=path))
        self.changed()

    def ask_source(self, name: str, choices: list[str]) -> str | None:
        """A searchable list of catalogue entries, best guesses first."""
        chosen, ok = QInputDialog.getItem(
            self.window, APP_NAME, f"Where did {name} come from? (owner/repository)", choices, 0
        )
        return chosen if ok else None

    def check(
        self,
        keys: list[tuple[Kind, str]],
        done: Callable[[dict[tuple[Kind, str], str]], None],
    ) -> None:
        """Ask GitHub for the latest version of each item with a known or guessed source."""
        reason = self.why_not()
        if reason is not None or self.install is None or self.get is None:
            self._warn(reason or "No reader.")
            return
        if self.catalogue() is None:
            self._warn(CATALOGUE_LOADING)
            return
        items, unknown = [], {}
        for key in keys:
            entry, record, _guessed = self.source_of(*key)
            if entry is None:
                unknown[key] = "Source unknown"
            else:
                items.append((key, entry, record))
        root, get = self.install.root, self.get
        self.busy = True
        self._checked = lambda found: done({**unknown, **found})
        run_in_background(self._results, "check", lambda: check_updates(root, items, get))

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self.window, APP_NAME, text)

    def _info(self, text: str) -> None:
        QMessageBox.information(self.window, APP_NAME, text)
