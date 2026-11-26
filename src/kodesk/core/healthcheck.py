"""After a write: did KOReader still load every plugin and patch? (B-10)

``_meta.lua`` declares no compatibility, so kodesk checks the result instead. Right after a
write it remembers where ``crash.log`` ended (its last few kB, the *anchor*) together with the
snapshot taken before the write, in ``pending-check.json`` next to the snapshots on the PC.

On the next connect only the log written after the anchor is read. The PocketBook launcher
trims ``crash.log`` to its last 500 kB when KOReader starts, so a byte offset would drift; the
anchor is searched instead, and when it was trimmed away everything left is newer than it.
No new log means KOReader has not started since the write and the check stays pending.

The log lines come from ``frontend/pluginloader.lua`` and ``frontend/userpatch.lua``::

    05/19/26-13:41:50 WARN  Error when loading plugins/x.koplugin/main.lua: <error>
    05/19/26-13:41:50 ERROR Failed to initialize x plugin: <error>
    08/28/26-21:06:52 INFO  Applying patch: ./patches/2-x.lua
    08/28/26-21:06:52 WARN  Patching failed: ./patches/2-x.lua:5: <error>
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from pathlib import Path

CRASH_LOG = "crash.log"
PENDING_FILE = "pending-check.json"
ANCHOR_SIZE = 4096
MAX_LOG_READ = 2 * 2**20  # the launcher keeps 500 kB; never read more than this
MAX_MESSAGE = 200
TEMP_SUFFIX = ".kodesk-tmp"

_LOAD = re.compile(
    r"Error when loading (?:.*?/)?(?P<name>[^/]+?)\.koplugin/main\.lua:?(?P<msg>.*)$"
)
_INIT = re.compile(r"Failed to initialize (?P<name>\S+) plugin:(?P<msg>.*)$")
_APPLY = re.compile(r"Applying patch: (?P<path>.+?)\s*$")
_PATCH_FAILED = re.compile(r"Patching failed:(?P<msg>.*)$")
_PATCH_IN_ERROR = re.compile(r"(?:^|/)patches/(?P<name>[^/:]+\.lua):")


@dataclass(frozen=True)
class Pending:
    """A check to run on the next connect: the snapshot before the write, the log anchor."""

    snapshot_id: str
    anchor: bytes


@dataclass(frozen=True)
class Failure:
    """A plugin or patch KOReader could not load, with the first line of its error."""

    kind: str  # "plugin" or "patch"
    name: str
    message: str


@dataclass(frozen=True)
class Report:
    """Result of a check: ``started`` is False while KOReader has not run since the write."""

    started: bool
    failures: tuple[Failure, ...] = ()


def _read_log(koreader_dir: Path) -> bytes | None:
    log = koreader_dir / CRASH_LOG
    if not log.is_file():
        return None
    with log.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - MAX_LOG_READ))
        return handle.read()


def remember(base: Path, koreader_dir: Path, snapshot_id: str) -> Pending:
    """Mark where ``crash.log`` ends now; the next connect checks what KOReader logs after it."""
    log = _read_log(koreader_dir) or b""
    pending = Pending(snapshot_id, log[-ANCHOR_SIZE:])
    data = {"snapshot": snapshot_id, "anchor": base64.b64encode(pending.anchor).decode("ascii")}
    base.mkdir(parents=True, exist_ok=True)
    target = base / PENDING_FILE
    temp = target.with_name(target.name + TEMP_SUFFIX)
    temp.write_text(json.dumps(data), encoding="utf-8")
    temp.replace(target)
    return pending


def load_pending(base: Path) -> Pending | None:
    """The pending check, or None; a damaged file counts as none (it only ever offers a help)."""
    try:
        data = json.loads((base / PENDING_FILE).read_text(encoding="utf-8"))
        snapshot_id, anchor = data["snapshot"], data["anchor"]
        if not isinstance(snapshot_id, str) or not isinstance(anchor, str):
            return None
        return Pending(snapshot_id, base64.b64decode(anchor, validate=True))
    except (OSError, ValueError, KeyError, TypeError, binascii.Error):
        return None


def clear_pending(base: Path) -> None:
    (base / PENDING_FILE).unlink(missing_ok=True)


def new_log(log: bytes, anchor: bytes) -> bytes:
    """What was logged after ``anchor``; all of ``log`` if the anchor was trimmed away."""
    if not anchor:
        return log
    at = log.rfind(anchor)
    return log if at < 0 else log[at + len(anchor) :]


def _message(text: str) -> str:
    text = text.strip()
    if text in ("", "nil"):
        return "main.lua did not return a plugin"
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def parse_failures(text: str) -> tuple[Failure, ...]:
    """Plugins and patches the log says failed to load, each once, in order of appearance."""
    found: dict[tuple[str, str], Failure] = {}
    patch: str | None = None
    for line in text.splitlines():
        if match := _APPLY.search(line):
            patch = match["path"].rsplit("/", 1)[-1]
            continue
        failure = None
        if match := _LOAD.search(line) or _INIT.search(line):
            failure = Failure("plugin", match["name"], _message(match["msg"]))
        elif match := _PATCH_FAILED.search(line):
            in_error = _PATCH_IN_ERROR.search(match["msg"])
            name = in_error["name"] if in_error else patch or "unknown patch"
            failure = Failure("patch", name, _message(match["msg"]))
        if failure is not None:
            found.setdefault((failure.kind, failure.name), failure)
    return tuple(found.values())


def check(koreader_dir: Path, pending: Pending) -> Report:
    """Read what KOReader logged since the write (read-only)."""
    log = _read_log(koreader_dir)
    if log is None:
        return Report(started=False)
    new = new_log(log, pending.anchor)
    if not new.strip():
        return Report(started=False)
    return Report(True, parse_failures(new.decode("utf-8", errors="replace")))
