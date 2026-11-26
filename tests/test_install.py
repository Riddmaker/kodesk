"""Finding KOReader on fake reader trees (read-only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodesk.core.install import InstallError, find_install, read_settings_file


def test_finds_pocketbook_from_mount_point(pocketbook: Path) -> None:
    install = find_install(pocketbook)
    assert install.platform == "pocketbook"
    assert install.version == "v2026.07.2"
    assert install.model == "PBVerseProColor"
    assert install.device is not None and install.device.dpi == 300
    assert install.read_settings().get(("copt_font_size",)) == 22


def test_finds_from_koreader_dir_itself(pocketbook: Path) -> None:
    install = find_install(pocketbook / "applications" / "koreader")
    assert install.platform == "pocketbook"


@pytest.mark.parametrize(
    ("platform", "relative"), [("kobo", ".adds/koreader"), ("kindle", "koreader")]
)
def test_other_layouts(tmp_path: Path, platform: str, relative: str) -> None:
    koreader = tmp_path / relative
    koreader.mkdir(parents=True)
    (koreader / "reader.lua").write_text("")
    install = find_install(tmp_path)
    assert install.platform == platform
    assert install.version is None and install.device is None


def test_no_install(tmp_path: Path) -> None:
    with pytest.raises(InstallError):
        find_install(tmp_path)


def test_hostile_settings_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "settings.reader.lua"
    path.write_text("return os.execute('touch /tmp/pwned')")
    with pytest.raises(InstallError):
        read_settings_file(path)


def test_missing_settings_file_means_defaults(tmp_path: Path) -> None:
    assert not read_settings_file(tmp_path / "nope.lua").data
