"""Unit tests for kodesk.core.luadump (pure logic, no Qt)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from kodesk.core.luadump import (
    KeyPath,
    LuaParseError,
    LuaTable,
    LuaValue,
    SettingsDocument,
    changed_paths,
    dump,
    dump_file,
    lua_number,
    lua_quote,
)

SAMPLE_DATA: LuaTable = {
    "copt_font_size": 22,
    "copt_line_spacing": 100,
    "copt_h_page_margins": {1: 15, 2: 15},
    "night_mode": False,
    "cre_font": "Noto Serif",
    "footer": {"all_at_once": True, "text_font_size": 14, "order": {1: "time", 2: "pages"}},
    "lastfile": '/mnt/ext1/Books/a "quoted" name\nwith newline.epub',
    "ratio": 0.33333333333333,
    "empty": {},
}


def sample_text() -> str:
    return dump_file(SAMPLE_DATA, "./settings.reader.lua")


def test_dump_matches_koreader_layout() -> None:
    text = sample_text()
    assert text.startswith('-- ./settings.reader.lua\nreturn {\n    ["copt_font_size"] = 22,\n')
    assert '    ["copt_h_page_margins"] = {\n        [1] = 15,\n        [2] = 15,\n    },\n' in text
    assert '    ["empty"] = {},\n' in text
    assert text.endswith("\n}\n")


def test_keys_sorted_like_ordered_pairs() -> None:
    keys = list(SettingsDocument.parse(sample_text()).data)
    assert keys == sorted(keys, key=lambda k: str(k).encode())


def test_round_trip_is_byte_identical() -> None:
    text = sample_text()
    doc = SettingsDocument.parse(text)
    assert doc.data == SAMPLE_DATA
    assert doc.text == text
    assert doc.is_canonical()


def test_set_existing_key_changes_one_line() -> None:
    doc = SettingsDocument.parse(sample_text())
    new = doc.set(("copt_font_size",), 26)
    before, after = doc.text.splitlines(), new.text.splitlines()
    assert len(before) == len(after)
    diff = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert diff == [('    ["copt_font_size"] = 22,', '    ["copt_font_size"] = 26,')]
    assert new.is_canonical()


def test_set_nested_and_new_keys_stay_canonical() -> None:
    doc = SettingsDocument.parse(sample_text())
    doc = doc.set(("footer", "text_font_size"), 16)
    doc = doc.set(("bbb_new", "deep", "x"), "y")
    doc = doc.set(("empty", "first"), 1)
    doc = doc.set(("zzz_last",), True)
    assert doc.get(("footer", "text_font_size")) == 16
    assert doc.get(("bbb_new", "deep", "x")) == "y"
    assert doc.is_canonical()


def test_delete_keys_stays_canonical() -> None:
    doc = SettingsDocument.parse(sample_text())
    doc = doc.delete(("night_mode",)).delete(("footer", "order", 1)).delete(("footer", "order", 2))
    assert "night_mode" not in doc.data
    assert doc.get(("footer", "order")) == {}
    assert doc.is_canonical()
    assert doc.delete(("does", "not", "exist")).text == doc.text


def test_non_canonical_file_edit_keeps_other_bytes() -> None:
    hand_written = 'return {\n  b = 2, -- comment\n  ["a"]=1;\n}\n'
    doc = SettingsDocument.parse(hand_written)
    assert doc.data == {"b": 2, "a": 1}
    assert not doc.is_canonical()
    new = doc.set(("a",), 5)
    assert new.text == 'return {\n  b = 2, -- comment\n  ["a"]=5;\n}\n'


def test_changed_paths() -> None:
    footer = SAMPLE_DATA["footer"]
    assert isinstance(footer, dict)
    after: LuaTable = {**SAMPLE_DATA, "copt_font_size": 24, "footer": {**footer, "x": 1}}
    del after["night_mode"]
    assert changed_paths(SAMPLE_DATA, after) == [
        ("copt_font_size",),
        ("footer", "x"),
        ("night_mode",),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "return os.execute('rm -rf /')",
        "return { a = os.getenv('HOME') }",
        "return { a = 1 + 2 }",
        "return { a = nil }",
        "x = 1 return {}",
        "return { a = 1, a = 2 }",
        "return { a = inf }",
        "return { a = 'unterminated }",
        "return {" * 100 + "}" * 100,
    ],
)
def test_rejects_anything_but_literals(bad: str) -> None:
    with pytest.raises(LuaParseError):
        SettingsDocument.parse(bad)


def test_string_escapes_and_invalid_utf8_round_trip() -> None:
    raw = b'-- x\nreturn {\n    ["k"] = "\\0\\0012\\9tab\\\\ \\"q\\" \\\n nl \xff\xfe end",\n}\n'
    doc = SettingsDocument.parse(raw)
    assert doc.to_bytes() == raw
    assert doc.is_canonical()


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (0, "0"),
        (-0.0, "-0"),
        (22, "22"),
        (0.5, "0.5"),
        (1e14, "1e+14"),
        (1 / 3, "0.33333333333333"),
        (123456789012345678, "1.2345678901235e+17"),
        (float("inf"), "inf"),
    ],
)
def test_lua_number(number: float, expected: str) -> None:
    assert lua_number(number) == expected


LUAJIT = shutil.which("luajit")
CORPUS = [
    "plain",
    'q"uote',
    "back\\slash",
    "new\nline",
    "cr\rx",
    "\x00",
    "\x001",
    "tab\t9",
    "\x7f",
    "\x1f",
    "ümlaut ✓",
    "\x0012",
]
NUMBERS = [0, 1, -1, 0.1, 1 / 3, 2**53, 1e100, 123.456, -0.000001, 15, 100000000000000]


@pytest.mark.skipif(LUAJIT is None, reason="luajit not on PATH (it is in the nix dev shell)")
def test_serializer_matches_real_luajit() -> None:
    """Cross-check %q and tostring() against LuaJIT itself, the interpreter KOReader runs on."""
    script = (
        "local s = io.read('*a')\n"
        "for item in s:gmatch('([^\\255]*)\\255') do\n"
        "  io.write(string.format('%q', item), '\\255')\n"
        "end\n"
        f"for _, n in ipairs({{{', '.join(repr(n) for n in NUMBERS)}}}) do\n"
        "  io.write(tostring(n), '\\255')\n"
        "end\n"
    )
    stdin = b"".join(item.encode() + b"\xff" for item in CORPUS)
    assert LUAJIT is not None
    result = subprocess.run([LUAJIT, "-e", script], input=stdin, capture_output=True, check=True)
    outputs = result.stdout.split(b"\xff")[:-1]
    expected = [lua_quote(item).encode() for item in CORPUS] + [
        lua_number(n).encode() for n in NUMBERS
    ]
    assert outputs == expected


def test_dump_scalar_and_type_errors() -> None:
    assert dump("a") == '"a"'
    assert dump(True) == "true"
    with pytest.raises(TypeError):
        dump(None)  # type: ignore[arg-type]


NESTED = """return {
    ["a"] = {
        ["b"] = {
            ["x"] = true,
        },
    },
    ["c"] = {
        ["y"] = 1,
    },
}
"""


@pytest.mark.parametrize(("path", "value"), [(("a", "b"), {"x": True}), (("c",), {"y": 1})])
def test_replacing_a_table_with_itself_changes_no_byte(path: KeyPath, value: LuaValue) -> None:
    """Regression (B-22): a table value was dumped one level too shallow when replacing."""
    document = SettingsDocument.parse(NESTED)
    assert document.set(path, value).text == NESTED
