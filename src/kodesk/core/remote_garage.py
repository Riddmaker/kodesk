"""Tuning-Garage changes over Wi-Fi (B-26): a change file that a small patch applies on the reader.

Over Wi-Fi KOReader is running and saves its settings itself, so kodesk cannot write
``settings.reader.lua`` there (B-27). Instead it writes ``kodesk/pending.json``: each change is
a key path with the value kodesk saw (``old``) and the one the user chose (``new``). The patch
``patches/1-kodesk-apply.lua`` (resources/kodesk-apply.lua) runs at the next KOReader start,
before KOReader opens its settings, and applies a change only while the key still has its old
value. It writes what it did to ``kodesk/result.json``, which kodesk reads on the next connect.

The patch is code that runs on the reader, so kodesk sends it only after the user agreed, and
it accepts nothing but the top-level keys the Garage writes (:data:`ALLOWED`, baked into it).
JSON has no Lua tables with mixed keys, so a table value travels as
``{"table": [[key, value], ...]}``; a key that is absent has no ``old`` or ``new``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from kodesk.core.extensions import EXTENSIONS
from kodesk.core.hotcorners import GESTURES_FILE, SECTION
from kodesk.core.librarymode import COVER_BROWSER, FILEMANAGER_KEY, PROJECT_TITLE, UNIFIED_KEYS
from kodesk.core.luadump import (
    ENCODING,
    KeyPath,
    LuaKey,
    LuaTable,
    LuaValue,
    changed_paths,
    lua_quote,
    ordered_keys,
)
from kodesk.core.statusbar import FOOTER_KEY, FOOTER_MODE_KEY, HEADER_ITEMS, header_key

PATCH_NAME = "1-kodesk-apply.lua"  # priority 1: before KOReader opens settings.reader.lua
PATCH = f"patches/{PATCH_NAME}"
PATCHES_DISABLED = "patches/.patches_disabled"  # KOReader then runs no patch at all
FOLDER = "kodesk"
PENDING = f"{FOLDER}/pending.json"
RESULT = f"{FOLDER}/result.json"
REJECTED = f"{FOLDER}/pending.rejected.json"
FORMAT = 1
# The same limits as in the patch.
MAX_BYTES = 65536
MAX_CHANGES = 200
MAX_DEPTH = 8
MAX_STRING = 4096
MAX_KEY = 200
MAX_RESULT_BYTES = 4 * MAX_BYTES

READER = "reader"
GESTURES = "gestures"
LIBRARY = "library"
SETTINGS_FILE = "settings.reader.lua"
FILES = {READER: SETTINGS_FILE, GESTURES: GESTURES_FILE}
TARGET_OF = {name: target for target, name in FILES.items()}

# The top-level keys of settings.reader.lua the Garage writes (kodesk.ui.garage); the patch
# refuses every other one. A new Garage key needs a new patch, which kodesk asks before sending.
READER_KEYS = frozenset(
    {
        "cre_font",
        "copt_font_size",
        "copt_line_spacing",
        "copt_h_page_margins",
        "copt_t_page_margin",
        "copt_b_page_margin",
        "night_mode",
        "copt_nightmode_images",
        "copt_status_line",
        "cre_header_status_font_size",
        "input_invert_page_turn_keys",
        FOOTER_KEY,
        FOOTER_MODE_KEY,
        *(header_key(item) for item, _label, _default in HEADER_ITEMS),
        *(str(path[0]) for extension in EXTENSIONS for path in extension.keys),  # B-23
    }
)
ALLOWED = {READER: READER_KEYS, GESTURES: frozenset({SECTION})}
LIBRARY_DBS = (COVER_BROWSER.cache, PROJECT_TITLE.cache)
LIBRARY_KEYS = (FILEMANAGER_KEY, *UNIFIED_KEYS)

type PatchState = Literal["missing", "current", "other"]
_LUA_WHERE = re.compile(r"^(?:[^\n]*?\.lua:\d+: )+")  # Lua's "<file>:<line>: " before a message


class RemoteGarageError(Exception):
    """A change kodesk cannot send over Wi-Fi, or a change or result file it does not trust."""


# --------------------------------------------------------------------------- values


def _check_text(text: str) -> str:
    try:
        size = len(text.encode(ENCODING))
    except UnicodeEncodeError as error:  # bytes that are not UTF-8 (surrogateescape)
        raise RemoteGarageError("a setting holds text kodesk cannot send over Wi-Fi") from error
    if size > MAX_STRING:
        raise RemoteGarageError("a setting holds text too long to send over Wi-Fi")
    return text


def _encode_key(key: LuaKey) -> str | int:
    if isinstance(key, str) and 0 < len(key.encode(ENCODING, "surrogateescape")) <= MAX_KEY:
        return _check_text(key)
    if isinstance(key, int) and not isinstance(key, bool) and abs(key) <= 10**9:
        return key
    raise RemoteGarageError(f"a setting has a key kodesk cannot send over Wi-Fi: {key!r}")


def encode_value(value: LuaValue, depth: int = 0) -> Any:
    """A Lua value as JSON for the patch: scalars as they are, tables as key/value pairs."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RemoteGarageError("a setting holds a number kodesk cannot send over Wi-Fi")
        return value
    if isinstance(value, str):
        return _check_text(value)
    if depth >= MAX_DEPTH:
        raise RemoteGarageError("a setting is nested too deeply to send over Wi-Fi")
    return {
        "table": [[_encode_key(k), encode_value(value[k], depth + 1)] for k in ordered_keys(value)]
    }


def _is_key(key: object) -> bool:
    if isinstance(key, str):
        return 0 < len(key.encode(ENCODING, "surrogatepass")) <= MAX_KEY
    return isinstance(key, int) and not isinstance(key, bool) and abs(key) <= 10**9


def decode_value(raw: object, depth: int = 0) -> LuaValue:
    """The reverse of :func:`encode_value`, for files read back from the reader (untrusted)."""
    if isinstance(raw, (bool, str)):
        return raw
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and math.isfinite(raw):
        return int(raw) if raw.is_integer() and abs(raw) < 2**53 else raw
    if isinstance(raw, dict) and depth < MAX_DEPTH and set(raw) == {"table"}:
        pairs = _as_list(raw["table"])
        table: LuaTable = {}
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2 or not _is_key(pair[0]):
                raise RemoteGarageError("not a table kodesk wrote")
            key = int(pair[0]) if isinstance(pair[0], float) else pair[0]
            table[key] = decode_value(pair[1], depth + 1)
        return table
    raise RemoteGarageError("a value kodesk did not write")


def _as_list(raw: object) -> list[Any]:
    """luajson writes an empty Lua table as ``{}``, an array or not."""
    if raw == {}:
        return []
    if not isinstance(raw, list):
        raise RemoteGarageError("a list kodesk did not write")
    return raw


def _same(left: LuaValue | None, right: LuaValue | None) -> bool:
    """Equal as Lua sees it (``True`` is not ``1``; table key order does not matter)."""
    return json.dumps(_canonical(left)) == json.dumps(_canonical(right))


def _canonical(value: LuaValue | None) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        return ["n", float(value)]
    return [[_canonical(k), _canonical(value[k])] for k in ordered_keys(value)]


# --------------------------------------------------------------------------- changes


@dataclass(frozen=True)
class Change:
    """One key: ``target`` (reader, gestures, library), its path, the value seen, the new one.

    For the library, ``path`` is ``(cache file, config row)``. None means: no such key.
    """

    target: str
    path: KeyPath
    old: LuaValue | None
    new: LuaValue | None

    def to_json(self) -> dict[str, Any]:
        """As the patch reads it; raises :class:`RemoteGarageError` for what cannot travel."""
        raw: dict[str, Any] = {"target": self.target}
        if self.target == LIBRARY:
            raw["db"], raw["key"] = self.path
        else:
            raw["path"] = [_encode_key(k) for k in self.path]
        for side in ("old", "new"):
            value = getattr(self, side)
            if value is not None:
                raw[side] = encode_value(value, len(self.path))
        return raw

    @classmethod
    def from_json(cls, raw: object) -> Change:
        """A change read back from the reader; refuses anything kodesk would not have sent."""
        sides = {"old", "new"} & set(raw) if isinstance(raw, dict) else set()
        if not isinstance(raw, dict) or not sides or any(raw[s] is None for s in sides):
            raise RemoteGarageError("not a change kodesk wrote")  # absent, never null
        old, new = raw.get("old"), raw.get("new")
        target = raw.get("target")
        if target == LIBRARY:
            db, key = raw.get("db"), raw.get("key")
            if set(raw) - {"target", "db", "key", "old", "new"} or db not in LIBRARY_DBS:
                raise RemoteGarageError("not a library change kodesk wrote")
            if key not in LIBRARY_KEYS or not all(
                v is None or isinstance(v, str) for v in (old, new)
            ):
                raise RemoteGarageError("not a library change kodesk wrote")
            return cls(LIBRARY, (str(db), str(key)), old, new)
        path = raw.get("path")
        if target not in ALLOWED or set(raw) - {"target", "path", "old", "new"}:
            raise RemoteGarageError("not a settings change kodesk wrote")
        if (
            not isinstance(path, list)
            or not 0 < len(path) <= MAX_DEPTH
            or not all(_is_key(k) for k in path)
            or path[0] not in ALLOWED[str(target)]
        ):
            raise RemoteGarageError("not a settings change kodesk wrote")
        keys = tuple(path)
        decoded = [None if v is None else decode_value(v, len(keys)) for v in (old, new)]
        return cls(str(target), keys, decoded[0], decoded[1])

    def inverse(self) -> Change:
        return Change(self.target, self.path, self.new, self.old)

    def related(self, other: Change) -> bool:
        """Same target, and one path is the other or lies inside it."""
        if self.target != other.target:
            return False
        shorter = min(len(self.path), len(other.path))
        return self.path[:shorter] == other.path[:shorter]

    def label(self) -> str:
        """For previews and reports: ``settings.reader.lua: copt_font_size``."""
        if self.target == LIBRARY:
            return f"{self.path[0]}: {self.path[1]}"
        return f"{FILES[self.target]}: {'.'.join(str(k) for k in self.path)}"


def settings_changes(name: str, before: LuaTable, after: LuaTable) -> list[Change]:
    """The changes from ``before`` to ``after`` of a settings file the Garage writes."""
    target = TARGET_OF.get(name)
    if target is None:
        raise RemoteGarageError(f"kodesk does not change {name} over Wi-Fi")
    changes = []
    for path in changed_paths(before, after):
        if path[0] not in ALLOWED[target]:
            raise RemoteGarageError(f"kodesk does not change {path[0]} over Wi-Fi")
        change = Change(target, path, _at(before, path), _at(after, path))
        change.to_json()  # refuses what cannot travel, before anything is written
        changes.append(change)
    return changes


def _at(data: LuaTable, path: KeyPath) -> LuaValue | None:
    value: LuaValue | None = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def library_changes(
    db: str, before: Mapping[str, str | None], rows: Mapping[str, str | None]
) -> list[Change]:
    """The library rows to write (:meth:`LibraryLook.rows`), against the cache as read."""
    if db not in LIBRARY_DBS or set(rows) - set(LIBRARY_KEYS):
        raise RemoteGarageError("kodesk does not change these library settings over Wi-Fi")
    return [Change(LIBRARY, (db, key), before.get(key), value) for key, value in rows.items()]


def merge(pending: Sequence[Change], added: Iterable[Change]) -> list[Change]:
    """Changes still waiting on the reader, then new ones.

    A new change of a key that is the last one waiting for that key becomes one change: the old
    value the reader still has, the newest value. One that brings a key back to where it was
    drops out. The patch applies the list in order, so related keys keep their order.
    """
    merged = list(pending)
    for change in added:
        last = max((i for i, c in enumerate(merged) if c.related(change)), default=None)
        if last is not None and merged[last].path == change.path:
            combined = Change(change.target, change.path, merged[last].old, change.new)
            if _same(combined.old, combined.new):
                del merged[last]
            else:
                merged[last] = combined
        elif not _same(change.old, change.new):
            merged.append(change)
    return merged


def new_id(now: datetime | None = None) -> str:
    """Sorts by time (the patch keeps the backups of the last three)."""
    when = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{when}-{secrets.token_hex(3)}"


def pending_document(change_id: str, changes: Sequence[Change]) -> bytes:
    """The bytes of ``kodesk/pending.json``."""
    if not changes:
        raise RemoteGarageError("nothing to send")
    if len(changes) > MAX_CHANGES:
        raise RemoteGarageError("too many changes at once; write some, restart KOReader, go on")
    document = {"format": FORMAT, "id": change_id, "changes": [c.to_json() for c in changes]}
    data = json.dumps(document, ensure_ascii=False, allow_nan=False).encode(ENCODING)
    if len(data) > MAX_BYTES:
        raise RemoteGarageError("the changes are too large to send at once")
    return data


def _load(data: bytes, limit: int) -> dict[str, Any]:
    if len(data) > limit:
        raise RemoteGarageError("file too large")
    try:
        document = json.loads(data.decode(ENCODING))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise RemoteGarageError(f"not a kodesk file: {error}") from error
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise RemoteGarageError("not a kodesk file")
    return document


def read_pending(data: bytes) -> list[Change]:
    """The changes in a ``pending.json`` still waiting on the reader."""
    document = _load(data, MAX_BYTES)
    return [Change.from_json(raw) for raw in _as_list(document.get("changes"))]


@dataclass(frozen=True)
class Result:
    """What the patch did with one change file (``kodesk/result.json``)."""

    id: str
    applied: tuple[Change, ...] = ()
    already: tuple[Change, ...] = ()
    skipped: tuple[tuple[Change, str], ...] = ()
    error: str | None = None
    restored: bool = False
    unreadable: tuple[str, ...] = field(default=())


def read_result(data: bytes) -> Result:
    """``result.json`` as the patch wrote it (untrusted: a file from the reader)."""
    document = _load(data, MAX_RESULT_BYTES)
    change_id, error = document.get("id"), document.get("error")
    if not isinstance(change_id, str) or not change_id or len(change_id) > 64:
        change_id = "?"  # the patch could not read the change file at all
    skipped = []
    for raw in _as_list(document.get("skipped", [])):
        reason = raw.get("reason") if isinstance(raw, dict) else None
        if not isinstance(reason, str):
            raise RemoteGarageError("not a result kodesk's patch wrote")
        skipped.append((Change.from_json(raw.get("change")), reason[:200]))
    return Result(
        change_id,
        tuple(Change.from_json(raw) for raw in _as_list(document.get("applied", []))),
        tuple(Change.from_json(raw) for raw in _as_list(document.get("already", []))),
        tuple(skipped),
        _LUA_WHERE.sub("", error)[:500] if isinstance(error, str) else None,
        document.get("restored") is True,
    )


def not_as_sent(changes: Iterable[Change], files: Mapping[str, LuaTable]) -> list[Change]:
    """Applied changes whose key does not hold the new value in the files read now."""
    return [
        c
        for c in changes
        if c.target in FILES
        and FILES[c.target] in files
        and not _same(_at(files[FILES[c.target]], c.path), c.new)
    ]


# --------------------------------------------------------------------------- the patch


def patch_source(version: str) -> bytes:
    """The patch with this kodesk's version and allow-list filled in."""
    template = resources.files("kodesk.core").joinpath("resources/kodesk-apply.lua")
    text = template.read_text(encoding=ENCODING)
    lines = []
    for target in (READER, GESTURES):
        keys = ", ".join(f"[{lua_quote(k)}] = true" for k in sorted(ALLOWED[target]))
        lines.append(f"    {target} = {{ {keys} }},")
    if text.count("@VERSION@") != 1 or text.count("-- @ALLOWED@") != 1:
        raise RemoteGarageError("the patch template is damaged")
    if not version.replace(".", "").replace("dev", "").replace("+", "").isalnum():
        raise RemoteGarageError(f"unexpected version {version!r}")
    text = text.replace("@VERSION@", version).replace("-- @ALLOWED@", "\n".join(lines))
    return text.encode(ENCODING)


def patch_state(koreader_dir: Path, version: str) -> PatchState:
    """Whether the reader has this kodesk's patch, none, or another one (older or changed)."""
    path = koreader_dir / PATCH
    if not path.is_file():
        return "missing"
    here = hashlib.sha256(path.read_bytes()).digest()
    return "current" if here == hashlib.sha256(patch_source(version)).digest() else "other"
