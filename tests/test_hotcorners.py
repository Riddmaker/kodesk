"""Hot corners (B-22): reading, describing and changing one gesture entry."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodesk.core import hotcorners as hc
from kodesk.core.install import read_settings_file
from kodesk.core.luadump import LuaValue, SettingsDocument, changed_paths
from kodesk.core.settings_write import StaleSettingsError, write_documents
from kodesk.core.snapshot import SnapshotStore


@pytest.fixture(name="gestures")
def fixture_gestures(reader: Path) -> SettingsDocument:
    return read_settings_file(reader / hc.GESTURES_FILE)


def test_corners_use_koreaders_names() -> None:
    assert [c.tap for c in hc.CORNERS] == [
        "tap_top_left_corner",
        "tap_top_right_corner",
        "tap_left_bottom_corner",
        "tap_right_bottom_corner",
    ]
    assert len(hc.ACTIONS) == 12


def test_describe_entries(gestures: SettingsDocument) -> None:
    assert hc.describe(hc.current(gestures, "tap_top_left_corner")) == "Toggle page flipping"
    assert hc.describe(hc.current(gestures, "tap_right_bottom_corner")) == "Pass through"
    assert (
        hc.describe(hc.current(gestures, "hold_bottom_right_corner")) == "screenshot + toggle_wifi"
    )
    assert hc.describe({"toc": True, "night_mode": True}) == "Table of contents + Toggle night mode"
    assert hc.describe("odd") == "Something kodesk does not know"


def test_action_of_only_takes_one_offered_action() -> None:
    assert hc.action_of({"toc": True}) == "toc"
    assert hc.action_of(None) is None
    odd: list[LuaValue] = [{"toc": True, "skim": True}, {"screenshot": True}, {"toc": 0}, "x"]
    for value in odd:
        with pytest.raises(KeyError):
            hc.action_of(value)


def test_without_a_gestures_file_the_defaults_are_shown() -> None:
    assert hc.current(None, "tap_top_right_corner") == {"toggle_bookmark": True}
    assert hc.current(None, "hold_top_left_corner") is None


def test_changing_one_corner_changes_only_that_entry(gestures: SettingsDocument) -> None:
    after = hc.with_action(gestures, "tap_right_bottom_corner", "night_mode")
    assert changed_paths(gestures.data, after.data) == [
        ("gesture_reader", "tap_right_bottom_corner")
    ]
    old, new = gestures.text.splitlines(), after.text.splitlines()
    added = [line for line in new if line not in old]
    assert added == [
        '        ["tap_right_bottom_corner"] = {',
        '            ["night_mode"] = true,',
    ]
    assert after.text.replace("\n".join(added) + "\n        },\n", "") == gestures.text


def test_pass_through_removes_the_entry(gestures: SettingsDocument) -> None:
    after = hc.with_action(gestures, "tap_top_right_corner", None)
    assert hc.current(after, "tap_top_right_corner") is None
    assert changed_paths(gestures.data, after.data) == [("gesture_reader", "tap_top_right_corner")]
    with pytest.raises(ValueError, match="not an offered action"):
        hc.with_action(gestures, "tap_top_right_corner", "screenshot")


def test_written_with_settings_in_one_snapshot(reader: Path, store: SnapshotStore) -> None:
    gestures = read_settings_file(reader / hc.GESTURES_FILE)
    settings = read_settings_file(reader / "settings.reader.lua")
    changed_gestures = hc.with_action(gestures, "hold_top_left_corner", "toc")
    snapshot = write_documents(
        store,
        reader,
        {
            "settings.reader.lua": (settings, settings.set(("copt_font_size",), 26)),
            hc.GESTURES_FILE: (gestures, changed_gestures),
        },
    )
    assert (reader / hc.GESTURES_FILE).read_bytes() == changed_gestures.to_bytes()
    assert '["copt_font_size"] = 26' in (reader / "settings.reader.lua").read_text()
    assert store.snapshots() == [snapshot]


def test_refuses_other_files_and_stale_gestures(reader: Path, store: SnapshotStore) -> None:
    gestures = read_settings_file(reader / hc.GESTURES_FILE)
    with pytest.raises(ValueError, match="does not write"):
        write_documents(store, reader, {"settings/koassistant.lua": (gestures, gestures)})
    (reader / hc.GESTURES_FILE).write_text("return {}\n")  # KOReader flushed in between
    with pytest.raises(StaleSettingsError, match=r"gestures\.lua"):
        write_documents(
            store,
            reader,
            {hc.GESTURES_FILE: (gestures, hc.with_action(gestures, "hold_top_left_corner", "toc"))},
        )
    assert not store.snapshots()
