"""Screenshots of the real widgets for docs/STYLEGUIDE.html (fake reader, never the real one).

    nix develop -c python tools/styleguide_shots.py OUTDIR [SUFFIX]

Uses whatever Qt style the environment selects: run once plain (Qt's own style, as in CI) and
once in the desktop session (e.g. Kvantum Catppuccin) to show that kodesk follows the theme.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtWidgets import QApplication, QSlider, QToolButton

from kodesk.core.install import find_install
from kodesk.ui.app import MainWindow, reader_context
from kodesk.ui.garage import View

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.conftest import make_pocketbook  # pylint: disable=wrong-import-position


def main(argv: list[str]) -> int:
    """Open the garage on a fake reader and save one picture per documented state."""
    out = Path(argv[0])
    suffix = f"-{argv[1]}" if len(argv) > 1 else ""
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(["kodesk-styleguide"])
    with tempfile.TemporaryDirectory() as tmp:
        window = MainWindow(reader_context(find_install(make_pocketbook(Path(tmp) / "PB634K3"))))
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
        window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
