"""Which version is installed and how it is checked (B-08). Fake GitHub, never the network."""

from __future__ import annotations

import pytest

from kodesk.core import catalogue as cat
from kodesk.core.net import NetError
from kodesk.core.sources import (
    Download,
    fetch,
    fetch_patch,
    git_blob_sha1,
    is_newer,
    latest,
    probable_sources,
)
from tests.conftest import FakeGitHub, fake_catalogue, plugin_files, plugin_zip

COMMIT = "2a9577b414fc9da63606784c71a7f9b6098df73d"


def _entry(name: str) -> cat.Entry:
    return next(e for e in cat.parse(*fake_catalogue()).entries if e.name == name)


def test_release_asset_is_checked_against_github_digest() -> None:
    github = FakeGitHub()
    data = plugin_zip(plugin_files("readingclock.koplugin"))
    github.release("bob", "readingclock.koplugin", "v1.2.0", {"readingclock.koplugin.zip": data})
    download = latest(_entry("readingclock"), github)
    assert download.tag == "v1.2.0" and download.version == "v1.2.0"
    assert download.sha256 and "SHA-256" in download.check
    assert fetch(download, github) == data
    github.files[download.url] = data + b"tampered"
    with pytest.raises(NetError, match="does not match"):
        fetch(download, github)


def test_release_without_a_single_checked_asset_uses_the_tag_archive() -> None:
    github = FakeGitHub()
    github.release("bob", "readingclock.koplugin", "v2.0", {"a.zip": b"1", "b.zip": b"2"})
    download = latest(_entry("readingclock"), github)
    assert download.url == (
        "https://codeload.github.com/bob/readingclock.koplugin/zip/refs/tags/v2.0"
    )
    assert download.sha256 is None and "no checksum" in download.check


def test_no_release_means_the_current_commit_of_the_default_branch() -> None:
    github = FakeGitHub()
    github.files["https://api.github.com/repos/carol/dictsync/commits/main"] = COMMIT.encode()
    download = latest(_entry("dictsync"), github)
    assert download.commit == COMMIT and download.tag is None
    assert download.url == f"https://codeload.github.com/carol/dictsync/zip/{COMMIT}"
    assert download.version == "no release, commit 2a9577b"


def test_odd_answers_are_refused() -> None:
    github = FakeGitHub()
    github.files["https://api.github.com/repos/carol/dictsync/commits/main"] = b"<html>"
    with pytest.raises(NetError, match="did not name the current commit"):
        latest(_entry("dictsync"), github)
    github.files["https://api.github.com/repos/carol/dictsync/releases/latest"] = (
        b'{"tag_name": "../../evil", "assets": []}'
    )
    with pytest.raises(NetError, match="did not name"):  # bad tag: treated as no release
        latest(_entry("dictsync"), github)


def test_git_blob_sha1_matches_git() -> None:
    assert git_blob_sha1(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"  # git's empty blob
    assert git_blob_sha1(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_patch_is_compared_with_the_index() -> None:
    entry = _entry("2-reader-footer-tweak.lua")
    assert entry.text_url is not None
    github = FakeGitHub()
    github.files[entry.text_url] = b"-- patch\n"
    assert fetch_patch(entry, github).matches_index is None  # no file_sha in the fake index
    same = cat.Entry(**{**entry.__dict__, "file_sha": git_blob_sha1(b"-- patch\n")})
    assert fetch_patch(same, github).matches_index is True
    github.files[entry.text_url] = b"-- newer\n"
    assert fetch_patch(same, github).matches_index is False


def test_probable_sources_and_newer() -> None:
    entries = cat.parse(*fake_catalogue()).entries
    found = probable_sources(entries, cat.Kind.PLUGIN, "ReadingClock")
    assert [e.id for e in found] == ["bob/readingclock.koplugin"]
    assert probable_sources(entries, cat.Kind.PATCH, "2-reader-footer-tweak.lua")
    assert not probable_sources(entries, cat.Kind.PLUGIN, "statistics")
    release = Download("o", "r", "https://codeload.github.com/x", tag="v2")
    assert is_newer("v1", None, release) and not is_newer("v2", None, release)
    head = Download("o", "r", "https://codeload.github.com/x", commit=COMMIT)
    assert not is_newer(None, COMMIT, head) and is_newer("v1", None, head)
