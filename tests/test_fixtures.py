"""The fake reader trees and release archives themselves (B-05)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from kodesk.core.install import find_install
from kodesk.core.inventory import Origin, list_patches, list_plugins
from tests.conftest import (
    RELEASE,
    THIRD_PARTY_PLUGIN,
    USER_PATCH,
    make_reader,
    make_release_zip,
    release_listing,
)


@pytest.mark.parametrize("platform", ["pocketbook", "kobo", "kindle"])
def test_fake_reader_is_found_with_its_user_state(tmp_path: Path, platform: str) -> None:
    koreader = make_reader(tmp_path, platform)
    install = find_install(tmp_path)
    assert (install.root, install.platform, install.version) == (koreader, platform, RELEASE)
    plugins = {p.name: p.origin for p in list_plugins(install.root, install.read_settings())}
    assert plugins[THIRD_PARTY_PLUGIN] is Origin.THIRD_PARTY
    assert plugins["statistics"] is Origin.BUILT_IN
    assert [p.name for p in list_patches(install.root) if p.runs] == [USER_PATCH]
    assert (tmp_path / "Books" / "Alice.sdr" / "metadata.epub.lua").is_file()


def test_release_archives_carry_no_user_state() -> None:
    """The research finding the update pipeline relies on (koreader-releases §2.1)."""
    user_files = {"settings.reader.lua", "history.lua", "defaults.custom.lua"}
    user_dirs = {"settings", "styletweaks", "patches", "history"}
    for platform in ("pocketbook", "kobo", "kindle"):
        for name in release_listing(platform):
            parts = name.split("/")
            assert parts[-1] not in user_files, name
            assert not user_dirs.intersection(parts[:-1]), name
            assert not any(part.endswith(".sdr") for part in parts), name


def test_fake_release_zip_can_carry_a_zip_slip_member(tmp_path: Path) -> None:
    archive = make_release_zip(
        tmp_path / "koreader.zip", "pocketbook", extra={"../../evil.sh": b"rm -rf /"}
    )
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "../../evil.sh" in names
    assert "applications/koreader/reader.lua" in names
