"""Screenshots of the real widgets for docs/STYLEGUIDE.html (fake reader, never the real one).

    nix develop -c python tools/styleguide_shots.py OUTDIR [SUFFIX]

Uses whatever Qt style the environment selects: run once plain (Qt's own style, as in CI) and
once in the desktop session (e.g. Kvantum Catppuccin) to show that kodesk follows the theme.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSlider, QToolButton

from kodesk.core import catalogue as cat
from kodesk.core import healthcheck
from kodesk.core.install import find_install
from kodesk.ui.app import MainWindow, reader_context
from kodesk.ui.garage import View

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.conftest import (  # pylint: disable=wrong-import-position
    fake_catalogue,
    make_pocketbook,
)


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

        def grab_preview() -> None:  # the modal preview: picture it, then cancel (no write)
            box = app.activeModalWidget()
            assert isinstance(box, QMessageBox)
            box.grab().save(str(out / f"write-preview{suffix}.png"))
            box.reject()

        QStandardPaths.setTestModeEnabled(True)  # a write would snapshot into a test folder
        QTimer.singleShot(300, grab_preview)
        garage.write_changes()
        garage.select_key("home")
        shot("garage-home-locked")
        for side in ("Bottom edge", "Front"):  # click the switch like a user would
            button = garage.findChild(QToolButton, f"seg-{side}")
            assert button is not None
            button.click()
            if side != "Front":
                shot("garage-bottom-edge")
        garage.set_view(View.SLEEP)
        shot("garage-sleep")
        snapshot_shots(app, window, out, suffix)
        store_shot(app, window, out, suffix)
        installed_shot(app, window, out, suffix)
        window.close()
    return 0


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
    window.tabs.setCurrentWidget(window.installed)
    app.processEvents()
    window.grab().save(str(out / f"installed{suffix}.png"))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
