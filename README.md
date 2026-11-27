# kodesk

A desktop GUI to update KOReader on an e-reader (first target: PocketBook) and to browse, install, update and remove its community plugins and user patches — without breaking the reader's carefully built setup.

**Status: early, not yet tested on many readers.** What runs today: the *Tuning-Garage* (your reader drawn on screen — PocketBook Verse Pro Color — with KOReader's book view, menu, library and sleep screen; change settings and write exactly those keys), the *Store* (browse community plugins and user patches from the [KOReader plugin index](https://github.com/omer-faruq/koreader-plugin-index)), the *Installed* tab (install, update and remove plugins and patches, check for updates, and update KOReader itself to the latest stable release on PocketBook, keeping files you changed if you want), and *snapshots*: before every write kodesk copies the reader's KOReader folder to your PC, checks afterwards that nothing else changed, and brings everything back with one click. *Wi-Fi* (Reader → Wi-Fi…): pair a reader once over USB, then connect over KOReader's SSH server: kodesk keeps a copy of the reader's KOReader folder on your PC, shows it, and installs, updates and removes plugins and patches over Wi-Fi (snapshot first, as over USB); a KOReader update is sent over Wi-Fi too and installs itself when you restart KOReader. Garage changes are sent over Wi-Fi as well and apply when you restart KOReader: for that kodesk puts a small patch of its own on the reader (`patches/1-kodesk-apply.lua`, after asking), which applies only the settings you changed, and only those not changed on the reader in the meantime.

## Run

kodesk uses Python 3.12+ and PySide6 (Qt 6).

```sh
nix develop                     # dev shell with Python, PySide6, linters, sway/grim/wtype
python -m kodesk                # first connected reader, or the demo reader
python -m kodesk /run/media/$USER/PB634K3   # a specific reader mount (or its koreader/ folder)
python -m kodesk --demo         # demo reader, ignore connected readers
python -m kodesk --offline      # never go online (the Store shows its last cached catalogue)
```

Without Nix: `pip install -e .` in a virtualenv, then `kodesk`.

Every key drawn on the reader is a real button. Click it, or reach it with Tab and press it with Space. Hold it for 2 s for a long press. The *Side* switch shows the reader's other faces.

## Develop

```sh
tools/check.sh                               # ruff format/check, mypy, pylint, pytest (as in CI; enters the dev shell)
tools/check.sh --fix                         # the same, after letting ruff format and fix what it can
nix develop -c tools/gui-smoke.sh /tmp/shots # headless sway, keyboard-only run, screenshots
nix build && nix develop -c tools/gui-smoke.sh /tmp/shots result/bin/kodesk
tools/lock-requirements.sh                   # refresh the hashed pip locks after editing data/packaging/*.in
tools/flatpak-deps.py                        # refresh the Flatpak's hash-pinned wheels after editing data/flatpak/requirements.in
tools/fixture-listings.py                    # refresh the release file lists the fake reader trees are built from
tools/styleguide-shots.sh                    # regenerate the styleguide screenshots (Qt style + desktop theme)
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
