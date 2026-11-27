# PyInstaller spec for the Windows and macOS bundles (PyInstaller 6.22, see requirements-bundle.txt).
#   pyinstaller --noconfirm data/packaging/kodesk.spec
# Linux ships as Nix package and Flatpak instead. Derived from the spec PyInstaller generates for
# `--windowed --add-data`, plus the macOS BUNDLE step.
# pylint: skip-file
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 - SPECPATH is set by PyInstaller
sys.path.insert(0, str(ROOT / "src"))
from kodesk import APP_ID, __version__  # noqa: E402

a = Analysis(  # noqa: F821
    [str(ROOT / "src/kodesk/__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "src/kodesk/ui/resources"), "kodesk/ui/resources"),
        (str(ROOT / "src/kodesk/core/resources"), "kodesk/core/resources"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="kodesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="kodesk",
)
if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="kodesk.app",
        icon=None,
        bundle_identifier=APP_ID,
        version=__version__,
    )
