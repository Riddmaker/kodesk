"""Find and describe a KOReader installation on a mounted reader. Read-only.

Install locations per platform: research/koreader-releases §7 (PocketBook ``applications/koreader``,
Kobo ``.adds/koreader``, Kindle ``koreader``, Android ``koreader`` on external storage).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kodesk.core.devices import DeviceModel, find_by_koreader_model
from kodesk.core.luadump import LuaParseError, SettingsDocument

KNOWN_LAYOUTS: tuple[tuple[str, str], ...] = (
    ("pocketbook", "applications/koreader"),
    ("kobo", ".adds/koreader"),
    ("kindle", "koreader"),  # Android uses the same relative path on external storage
)
MAX_SETTINGS_BYTES = 8 * 1024 * 1024  # a settings file larger than this is not a real one


class InstallError(Exception):
    """No usable KOReader installation at the given place."""


@dataclass(frozen=True)
class KOReaderInstall:
    """A KOReader installation found on a mounted reader (read-only view of it)."""

    root: Path  # the KOReader directory itself
    platform: str
    version: str | None  # first line of git-rev, e.g. "v2026.07.2"
    model: str | None  # last field of the last version.log line, e.g. "PBVerseProColor"
    device: DeviceModel | None

    @property
    def settings_path(self) -> Path:
        return self.root / "settings.reader.lua"

    @property
    def fonts_dir(self) -> Path:
        return self.root / "fonts"

    def read_settings(self) -> SettingsDocument:
        return read_settings_file(self.settings_path)


def read_settings_file(path: Path) -> SettingsDocument:
    """Parse a settings file; a missing file is an empty table, an oversized one an error."""
    if not path.is_file():
        return SettingsDocument.parse("return {}\n")
    if path.stat().st_size > MAX_SETTINGS_BYTES:
        raise InstallError(f"{path.name} is unexpectedly large")
    try:
        return SettingsDocument.parse(path.read_bytes())
    except LuaParseError as error:
        raise InstallError(f"{path.name} is not a KOReader settings file ({error})") from error


def _first_line(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            line = handle.readline().strip()
    except OSError:
        return None
    return line or None


def _model_from_version_log(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.strip():
            return line.rsplit(",", 1)[-1].strip() or None
    return None


def describe(koreader_dir: Path, platform: str) -> KOReaderInstall:
    if not (koreader_dir / "reader.lua").is_file():
        raise InstallError(f"{koreader_dir} does not contain reader.lua")
    model = _model_from_version_log(koreader_dir / "version.log")
    return KOReaderInstall(
        root=koreader_dir,
        platform=platform,
        version=_first_line(koreader_dir / "git-rev"),
        model=model,
        device=find_by_koreader_model(model) if model else None,
    )


def find_install(path: Path) -> KOReaderInstall:
    """Accept either the reader's mount point or the KOReader directory itself."""
    path = path.resolve()
    if (path / "reader.lua").is_file():
        platform = next(
            (name for name, rel in KNOWN_LAYOUTS if path.as_posix().endswith(rel)), "unknown"
        )
        return describe(path, platform)
    for platform, relative in KNOWN_LAYOUTS:
        candidate = path / relative
        if (candidate / "reader.lua").is_file():
            return describe(candidate, platform)
    raise InstallError(f"no KOReader installation found under {path}")
