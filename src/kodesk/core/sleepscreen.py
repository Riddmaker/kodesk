"""Sleep-screen pictures for PocketBook (B-14, D-14); no Qt here.

On PocketBook the firmware draws the sleep screen, not KOReader (checked on the owner's Verse Pro
Color): ``system/logo/sleeplogo/`` holds the BMP it shows, and its logo customizer
(``system/config/logos_customizer/logos.json``) makes that BMP from a picture the user picks in
``Photo/`` on the reader. kodesk touches neither: it only adds a new picture to ``Photo/``, and the
user picks it on the reader (Settings → Personalize → Logos).

A new picture never replaces a file: the name carries the time and the file is created
exclusively. Nothing in the KOReader folder changes, so the snapshot taken before (hard rule)
verifies byte for byte that the reader's setup is untouched.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

PHOTO_DIR = "Photo"
SLEEP_LOGO = "system/logo/sleeplogo/pb_sleeplogo.bmp"  # the firmware's picture (read only)
MAX_BYTES = 32 * 1024 * 1024  # a screen-sized PNG is a few MB; anything bigger is a bug
PICTURE_NAME = re.compile(r"kodesk-sleep-\d{8}-\d{6}\.png")  # as picture_name makes them
PLATFORMS = ("pocketbook",)  # Kobo/Kindle: KOReader's own screensaver settings (not built)


class SleepScreenError(Exception):
    """The picture could not be placed; nothing was left behind."""


def picture_name(now: datetime) -> str:
    return f"kodesk-sleep-{now:%Y%m%d-%H%M%S}.png"


def photo_folder(volume: Path) -> Path:
    return volume / PHOTO_DIR


def place_picture(volume: Path, data: bytes, now: datetime) -> Path:
    """Add ``data`` (a PNG) as a new file in the reader's ``Photo/`` folder; return its path."""
    if not data or len(data) > MAX_BYTES:
        raise SleepScreenError(f"the picture has an unexpected size ({len(data)} bytes)")
    folder = photo_folder(volume)
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise SleepScreenError(f"{PHOTO_DIR} on the reader is not a plain folder")
    folder.mkdir(exist_ok=True)  # the firmware creates it; a reset reader may not have it yet
    target = folder / picture_name(now)
    try:
        with target.open("xb") as file:  # never replace an existing picture
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError as error:
        raise SleepScreenError(f"{target.name} is already on the reader") from error
    except OSError as error:
        target.unlink(missing_ok=True)
        raise SleepScreenError(f"could not write {target.name}: {error.strerror}") from error
    return target
