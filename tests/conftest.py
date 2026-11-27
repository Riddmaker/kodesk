"""Shared fixtures: fake reader trees in tmp dirs (never the real device, HABIT 7)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from kodesk.core.luadump import dump_file
from kodesk.core.net import CancelledError, NetError, Response
from kodesk.core.snapshot import SnapshotStore

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

OWNER_LIKE_SETTINGS: dict[object, object] = {
    "copt_font_size": 22,
    "copt_h_page_margins": {1: 10, 2: 10},
    "copt_t_page_margin": 10,
    "copt_b_page_margin": 5,
    "copt_line_spacing": 100,
    "copt_status_line": 0,
    "cre_header_status_font_size": 28,
    "cre_font": "Noto Serif",
    "night_mode": True,
    "home_dir": "/mnt/ext1/Books",
    "footer": {"container_height": 14, "container_bottom_padding": 1, "disabled": False},
    "reader_footer_mode": 1,
}


def make_pocketbook(root: Path, settings: dict[object, object] | None = None) -> Path:
    """A minimal PocketBook volume with KOReader v2026.07.2 on a Verse Pro Color."""
    koreader = root / "applications" / "koreader"
    koreader.mkdir(parents=True)
    (koreader / "reader.lua").write_text("-- fake\n")
    (koreader / "git-rev").write_text("v2026.07.2\n")
    (koreader / "version.log").write_text(
        "2026-02-11, 19:37:30, v2025.10, PBVerseProColor\n"
        "2026-08-26, 08:01:22, v2026.07.2, PBVerseProColor\n"
    )
    data = OWNER_LIKE_SETTINGS if settings is None else settings
    (koreader / "settings.reader.lua").write_text(
        dump_file(data, "./settings.reader.lua")  # type: ignore[arg-type]
    )
    (koreader / "settings").mkdir()
    (koreader / "settings/gestures.lua").write_text(gestures_lua())
    books = root / "Books"
    books.mkdir()
    (books / "Alice.epub").write_bytes(b"PK")
    (books / "Alice.sdr").mkdir()
    (books / "Classics").mkdir()
    return root


@pytest.fixture
def pocketbook(tmp_path: Path) -> Path:
    return make_pocketbook(tmp_path / "PB634K3")


LISTINGS = Path(__file__).parent / "fixtures" / "listings"
RELEASE = "v2026.07.1"
# Where each archive is unpacked on the reader's USB volume (research: koreader-releases §2).
UNPACK_DIR = {"pocketbook": "", "kobo": ".adds/", "kindle": ""}
KOREADER_DIR = {
    "pocketbook": "applications/koreader",
    "kobo": ".adds/koreader",
    "kindle": "koreader",
}
THIRD_PARTY_PLUGIN = "projecttitle"
USER_PATCH = "2-reader-footer-tweak.lua"


def release_listing(platform: str) -> list[str]:
    """Member names of the real release archive (tools/fixture-listings.py)."""
    path = LISTINGS / f"koreader-{platform}-{RELEASE}.txt"
    return path.read_text(encoding="utf-8").split()


# Archive members KOReader's own update skips, so its ota/package.index leaves them out.
NOT_INDEXED = {"pocketbook": ("system/",), "kobo": ("koreader.png",), "kindle": ()}


def make_release_zip(  # pylint: disable=too-many-arguments
    target: Path,
    platform: str,
    extra: dict[str, bytes] | None = None,
    *,
    version: str = RELEASE,
    changed: dict[str, bytes] | None = None,
    dropped: tuple[str, ...] = (),
) -> Path:
    """A fake release archive with the real member names.

    ``extra`` adds hostile members (not in the index); ``changed`` replaces or adds shipped
    files (listed in the index); ``dropped`` leaves shipped files out of archive and index.
    """
    files = shipped_files(platform, version, changed, dropped)
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return target


def shipped_files(
    platform: str,
    version: str = RELEASE,
    changed: dict[str, bytes] | None = None,
    dropped: tuple[str, ...] = (),
) -> dict[str, bytes]:
    """Archive member -> content of a fake release (see :func:`make_release_zip`)."""
    names = [n for n in release_listing(platform) if n not in dropped]
    names += [n for n in (changed or {}) if n not in names]
    index = next(n for n in names if n.endswith("ota/package.index"))
    listed = [n for n in names if not n.startswith(NOT_INDEXED[platform])]
    files = {}
    for name in names:
        if name == index:
            files[name] = "".join(f"{member}\n" for member in listed).encode()
        elif name in (changed or {}):
            files[name] = (changed or {})[name]
        else:
            files[name] = _shipped_content(name, version)
    return files


def _shipped_content(name: str, version: str = RELEASE) -> bytes:
    if name.endswith("/git-rev"):
        return f"{version}\n".encode()
    if name.endswith(".app"):
        return f"#!/bin/sh\n# {name} {version}\n".encode()
    return f"-- {name}\n".encode()


# settings/gestures.lua as KOReader leaves it after its first start (defaults copied, B-22), plus
# one corner the user gave two actions to and a multiswipe: kodesk must keep both as they are.
OWNER_LIKE_GESTURES = {
    "custom_multiswipes": {},
    "gesture_fm": {"tap_left_bottom_corner": {"toggle_frontlight": True}},
    "gesture_reader": {
        "hold_bottom_right_corner": {
            "screenshot": True,
            "settings": {"order": {1: "screenshot", 2: "toggle_wifi"}},
            "toggle_wifi": True,
        },
        "multiswipe_north_south": {"folder_up": True},
        "tap_left_bottom_corner": {"toggle_frontlight": True},
        "tap_top_left_corner": {"toggle_page_flipping": True},
        "tap_top_right_corner": {"toggle_bookmark": True},
    },
}


def gestures_lua() -> str:
    """OWNER_LIKE_GESTURES as KOReader's serializer writes the file."""
    return dump_file(
        OWNER_LIKE_GESTURES,  # type: ignore[arg-type]
        "/mnt/ext1/applications/koreader/settings/gestures.lua",
    )


def make_reader(root: Path, platform: str, settings: dict[object, object] | None = None) -> Path:
    """A reader volume: the real release's files plus typical user state (B-05).

    User state: settings, history, a settings/ file, a style tweak, a third-party plugin, a user
    patch and a book with its .sdr metadata. Returns the KOReader directory.
    """
    for name, shipped in shipped_files(platform).items():
        path = root / (UNPACK_DIR[platform] + name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(shipped)
    koreader = root / KOREADER_DIR[platform]
    data = OWNER_LIKE_SETTINGS if settings is None else settings
    user_files = {
        "settings.reader.lua": dump_file(data, "./settings.reader.lua"),  # type: ignore[arg-type]
        "settings/gestures.lua": gestures_lua(),
        "history.lua": 'return {\n    [1] = {\n        ["file"] = "/mnt/ext1/Books/Alice.epub",\n'
        '        ["time"] = 1764000000,\n    },\n}\n',
        "settings/vocabulary_builder.sqlite3": "fake db",
        "styletweaks/justify.css": "p { text-align: justify; }\n",
        f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/main.lua": "-- third-party\n",
        f"plugins/{THIRD_PARTY_PLUGIN}.koplugin/_meta.lua": "return { name = 'projecttitle' }\n",
        f"patches/{USER_PATCH}": "-- user patch\n",
        "version.log": f"2026-08-01, 10:00:00, {RELEASE}, Fake\n",
    }
    for relative, content in user_files.items():
        path = koreader / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    sdr = root / "Books" / "Alice.sdr"
    sdr.mkdir(parents=True)
    (root / "Books" / "Alice.epub").write_bytes(b"PK")
    (sdr / "metadata.epub.lua").write_text('return {\n    ["percent_finished"] = 0.42,\n}\n')
    return koreader


def _clock() -> Callable[[], datetime]:
    moments = iter(
        datetime(2026, 11, 26, 13, 0, tzinfo=UTC) + timedelta(minutes=i) for i in range(99)
    )
    return lambda: next(moments)


@pytest.fixture
def reader(tmp_path: Path) -> Path:
    return make_reader(tmp_path / "PB634K3", "pocketbook")


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(tmp_path / "data" / "snapshots" / "PBVerseProColor-PB634K3", _clock())


# -- fake store catalogue (B-07): invented entries in the index's schema 2, never real data --

FAKE_GENERATED_AT = "2026-11-26T12:55:30Z"


def _fake_plugin(
    owner: str, repo: str, stars: int, description: str, **extra: object
) -> dict[str, object]:
    return {
        "id": f"{owner}/{repo}",
        "owner": owner,
        "repo": repo,
        "url": f"https://github.com/{owner}/{repo}",
        "description": description,
        "purpose": "",
        "categories": extra.pop("categories", ["ui"]),
        "keywords": extra.pop("keywords", []),
        "topics": ["koreader"],
        "stars": stars,
        "license": extra.pop("license", "GPL-3.0"),
        "default_branch": "main",
        "activity": "active",
        "pushed_at": "2026-11-20T10:00:00Z",
        "archived": False,
        **extra,
    }


def fake_catalogue() -> tuple[bytes, bytes]:
    """``index.json`` and ``patches.json`` bytes with a handful of made-up entries."""
    plugins = [
        _fake_plugin("alice", f"{THIRD_PARTY_PLUGIN}.koplugin", 900, "Cover grid for the library"),
        _fake_plugin(
            "bob", "readingclock.koplugin", 40, "Shows a clock while reading", keywords=["time"]
        ),
        _fake_plugin(
            "carol", "dictsync", 120, "Sync dictionary lookups", categories=["dict", "sync"]
        ),
        _fake_plugin("mallory", "evil", 5000, "Not on GitHub", url="https://evil.example/m/evil"),
        _fake_plugin("dave", "nolicence.koplugin", 3, "No licence given", license=None),
    ]
    patches = [
        {
            "id": f"erin/kopatches:{USER_PATCH}",
            "owner": "erin",
            "repo": "kopatches",
            "path": USER_PATCH,
            "url": f"https://github.com/erin/kopatches/blob/main/{USER_PATCH}",
            "raw_url": f"https://raw.githubusercontent.com/erin/kopatches/main/{USER_PATCH}",
            "purpose": "Tweaks the reader footer",
            "categories": ["ui"],
            "keywords": ["footer"],
            "repo_stars": 60,
            "file_modified_at": "2026-10-25T14:47:16Z",
            "archived": False,
            "activity": "active",
        },
        {
            "id": "erin/kopatches:notlua.txt",
            "owner": "erin",
            "repo": "kopatches",
            "path": "notlua.txt",
            "url": "https://github.com/erin/kopatches/blob/main/notlua.txt",
            "raw_url": "https://raw.githubusercontent.com/erin/kopatches/main/notlua.txt",
        },
    ]
    index = {
        "schema": 2,
        "generated_at": FAKE_GENERATED_AT,
        "categories": [
            {"id": "ui", "label": "Interface"},
            {"id": "dict", "label": "Dictionary & language"},
            {"id": "sync", "label": "Sync"},
        ],
        "plugins": plugins,
        "patches_url": "patches.json",
    }
    return json.dumps(index).encode(), json.dumps({"schema": 2, "patches": patches}).encode()


def write_fake_catalogue(cache_dir: Path) -> None:
    """Seed the catalogue cache (``kodesk --offline`` then shows it without any network)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    index, patches = fake_catalogue()
    (cache_dir / "index.json").write_bytes(index)
    (cache_dir / "patches.json").write_bytes(patches)


# -- fake GitHub for installs (B-08): invented repos and archives, never the network --


def plugin_zip(files: dict[str, bytes], links: tuple[str, ...] = ()) -> bytes:
    """A zip with ``files``; ``links`` are stored as symbolic links (a hostile archive)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def plugin_files(folder: str, version: str = "1.0") -> dict[str, bytes]:
    return {
        f"{folder}/main.lua": f"-- main {version}\n".encode(),
        f"{folder}/_meta.lua": f'return {{ name = "x", version = "{version}" }}\n'.encode(),
        f"{folder}/lib/util.lua": f"-- util {version}\n".encode(),
    }


class FakeGitHub:
    """Answers like api.github.com, codeload and the asset host for the given URLs."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], limit: int) -> Response:
        self.requests.append(url)
        if url not in self.files:
            raise NetError(f"{url}: HTTP 404", 404)
        body = self.files[url]
        if len(body) > limit:
            raise NetError(f"{url}: too large")
        return Response(200, None, body)

    def release(self, owner: str, repo: str, tag: str, assets: dict[str, bytes]) -> None:
        """A latest release whose assets carry GitHub's SHA-256 digest."""
        listed = []
        for name, data in assets.items():
            url = f"https://github.com/{owner}/{repo}/releases/download/{tag}/{name}"
            self.files[url] = data
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            listed.append({"name": name, "browser_download_url": url, "digest": digest})
        body = {"tag_name": tag, "draft": False, "prerelease": False, "assets": listed}
        api = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        self.files[api] = json.dumps(body).encode()

    def koreader(
        self, tag: str, archive: Path, platform: str = "pocketbook", latest: bool = True
    ) -> None:
        """A KOReader release (api.github.com/repos/koreader/koreader) with one archive."""
        name = f"koreader-{platform}-{tag}.zip"
        url = f"https://github.com/koreader/koreader/releases/download/{tag}/{name}"
        data = archive.read_bytes()
        self.files[url] = data
        asset = {
            "name": name,
            "browser_download_url": url,
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        body = json.dumps(
            {"tag_name": tag, "draft": False, "prerelease": False, "assets": [asset]}
        ).encode()
        api = "https://api.github.com/repos/koreader/koreader/releases"
        self.files[f"{api}/tags/{tag}"] = body
        if latest:
            self.files[f"{api}/latest"] = body

    def save(self, url: str, target: Path, limit: int, cancelled: Callable[[], bool]) -> str:
        """Like :func:`kodesk.core.net.https_save`: the file and its SHA-256."""
        body = self(url, {}, limit)
        if cancelled():
            raise CancelledError("download cancelled")
        with target.open("xb") as out:
            out.write(body.body)
        return hashlib.sha256(body.body).hexdigest()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never hit the network (CLAUDE.md): any real request fails the test loudly."""

    def refuse(_self: object, request: object, *_args: object, **_kwargs: object) -> None:
        url = getattr(request, "full_url", request)
        raise AssertionError(f"a test tried to reach the network: {url}")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


@pytest.fixture(autouse=True)
def no_blocking_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message box a test did not patch would wait for a click forever and hang the whole
    run: fail the test at once instead, naming the dialog. Tests patch what they expect."""

    def refuse(*args: object, **_kwargs: object) -> None:
        texts = [a for a in args if isinstance(a, str)]
        raise AssertionError(f"a test opened a blocking message box: {texts}")

    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, name, refuse)


@pytest.fixture(autouse=True)
def close_windows() -> Iterator[None]:
    """After each test, close every window it left open: a leftover modal dialog would block
    the next test's dialogs and the reader watcher (which waits while a dialog is open)."""
    yield
    app = QApplication.instance()
    if isinstance(app, QApplication):
        for widget in app.topLevelWidgets():
            widget.close()
