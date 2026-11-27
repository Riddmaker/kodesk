"""Read and surgically edit KOReader settings files without executing Lua.

KOReader writes its settings (``settings.reader.lua``, ``settings/*.lua``, ``*.sdr/metadata.*.lua``)
with ``LuaSettings:flush()``: ``"-- <path>\\nreturn " .. dump(data, nil, true) .. "\\n"``, where
``dump`` (frontend/dump.lua) emits one ``[key] = value,`` per line, keys sorted by
``ffi/util.orderedPairs``, strings via ``string.format("%q")`` and numbers via ``tostring``
(LuaJIT: ``%.14g``).

This module parses that subset of Lua into Python values *and* remembers where every entry sits in
the source. Edits then replace only the bytes of the changed entry, so every other key stays
byte-identical (hard rule "the reader's setup is sacred"). :func:`dump` reproduces KOReader's
serializer for new values and for the canonical-format check.

Nothing here evaluates code: the parser accepts literals and table constructors only and rejects
anything else (function calls, operators, variables) with :class:`LuaParseError`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import ClassVar

type LuaKey = str | int | float | bool
type LuaValue = str | int | float | bool | LuaTable
type LuaTable = dict[LuaKey, LuaValue]
type KeyPath = tuple[LuaKey, ...]

INDENT = "    "
MAX_DEPTH = 64  # guards against hostile or broken files nesting tables endlessly
ENCODING = "utf-8"
ERRORS = "surrogateescape"  # Lua strings are bytes; keep invalid UTF-8 round-trippable


class LuaParseError(ValueError):
    """The file is not a plain KOReader settings dump; kodesk must not edit it."""


# --------------------------------------------------------------------------- serializer


def lua_number(value: int | float) -> str:
    """Format like LuaJIT's ``tostring(number)`` (``%.14g``)."""
    number = float(value)
    if math.isnan(number):
        return "nan" if math.copysign(1.0, number) > 0 else "-nan"
    if math.isinf(number):
        return "inf" if number > 0 else "-inf"
    return format(number, ".14g")


def lua_quote(text: str) -> str:
    """Format like LuaJIT 2.1's ``string.format("%q", s)`` (lj_strfmt_putquoted)."""
    raw = text.encode(ENCODING, ERRORS)
    out = bytearray(b'"')
    for index, byte in enumerate(raw):
        if byte in (0x22, 0x5C, 0x0A):  # " \ newline
            out += b"\\" + bytes([byte])
        elif byte < 0x20 or byte == 0x7F:
            following = raw[index + 1 : index + 2]
            if following.isdigit() or byte >= 100:
                out += b"\\%03d" % byte
            else:
                out += b"\\%d" % byte
        else:
            out.append(byte)
    out += b'"'
    return out.decode(ENCODING, ERRORS)


def _sort_key_cmp(left: LuaKey, right: LuaKey) -> bool:
    """``left < right`` as in ffi/util.orderedPairs: same type compares, else tostring()."""
    if _lua_type(left) == _lua_type(right):
        if isinstance(left, str) and isinstance(right, str):
            return left.encode(ENCODING, ERRORS) < right.encode(ENCODING, ERRORS)
        return bool(left < right)  # type: ignore[operator]
    return _tostring(left).encode(ENCODING, ERRORS) < _tostring(right).encode(ENCODING, ERRORS)


def _lua_type(value: LuaKey | LuaValue) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "table"


def _tostring(value: LuaKey) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return lua_number(value)
    return value


class _Sortable:
    """functools.cmp_to_key equivalent for the orderedPairs comparator."""

    __slots__ = ("key",)

    def __init__(self, key: LuaKey) -> None:
        self.key = key

    def __lt__(self, other: _Sortable) -> bool:
        return _sort_key_cmp(self.key, other.key)


def ordered_keys(table: LuaTable) -> list[LuaKey]:
    """Keys in the order KOReader's dump writes them."""
    return [item.key for item in sorted((_Sortable(k) for k in table), key=lambda s: s)]


def _dump_scalar(value: LuaKey | LuaValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return lua_number(value)
    if isinstance(value, str):
        return lua_quote(value)
    raise TypeError(f"not a Lua scalar: {type(value).__name__}")


def dump(value: LuaValue, indent: int = 0) -> str:
    """Serialize like KOReader's ``dump(value, nil, true)`` at the given indent level."""
    if not isinstance(value, dict):
        return _dump_scalar(value)
    if indent > MAX_DEPTH:
        raise ValueError("table nesting too deep")
    parts = ["{"]
    for key in ordered_keys(value):
        parts.append("\n" + INDENT * (indent + 1))
        parts.append(f"[{_dump_scalar(key)}] = {dump(value[key], indent + 1)},")
    if len(parts) > 1:
        parts.append("\n" + INDENT * indent)
    parts.append("}")
    return "".join(parts)


def dump_file(data: LuaTable, header_path: str) -> str:
    """Whole file as ``LuaSettings:flush()`` writes it."""
    return f"-- {header_path}\nreturn {dump(data)}\n"


# --------------------------------------------------------------------------- parser


@dataclass
class Entry:
    """One ``[key] = value,`` line of a table, with its byte span in the source text."""

    key: LuaKey
    start: int  # offset of the "\n" before the entry (or of the key if none)
    value_start: int
    value_end: int
    end: int  # offset just after the trailing "," (or after the value)
    table: TableNode | None = None  # set when the value is a table


@dataclass
class TableNode:
    """A table constructor ``{ ... }`` and where its entries sit."""

    open_pos: int  # offset of "{"
    close_pos: int  # offset of "}"
    depth: int
    entries: list[Entry] = field(default_factory=list)


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.next_array_index = 1

    def error(self, message: str) -> LuaParseError:
        line = self.text.count("\n", 0, self.pos) + 1
        return LuaParseError(f"line {line}: {message}")

    def skip_space(self) -> None:
        """Skip whitespace and Lua comments (line and long-bracket)."""
        text = self.text
        while self.pos < len(text):
            char = text[self.pos]
            if char in " \t\r\n":
                self.pos += 1
            elif text.startswith("--", self.pos):
                self._skip_comment()
            else:
                return

    def _skip_comment(self) -> None:
        self.pos += 2
        level = self._long_bracket_level()
        if level is not None:
            self._read_long_bracket(level)
            return
        newline = self.text.find("\n", self.pos)
        self.pos = len(self.text) if newline < 0 else newline + 1

    def _long_bracket_level(self) -> int | None:
        if not self.text.startswith("[", self.pos):
            return None
        end = self.pos + 1
        while end < len(self.text) and self.text[end] == "=":
            end += 1
        if end < len(self.text) and self.text[end] == "[":
            return end - self.pos - 1
        return None

    def _read_long_bracket(self, level: int) -> str:
        start = self.pos + level + 2
        closing = "]" + "=" * level + "]"
        end = self.text.find(closing, start)
        if end < 0:
            raise self.error("unterminated long bracket")
        self.pos = end + len(closing)
        content = self.text[start:end]
        if content.startswith("\r\n"):
            return content[2:]
        return content[1:] if content.startswith("\n") else content

    def expect(self, token: str) -> None:
        self.skip_space()
        if not self.text.startswith(token, self.pos):
            raise self.error(f"expected {token!r}")
        self.pos += len(token)

    def parse_document(self) -> tuple[LuaTable, TableNode]:
        """``return { ... }`` and nothing else."""
        self.skip_space()
        if not self.text.startswith("return", self.pos):
            raise self.error("settings file must start with 'return'")
        self.pos += len("return")
        self.skip_space()
        if not self.text.startswith("{", self.pos):
            raise self.error("settings file must return a table")
        value, node = self.parse_table(0)
        self.skip_space()
        if self.pos != len(self.text):
            raise self.error("unexpected text after the table")
        return value, node

    def parse_value(self, depth: int) -> tuple[LuaValue, TableNode | None]:
        """One literal: table, string, boolean or number (never nil, a call or an operator)."""
        self.skip_space()
        if self.pos >= len(self.text):
            raise self.error("unexpected end of file")
        char = self.text[self.pos]
        if char == "{":
            return self.parse_table(depth + 1)
        if char in "\"'":
            return self.parse_string(), None
        if char == "[" and self._long_bracket_level() is not None:
            return self._read_long_bracket(self._long_bracket_level() or 0), None
        return self.parse_scalar_word(), None

    def parse_table(self, depth: int) -> tuple[LuaTable, TableNode]:
        """A table constructor; records each entry's source span for surgical edits."""
        if depth > MAX_DEPTH:
            raise self.error("table nesting too deep")
        node = TableNode(open_pos=self.pos, close_pos=-1, depth=depth)
        self.pos += 1
        result: LuaTable = {}
        array_index = 1
        while True:
            entry_start = self.pos
            self.skip_space()
            if self.text.startswith("}", self.pos):
                node.close_pos = self.pos
                self.pos += 1
                return result, node
            key: LuaKey
            if self.text.startswith("[", self.pos) and self._long_bracket_level() is None:
                self.pos += 1
                raw_key, sub = self.parse_value(depth)
                if sub is not None or isinstance(raw_key, dict):
                    raise self.error("table keys are not supported")
                key = raw_key
                self.expect("]")
                self.expect("=")
            elif self._at_name_assignment():
                key = self._read_name()
                self.expect("=")
            else:
                key = array_index
                array_index += 1
            self.skip_space()
            value_start = self.pos
            value, sub_node = self.parse_value(depth)
            value_end = self.pos
            if isinstance(key, float) and key.is_integer():
                key = int(key)
            if key in result:
                raise self.error(f"duplicate key {key!r}")
            result[key] = value
            self.skip_space()
            if self.text.startswith((",", ";"), self.pos):
                self.pos += 1
            elif not self.text.startswith("}", self.pos):
                raise self.error("expected ',' or '}'")
            node.entries.append(Entry(key, entry_start, value_start, value_end, self.pos, sub_node))

    def _at_name_assignment(self) -> bool:
        char = self.text[self.pos] if self.pos < len(self.text) else ""
        if not (char.isalpha() or char == "_"):
            return False
        save = self.pos
        name = self._read_name()
        self.skip_space()
        is_assignment = self.text.startswith("=", self.pos) and not self.text.startswith(
            "==", self.pos
        )
        self.pos = save
        return is_assignment and name not in {"true", "false", "nil"}

    def _read_name(self) -> str:
        start = self.pos
        while self.pos < len(self.text) and (
            self.text[self.pos].isalnum() or self.text[self.pos] == "_"
        ):
            self.pos += 1
        return self.text[start : self.pos]

    def parse_scalar_word(self) -> LuaValue:
        """``true``, ``false`` or a number literal."""
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in " \t\r\n,;}]=":
            self.pos += 1
        word = self.text[start : self.pos]
        if word == "true":
            return True
        if word == "false":
            return False
        if word in ("nil", ""):
            raise self.error(f"unsupported value {word!r}")
        return self._parse_number(word)

    def _parse_number(self, word: str) -> int | float:
        sign = -1 if word.startswith("-") else 1
        body = word[1:] if word[:1] in "+-" else word
        try:
            if body.lower().startswith("0x"):
                return sign * int(body, 16)
            if body.isdigit():
                return sign * int(body)
            if body in ("inf", "nan"):
                raise ValueError(body)  # LuaJIT writes these but cannot read them back
            return sign * float(body)
        except ValueError:
            raise self.error(f"not a literal: {word[:40]!r}") from None

    def parse_string(self) -> str:
        """A quoted string with all Lua escapes; raw bytes survive via surrogateescape."""
        quote = self.text[self.pos]
        self.pos += 1
        out = bytearray()
        text = self.text
        while True:
            if self.pos >= len(text):
                raise self.error("unterminated string")
            char = text[self.pos]
            if char == quote:
                self.pos += 1
                return out.decode(ENCODING, ERRORS)
            if char == "\n":
                raise self.error("newline in string")
            if char != "\\":
                out += char.encode(ENCODING, ERRORS)
                self.pos += 1
                continue
            self.pos += 1
            out += self._parse_escape()

    _SIMPLE_ESCAPES: ClassVar[dict[str, bytes]] = {
        "a": b"\a",
        "b": b"\b",
        "f": b"\f",
        "n": b"\n",
        "r": b"\r",
        "t": b"\t",
        "v": b"\v",
        "\\": b"\\",
        '"': b'"',
        "'": b"'",
        "\n": b"\n",
    }

    def _parse_escape(self) -> bytes:
        text = self.text
        if self.pos >= len(text):
            raise self.error("unterminated escape")
        char = text[self.pos]
        if char in self._SIMPLE_ESCAPES:
            self.pos += 1
            return self._SIMPLE_ESCAPES[char]
        if char.isdigit():
            end = self.pos
            while end < len(text) and end - self.pos < 3 and text[end].isdigit():
                end += 1
            code = int(text[self.pos : end])
            if code > 255:
                raise self.error("escape too large")
            self.pos = end
            return bytes([code])
        if char == "x":
            digits = text[self.pos + 1 : self.pos + 3]
            if len(digits) != 2 or not all(c in "0123456789abcdefABCDEF" for c in digits):
                raise self.error("bad \\x escape")
            self.pos += 3
            return bytes([int(digits, 16)])
        if char == "z":
            self.pos += 1
            while self.pos < len(text) and text[self.pos] in " \t\r\n":
                self.pos += 1
            return b""
        if char == "u" and text.startswith("{", self.pos + 1):
            end = text.find("}", self.pos)
            if end < 0:
                raise self.error("bad \\u escape")
            code = int(text[self.pos + 2 : end], 16)
            self.pos = end + 1
            return chr(code).encode(ENCODING)
        raise self.error(f"unknown escape \\{char}")


# --------------------------------------------------------------------------- document


@dataclass
class SettingsDocument:
    """A parsed settings file that can be edited surgically.

    ``data`` holds the Python values; ``text`` the exact source. :meth:`set` and :meth:`delete`
    return a *new* document whose text differs from this one only inside the edited entry.
    """

    text: str
    data: LuaTable
    root: TableNode

    @classmethod
    def parse(cls, raw: bytes | str) -> SettingsDocument:
        text = raw.decode(ENCODING, ERRORS) if isinstance(raw, bytes) else raw
        data, root = _Parser(text).parse_document()
        return cls(text=text, data=data, root=root)

    def to_bytes(self) -> bytes:
        return self.text.encode(ENCODING, ERRORS)

    @property
    def header_path(self) -> str | None:
        """The path KOReader wrote into the first line, e.g. ``./settings.reader.lua``."""
        first = self.text.split("\n", 1)[0]
        return first[3:] if first.startswith("-- ") else None

    def is_canonical(self) -> bool:
        """True if the text is exactly what KOReader's own serializer would write."""
        header = self.header_path
        return header is not None and dump_file(self.data, header) == self.text

    def get(self, path: KeyPath) -> LuaValue | None:
        current: LuaValue = self.data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return current

    def set(self, path: KeyPath, value: LuaValue) -> SettingsDocument:
        """Set ``path`` to ``value``; missing parent tables are created."""
        if not path:
            raise ValueError("empty key path")
        node, depth_index = self._deepest_existing(path)
        remaining = path[depth_index:]
        entry = _find(node, remaining[0])
        if entry is not None and len(remaining) == 1:
            new_text = (
                self.text[: entry.value_start]
                + dump(value, node.depth + 1)  # the entry sits one level inside node
                + self.text[entry.value_end :]
            )
            return SettingsDocument.parse(new_text)
        new_value: LuaValue = value
        for key in reversed(remaining[1:]):
            new_value = {key: new_value}
        return SettingsDocument.parse(self._insert(node, remaining[0], new_value))

    def delete(self, path: KeyPath) -> SettingsDocument:
        """Remove ``path``; a missing key is not an error (returns an unchanged copy)."""
        node, depth_index = self._deepest_existing(path)
        if depth_index != len(path) - 1:
            return SettingsDocument.parse(self.text)
        entry = _find(node, path[-1])
        if entry is None:
            return SettingsDocument.parse(self.text)
        if len(node.entries) == 1:  # dump writes an empty table as "{}"
            new_text = self.text[: node.open_pos + 1] + self.text[node.close_pos :]
        else:
            new_text = self.text[: entry.start] + self.text[entry.end :]
        return SettingsDocument.parse(new_text)

    def _deepest_existing(self, path: KeyPath) -> tuple[TableNode, int]:
        """Walk ``path`` through existing tables; return the last table and how far we got."""
        node = self.root
        for index, key in enumerate(path[:-1]):
            entry = _find(node, key)
            if entry is None:
                return node, index
            if entry.table is None:
                raise LuaParseError(f"{key!r} is not a table")
            node = entry.table
        return node, len(path) - 1

    def _insert(self, node: TableNode, key: LuaKey, value: LuaValue) -> str:
        line = "\n" + INDENT * (node.depth + 1) + f"[{_dump_scalar(key)}] = "
        line += dump(value, node.depth + 1) + ","
        if not node.entries:
            closing = "\n" + INDENT * node.depth
            return self.text[: node.open_pos + 1] + line + closing + self.text[node.close_pos :]
        for entry in node.entries:
            if _sort_key_cmp(key, entry.key):
                return self.text[: entry.start] + line + self.text[entry.start :]
        last = node.entries[-1]
        return self.text[: last.end] + line + self.text[last.end :]


def _find(node: TableNode, key: LuaKey) -> Entry | None:
    for entry in node.entries:
        if entry.key == key and _lua_type(entry.key) == _lua_type(key):
            return entry
    return None


def changed_paths(before: LuaTable, after: LuaTable, prefix: KeyPath = ()) -> list[KeyPath]:
    """Leaf key paths whose value differs (added, removed or changed), in dump order."""
    changes: list[KeyPath] = []
    merged: LuaTable = {**before, **after}
    for key in ordered_keys(merged):
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            changes.extend(changed_paths(old, new, (*prefix, key)))
        elif old != new or _lua_type(old or 0) != _lua_type(new or 0):
            changes.append((*prefix, key))
    return changes
