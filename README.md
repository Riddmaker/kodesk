# kodesk

A desktop GUI to update KOReader on an e-reader (first target: PocketBook) and to browse, install, update and remove its community plugins and user patches — without breaking the reader's carefully built setup.

**Status: early spike.** What runs today is the *Tuning-Garage* mockup: your reader drawn on screen (PocketBook Verse Pro Color), with KOReader's book view, menu, library and sleep screen. It reads the reader's settings and shows the changes you would make. It is **read-only**: nothing is written to the reader yet. Writing comes only after snapshot and restore exist.

## Run

kodesk uses Python 3.12+ and PySide6 (Qt 6).

```sh
nix develop                     # dev shell with Python, PySide6, linters, sway/grim/wtype
python -m kodesk                # first connected reader, or the demo reader
python -m kodesk /run/media/$USER/PB634K3   # a specific reader mount (or its koreader/ folder)
python -m kodesk --demo         # demo reader, ignore connected readers
```

Without Nix: `pip install -e .` in a virtualenv, then `kodesk`.

Every key drawn on the reader is a real button. Click it, or reach it with Tab and press it with Space. Hold it for 2 s for a long press. The *Side* switch shows the reader's other faces.

## Develop

```sh
nix develop -c tools/check.sh                # ruff format/check, mypy, pylint, pytest (as in CI)
nix develop -c tools/gui-smoke.sh /tmp/shots # headless sway, keyboard-only run, screenshots
nix build && nix develop -c tools/gui-smoke.sh /tmp/shots result/bin/kodesk
tools/lock-requirements.sh                   # refresh the hashed pip locks after editing data/packaging/*.in
tools/fixture-listings.py                    # refresh the release file lists the fake reader trees are built from
```

The tests and the smoke run use fake reader trees (`tests/conftest.py`). They never touch a connected reader.

## Packaging

| Channel | Source | Built by |
|---|---|---|
| Nix | `flake.nix` | `nix build` |
| Flatpak | `data/flatpak/io.github.riddmaker.kodesk.yml` (KDE 6.11 + PySide6 BaseApp) | `.github/workflows/package.yml` |
| Windows | PyInstaller (`data/packaging/kodesk.spec`) + Inno Setup (`data/windows/kodesk.iss`), per-user install | `package.yml` |
| macOS | PyInstaller `.app`, signed and notarized when the signing secrets are set | `package.yml` |

The app follows your desktop's Qt style; it has no theme of its own.

License: GPL-3.0-or-later (see `LICENSE`).
