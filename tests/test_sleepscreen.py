"""Sleep-screen pictures go into Photo/ as new files, never replacing anything (B-14)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from kodesk.core.sleepscreen import MAX_BYTES, SleepScreenError, place_picture

NOW = datetime(2026, 11, 26, 23, 5, 9)


def test_picture_is_added_as_a_new_file(tmp_path: Path) -> None:
    (tmp_path / "Photo").mkdir()
    (tmp_path / "Photo" / "holiday.jpg").write_bytes(b"mine")
    placed = place_picture(tmp_path, b"png", NOW)
    assert placed == tmp_path / "Photo" / "kodesk-sleep-20261126-230509.png"
    assert placed.read_bytes() == b"png"
    assert (tmp_path / "Photo" / "holiday.jpg").read_bytes() == b"mine"


def test_missing_photo_folder_is_created(tmp_path: Path) -> None:
    assert place_picture(tmp_path, b"png", NOW).parent.is_dir()


def test_an_existing_file_is_never_replaced(tmp_path: Path) -> None:
    first = place_picture(tmp_path, b"first", NOW)
    with pytest.raises(SleepScreenError, match="already on the reader"):
        place_picture(tmp_path, b"second", NOW)
    assert first.read_bytes() == b"first"


def test_photo_must_be_a_plain_folder(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    volume = tmp_path / "volume"
    volume.mkdir()
    (volume / "Photo").symlink_to(elsewhere)
    with pytest.raises(SleepScreenError, match="not a plain folder"):
        place_picture(volume, b"png", NOW)
    assert not any(elsewhere.iterdir())


@pytest.mark.parametrize("size", [0, MAX_BYTES + 1])
def test_unexpected_sizes_are_refused(tmp_path: Path, size: int) -> None:
    with pytest.raises(SleepScreenError, match="unexpected size"):
        place_picture(tmp_path, b"x" * size, NOW)
    assert not (tmp_path / "Photo").exists()
