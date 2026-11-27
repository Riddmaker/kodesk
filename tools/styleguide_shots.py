"""Screenshots of the real widgets for docs/STYLEGUIDE.html (fake reader, never the real one).

    nix develop -c python tools/styleguide_shots.py OUTDIR [SUFFIX]

Uses whatever Qt style the environment selects: run once plain (Qt's own style, as in CI) and
once in the desktop session (e.g. Kvantum Catppuccin) to show that kodesk follows the theme.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QProgressDialog,
    QScrollArea,
    QSlider,
    QToolButton,
)

from kodesk.core import catalogue as cat
from kodesk.core import healthcheck, update
from kodesk.core.install import find_install
from kodesk.core.records import Records
from kodesk.core.snapshot import Progress
from kodesk.ui.app import MainWindow, reader_context
from kodesk.ui.changes import prepare_entry
from kodesk.ui.garage import TuningGarage, View
from kodesk.ui.koupdate import UpdateDialog
from kodesk.ui.snapshots import with_snapshot_progress

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.conftest import (  # pylint: disable=wrong-import-position
    FakeGitHub,
    fake_catalogue,
    make_pocketbook,
    make_reader,
    plugin_files,
    plugin_zip,
)
from tests.test_update import EDITED, koreader_github  # pylint: disable=wrong-import-position


def main(argv: list[str]) -> int:
    """Open the garage on a fake reader and save one picture per documented state."""
    out = Path(argv[0])
    suffix = f"-{argv[1]}" if len(argv) > 1 else ""
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(["kodesk-styleguide"])
    with tempfile.TemporaryDirectory() as tmp:
        install = find_install(make_pocketbook(Path(tmp) / "PB634K3"))
        window = MainWindow(reader_context(install), install, offline=True)  # never online
        window.resize(1400, 900)
        window.show()
        garage = window.garage

        def shot(name: str) -> None:
            app.processEvents()
            window.grab().save(str(out / f"{name}{suffix}.png"))

        shot("garage-book")
        garage.select_key("next")
        garage.device_view.keys["next"].setFocus()
        slider = garage.findChild(QSlider, "font-size")
        assert slider is not None
        slider.setValue(slider.value() + 4)
        shot("garage-key-and-diff")

        QStandardPaths.setTestModeEnabled(True)  # a write would snapshot into a test folder
        grab_modal(app, out / f"write-preview{suffix}.png")  # picture it, then cancel (no write)
        garage.write_changes()
        garage.select_key("home")
        shot("garage-home-locked")
        garage.set_view(View.MENU)
        garage.select_spot("icons")
        shot("garage-icons-locked")
        garage.set_view(View.BOOK)
        for side in ("Bottom edge", "Front"):  # click the switch like a user would
            button = garage.findChild(QToolButton, f"seg-{side}")
            assert button is not None
            button.click()
            if side != "Front":
                shot("garage-bottom-edge")
        status_bar_shot(garage)
        shot("garage-status-bars")
        library_shots(garage, shot)
        corners_shot(garage, shot)
        garage.set_view(View.SLEEP)
        shot("garage-sleep")
        snapshot_shots(app, window, out, suffix)
        snapshot_progress_shot(app, window, out, suffix)
        store_shot(app, window, out, suffix)
        install_preview_shot(app, window, out, suffix)
        installed_shot(app, window, out, suffix)
        koreader_update_shot(app, window, out, suffix)
        window.close()
    return 0


def library_shots(garage: TuningGarage, shot: Callable[[str], None]) -> None:
    """The library as Cover browser draws it by default, then switched to the mosaic."""
    garage.set_view(View.LIBRARY)
    shot("garage-library-list")
    mode = garage.library_panel.combo
    mode.setCurrentIndex(mode.findData("mosaic_image"))
    mode.setFocus()
    shot("garage-library-mosaic")


def corners_shot(garage: TuningGarage, shot: Callable[[str], None]) -> None:
    """The Book view with the bottom-right corner chosen: its field focused in the panel."""
    garage.set_view(View.BOOK)
    garage.select_spot("corner-bottom-right")
    shot("garage-corners")


def grab_modal(app: QApplication, path: Path, tries: int = 50) -> None:
    """Once the next modal box is up, picture it and cancel it (waits for slow compositors)."""

    def attempt(left: int) -> None:
        box = app.activeModalWidget()
        if isinstance(box, QDialog):
            app.processEvents()
            box.grab().save(str(path))
            box.reject()
        elif left > 0:
            QTimer.singleShot(100, lambda: attempt(left - 1))
        else:
            raise SystemExit(f"no dialog appeared for {path.name}")

    QTimer.singleShot(100, lambda: attempt(tries))


def status_bar_shot(garage: TuningGarage) -> None:
    """The bottom status bar with a few items switched on, its group scrolled into view."""
    for key in ("all_at_once", "page_progress", "percentage", "time", "battery"):
        box = garage.findChild(QCheckBox, f"footer-{key}")
        assert box is not None
        box.setChecked(True)
    scroll = garage.findChild(QScrollArea, "book-settings")
    bold = garage.findChild(QCheckBox, "footer-text_font_bold")
    assert scroll is not None and bold is not None
    scroll.ensureWidgetVisible(bold)


def snapshot_shots(app: QApplication, window: MainWindow, out: Path, suffix: str) -> None:
    """The Snapshots window, then the health-check warning after a plugin failed to load."""
    dialog = window.show_snapshots()
    assert dialog is not None
    dialog.take()
    app.processEvents()
    dialog.grab().save(str(out / f"snapshots{suffix}.png"))
    healthcheck.remember(dialog.store.base, dialog.koreader_dir, dialog.store.snapshots()[0].id)
    (dialog.koreader_dir / healthcheck.CRASH_LOG).write_text(
        "11/26/26-14:00:01 WARN  Error when loading plugins/projecttitle.koplugin/main.lua: "
        "plugins/projecttitle.koplugin/main.lua:40: attempt to index a nil value\n"
    )
    box = window.check_health()
    assert box is not None
    app.processEvents()
    box.grab().save(str(out / f"health-check{suffix}.png"))
    box.reject()
    test_home = dialog.store.base.parents[3]  # ~/.qttest/share/<app>/snapshots/<id>
    shutil.rmtree(dialog.store.base.parents[1])
    for folder in (test_home / "share", test_home):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    dialog.close()


def snapshot_progress_shot(app: QApplication, window: MainWindow, out: Path, suffix: str) -> None:
    """The progress dialog while a snapshot reads the reader (no file is read or written)."""

    def action(progress: Progress) -> None:
        progress(0, 380 * 2**20)
        progress(152 * 2**20, 380 * 2**20)
        dialog = window.findChild(QProgressDialog, "snapshot-progress")
        assert dialog is not None
        dialog.show()  # shown at once for the picture, not after its usual delay
        app.processEvents()
        dialog.grab().save(str(out / f"snapshot-progress{suffix}.png"))

    with_snapshot_progress(window, action)


def store_shot(app: QApplication, window: MainWindow, out: Path, suffix: str) -> None:
    """The Store tab on the fake catalogue, one entry selected with its README."""
    store = window.store
    store.set_catalogue(cat.parse(*fake_catalogue()))
    window.tabs.setCurrentWidget(store)
    store.search.setText("clock")
    store.enter_results()
    entry = store.current()
    assert entry is not None
    store.details.readme.show_text(entry, README)
    app.processEvents()
    window.grab().save(str(out / f"store{suffix}.png"))


def install_preview_shot(app: QApplication, window: MainWindow, out: Path, suffix: str) -> None:
    """The preview before an install (fake GitHub, cancelled: nothing is written)."""
    install = window.installed.install
    entry = window.store.current()
    assert install is not None and entry is not None
    github = FakeGitHub()
    data = plugin_zip(plugin_files("readingclock.koplugin", "1.2.0"))
    github.release("bob", "readingclock.koplugin", "v1.2.0", {"readingclock.koplugin.zip": data})
    with tempfile.TemporaryDirectory() as records:
        prepared = prepare_entry(install.root, entry, Records(Path(records)), github)
        grab_modal(app, out / f"install-preview{suffix}.png")
        assert not window.changes.confirm(prepared)  # cancelled
        if prepared.staging is not None:
            shutil.rmtree(prepared.staging, ignore_errors=True)


README = """# Reading clock

Shows the time in the corner of the page while you read.

## Install

Copy `readingclock.koplugin` to `koreader/plugins/` and restart KOReader.

- **Tap** the clock to hide it
- See the [settings](https://github.com/bob/readingclock.koplugin#settings)
"""


def installed_shot(app: QApplication, window: MainWindow, out: Path, suffix: str) -> None:
    """The Installed tab with a plugin and two patches the user added."""
    install = window.installed.install
    assert install is not None
    plugin = install.root / "plugins" / "projecttitle.koplugin"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "main.lua").write_text("-- fake\n")
    patches = install.root / "patches"
    patches.mkdir(exist_ok=True)
    (patches / "2-reader-footer-tweak.lua").write_text("-- fake\n")
    (patches / "footer-old.lua").write_text("-- fake\n")
    window.installed.set_install(install)
    # What "Check for updates" says on the owner's reader (v2026.07.2 is a tag, not a release).
    window.installed.set_koreader_status(update.UpToDate("v2026.07.2", "v2026.07.1").text)
    window.tabs.setCurrentWidget(window.installed)
    app.processEvents()
    window.grab().save(str(out / f"installed{suffix}.png"))


def koreader_update_shot(app: QApplication, window: MainWindow, out: Path, suffix: str) -> None:
    """The KOReader update preview: a fake reader one release behind, one file edited by the
    user (fake GitHub, cancelled: nothing is written)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        koreader = make_reader(root / "PB634K3", "pocketbook")
        (root / "PB634K3" / EDITED).write_text("/* mine */\n")
        github = koreader_github(root)
        install = find_install(koreader)
        plan = update.prepare(install, github, github.save, root / "staging", lambda: False)
        assert isinstance(plan, update.UpdatePlan)
        dialog = UpdateDialog(plan, window)
        grab_modal(app, out / f"koreader-update{suffix}.png")
        assert dialog.exec() == QDialog.DialogCode.Rejected


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
