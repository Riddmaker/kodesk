"""Store catalogue (B-07): parsing, cache with ETag, offline, URL rules. Never the network."""

from __future__ import annotations

import urllib.request
from collections.abc import Mapping
from pathlib import Path

import pytest

from kodesk.core import catalogue as cat
from kodesk.core import net
from kodesk.core.catalogue import CatalogueError, Kind, Response
from tests.conftest import THIRD_PARTY_PLUGIN, USER_PATCH, fake_catalogue


class FakeServer:
    """Answers like GitHub Pages: 200 with an ETag, 304 when If-None-Match matches."""

    def __init__(self) -> None:
        index, patches = fake_catalogue()
        self.files = {cat.INDEX_URL: index, cat.PATCHES_URL: patches}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.down = False

    def __call__(self, url: str, headers: Mapping[str, str], limit: int) -> Response:
        self.requests.append((url, dict(headers)))
        if self.down:
            raise OSError("network unreachable")
        if url not in self.files:
            raise CatalogueError(f"{url}: HTTP 404", 404)
        body = self.files[url]
        assert len(body) <= limit
        etag = f'"{len(body)}"'
        if headers.get("If-None-Match") == etag:
            return Response(304, etag, b"")
        return Response(200, etag, body)


def test_parses_valid_entries_and_drops_the_rest() -> None:
    catalogue = cat.parse(*fake_catalogue())
    names = [(e.kind, e.name) for e in catalogue.entries]
    assert names == [  # most stars first; "evil" (not on github.com) and notlua.txt dropped
        (Kind.PLUGIN, THIRD_PARTY_PLUGIN),
        (Kind.PLUGIN, "dictsync"),
        (Kind.PATCH, USER_PATCH),
        (Kind.PLUGIN, "readingclock"),
        (Kind.PLUGIN, "nolicence"),
    ]
    first = catalogue.entries[0]
    assert first.url == f"https://github.com/alice/{THIRD_PARTY_PLUGIN}.koplugin"
    assert first.text_url == (
        f"https://raw.githubusercontent.com/alice/{THIRD_PARTY_PLUGIN}.koplugin/main/README.md"
    )
    assert catalogue.entries[-1].license is None
    assert catalogue.categories["dict"] == "Dictionary & language"
    assert cat.built(catalogue) == "26 Nov 2026"


def test_search_filters_and_words() -> None:
    entries = cat.parse(*fake_catalogue()).entries
    assert [e.name for e in cat.search(entries, "clock")] == ["readingclock"]
    assert [e.name for e in cat.search(entries, "TIME reading")] == ["readingclock"]
    assert [e.name for e in cat.search(entries, kind=Kind.PATCH)] == [USER_PATCH]
    assert [e.name for e in cat.search(entries, category="sync")] == ["dictsync"]
    assert cat.search(entries, "nothing-like-this") == []


def test_installed_by_name() -> None:
    entries = {e.name: e for e in cat.parse(*fake_catalogue()).entries}
    plugins, patches = frozenset({THIRD_PARTY_PLUGIN}), frozenset({USER_PATCH})
    assert cat.is_installed(entries[THIRD_PARTY_PLUGIN], plugins, patches)
    assert cat.is_installed(entries[USER_PATCH], plugins, patches)
    assert not cat.is_installed(entries["dictsync"], plugins, patches)


def test_cache_revalidates_with_the_etag(tmp_path: Path) -> None:
    server = FakeServer()
    first = cat.load(tmp_path, server)
    second = cat.load(tmp_path, server)
    assert first.entries == second.entries and not second.offline
    assert server.requests[2][1]["If-None-Match"] == f'"{len(server.files[cat.INDEX_URL])}"'


def test_offline_uses_the_cache_and_says_so(tmp_path: Path) -> None:
    server = FakeServer()
    cat.load(tmp_path, server)
    server.down = True
    assert cat.load(tmp_path, server).offline
    assert cat.load(tmp_path, None).offline  # kodesk --offline: no request at all
    assert len(server.requests) == 3  # 2 first load, 1 failed try; --offline sent none


def test_no_cache_and_no_network(tmp_path: Path) -> None:
    server = FakeServer()
    server.down = True
    with pytest.raises(CatalogueError, match="Could not load the catalogue"):
        cat.load(tmp_path, server)
    with pytest.raises(CatalogueError, match="offline mode"):
        cat.load(tmp_path, None)


def test_broken_index_is_an_error(tmp_path: Path) -> None:
    server = FakeServer()
    server.files[cat.INDEX_URL] = b"<html>maintenance</html>"
    with pytest.raises(CatalogueError, match="not valid JSON"):
        cat.load(tmp_path, server)
    server.files[cat.INDEX_URL] = b"[]"
    with pytest.raises(CatalogueError, match="unexpected format"):
        cat.load(tmp_path, server)


@pytest.mark.parametrize(
    "url",
    [
        "http://omer-faruq.github.io/koreader-plugin-index/index.json",
        "https://evil.example/index.json",
        "https://user:pw@raw.githubusercontent.com/a/b/main/README.md",
        "https://raw.githubusercontent.com:8443/a/b/main/README.md",
        "file:///etc/passwd",
        "https://omer-faruq.github.io.evil.example/index.json",
    ],
)
def test_only_https_to_the_allowed_hosts(url: str) -> None:
    with pytest.raises(CatalogueError, match="refusing"):
        cat.check_url(url)
    with pytest.raises(CatalogueError, match="refusing"):
        cat.https_get(url, {}, 10)  # refused before any connection


def test_readme_tries_the_usual_names() -> None:
    entry = next(e for e in cat.parse(*fake_catalogue()).entries if e.name == "dictsync")
    server = FakeServer()
    lower = "https://raw.githubusercontent.com/carol/dictsync/main/readme.md"
    server.files[lower] = b"# Dict sync\n"
    assert cat.fetch_text(entry, server) == "# Dict sync\n"
    assert [u for u, _h in server.requests][-1] == lower
    server.files.pop(lower)
    with pytest.raises(CatalogueError, match="404"):
        cat.fetch_text(entry, server)


def test_patch_text_is_the_file_itself() -> None:
    entry = next(e for e in cat.parse(*fake_catalogue()).entries if e.kind is Kind.PATCH)
    server = FakeServer()
    assert entry.text_url is not None
    server.files[entry.text_url] = b"-- patch\n"
    assert cat.fetch_text(entry, server) == "-- patch\n"


@pytest.mark.parametrize(
    "target",
    ["http://omer-faruq.github.io/index.json", "https://evil.example/index.json"],
)
def test_redirects_follow_the_same_rules(target: str) -> None:
    # pylint: disable-next=protected-access  # the handler https_get installs
    handler = net._CheckedRedirects()
    request = urllib.request.Request(cat.INDEX_URL)
    with pytest.raises(CatalogueError, match="refusing"):
        handler.redirect_request(request, None, 302, "Found", {}, target)
    allowed = "https://raw.githubusercontent.com/a/b/main/README.md"
    followed = handler.redirect_request(request, None, 302, "Found", {}, allowed)
    assert followed is not None and followed.full_url == allowed
