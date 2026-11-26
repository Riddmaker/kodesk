"""What is installed on top of KOReader: plugins and user patches. Read-only.

Rules taken from KOReader v2026.07.2 (``frontend/pluginloader.lua``, ``frontend/userpatch.lua``):

* A plugin is a ``plugins/<name>.koplugin`` folder. It is off when ``plugins_disabled[<name>]``
  is set in ``settings.reader.lua``, or when ``plugins_disable_external`` is on and it is not
  built in.
* What is built in comes from ``ota/package.index``, the file list of the installed release
  (paths like ``../applications/koreader/plugins/x.koplugin/main.lua``). Without that file the
  origin of a plugin is unknown; kodesk does not guess it.
* A user patch is a file in ``patches/``. KOReader runs ``<priority><digits>-<name>.lua`` for the
  priorities 0, 1, 2, 8 and 9 (3 to 7 are reserved and never run), and none at all while
  ``patches/.patches_disabled`` exists.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from pathlib import Path

from kodesk.core.luadump import SettingsDocument

MAX_INDEX_BYTES = 4 * 1024 * 1024  # the real index is ~60 kB
RUN_PRIORITIES = frozenset("01289")
_PATCH_NAME = re.compile(r"^(?P<priority>[0-9])[0-9]*-.*\.lua$")
_INDEX_PLUGIN = re.compile(r"(?:^|/)koreader/plugins/(?P<name>[^/]+)\.koplugin(?:/|$)")


class Origin(enum.Enum):
    BUILT_IN = "built-in"
    THIRD_PARTY = "third-party"
    UNKNOWN = "unknown"  # no ota/package.index to compare against


@dataclass(frozen=True)
class Plugin:
    name: str
    origin: Origin
    enabled: bool


@dataclass(frozen=True)
class Patch:
    name: str
    priority: str | None  # first digit of the file name, None if the name does not qualify
    runs: bool  # KOReader would run it on the next start (priority 0: only after an update)


def built_in_plugins(koreader_dir: Path) -> frozenset[str] | None:
    """Plugin names shipped with the installed release, or None without a readable index."""
    index = koreader_dir / "ota" / "package.index"
    try:
        if not index.is_file() or index.stat().st_size > MAX_INDEX_BYTES:
            return None
        lines = index.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return frozenset(
        match["name"] for line in lines if (match := _INDEX_PLUGIN.search(line.strip()))
    )


def list_plugins(koreader_dir: Path, settings: SettingsDocument) -> tuple[Plugin, ...]:
    """Installed plugins, sorted by name, with origin and on/off state."""
    plugins_dir = koreader_dir / "plugins"
    if not plugins_dir.is_dir():
        return ()
    built_in = built_in_plugins(koreader_dir)
    external_off = settings.get(("plugins_disable_external",)) is True
    result = []
    for path in sorted(plugins_dir.iterdir(), key=lambda p: p.name.lower()):
        if not (path.name.endswith(".koplugin") and path.is_dir()):
            continue
        name = path.name.removesuffix(".koplugin")
        if built_in is None:
            origin = Origin.UNKNOWN
        else:
            origin = Origin.BUILT_IN if name in built_in else Origin.THIRD_PARTY
        disabled = bool(settings.get(("plugins_disabled", name))) or (
            external_off and origin is not Origin.BUILT_IN
        )
        result.append(Plugin(name=name, origin=origin, enabled=not disabled))
    return tuple(result)


def patch_priority(name: str) -> str | None:
    """The priority digit of a patch file name, None if KOReader would never run it."""
    match = _PATCH_NAME.match(name)
    return match["priority"] if match else None


def list_patches(koreader_dir: Path) -> tuple[Patch, ...]:
    """Files in ``patches/``, sorted by name, with whether KOReader would run them."""
    patches_dir = koreader_dir / "patches"
    if not patches_dir.is_dir():
        return ()
    all_off = (patches_dir / ".patches_disabled").is_file()
    result = []
    for path in sorted(patches_dir.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name == ".patches_disabled":
            continue
        priority = patch_priority(path.name)
        runs = not all_off and priority in RUN_PRIORITIES
        result.append(Patch(name=path.name, priority=priority, runs=runs))
    return tuple(result)
