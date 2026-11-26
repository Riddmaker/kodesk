"""The store catalogue: the community plugin and patch index, fetched at runtime (B-07, D-08).

The index (``omer-faruq/koreader-plugin-index``) is published as JSON on GitHub Pages. It has
no licence yet, so kodesk never ships or vendors it: it is fetched when the user opens the
store, cached on the PC with its ETag (a revalidation costs a 304), and credited on screen.

Everything from the network is untrusted data: HTTPS only, a fixed host list (redirects too),
a size cap, and every entry is validated; entries whose repo is not on github.com are dropped.
Nothing here installs anything (B-08).
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kodesk import __version__

INDEX_BASE = "https://omer-faruq.github.io/koreader-plugin-index/"
INDEX_URL = INDEX_BASE + "index.json"
PATCHES_URL = INDEX_BASE + "patches.json"
SOURCE_NAME = "omer-faruq/koreader-plugin-index"
SOURCE_URL = "https://github.com/omer-faruq/koreader-plugin-index"
ALLOWED_HOSTS = frozenset({"omer-faruq.github.io", "raw.githubusercontent.com"})
MAX_INDEX_BYTES = 16 * 2**20  # index.json is 2.5 MB today
MAX_TEXT_BYTES = 2 * 2**20  # one README or patch file
TIMEOUT = 20
MAX_FIELD = 2000  # characters kept of any text field
USER_AGENT = f"kodesk/{__version__} (+https://github.com/Riddmaker/kodesk)"
TEMP_SUFFIX = ".kodesk-tmp"

_NAME = r"[A-Za-z0-9_.-]+"
_REPO_URL = re.compile(rf"^https://github\.com/(?P<owner>{_NAME})/(?P<repo>{_NAME})$")
_BRANCH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


class CatalogueError(Exception):
    """The catalogue (or a README) could not be loaded or is not what kodesk expects."""


class Kind(StrEnum):
    PLUGIN = "plugin"
    PATCH = "patch"


@dataclass(frozen=True)
class Response:
    status: int  # 200 or 304
    etag: str | None
    body: bytes


Get = Callable[[str, Mapping[str, str], int], Response]


@dataclass(frozen=True)
class Entry:
    """One plugin repo or one patch file from the index."""

    kind: Kind
    id: str
    name: str  # plugin: repo name without ".koplugin"; patch: file name
    owner: str
    repo: str
    url: str  # https://github.com/owner/repo (patches: the file's page)
    description: str
    categories: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    stars: int = 0
    license: str | None = None
    activity: str = ""
    pushed_at: str = ""
    archived: bool = False
    text_url: str | None = None  # README (plugins) or the patch file itself, raw

    def matches(self, words: Iterable[str]) -> bool:
        haystack = " ".join((self.name, self.owner, self.repo, self.description, *self.keywords))
        haystack = haystack.lower()
        return all(word in haystack for word in words)


@dataclass(frozen=True)
class Catalogue:
    entries: tuple[Entry, ...]
    categories: Mapping[str, str]  # id -> label
    generated_at: str  # when the index was built (shown with the credit)
    offline: bool = False  # True: network failed, this is the cached copy


def check_url(url: str) -> str:
    """Only HTTPS to the allowed hosts, no credentials or odd ports in the URL."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise CatalogueError(f"refusing to load {url!r}: only HTTPS from {sorted(ALLOWED_HOSTS)}")
    return url


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def https_get(url: str, headers: Mapping[str, str], limit: int) -> Response:
    """GET ``url`` over verified TLS; 304 is a valid answer; at most ``limit`` bytes."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), _CheckedRedirects()
    )
    headers = {"User-Agent": USER_AGENT, **headers}
    request = urllib.request.Request(check_url(url), headers=headers)  # noqa: S310  # https only
    try:
        with opener.open(request, timeout=TIMEOUT) as answer:
            body = answer.read(limit + 1)
            etag = answer.headers.get("ETag")
            status = answer.status
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return Response(304, error.headers.get("ETag"), b"")
        raise CatalogueError(f"{url}: HTTP {error.code}") from error
    if len(body) > limit:
        raise CatalogueError(f"{url}: larger than {limit // 2**20} MB")
    return Response(status, etag, body)


def fetch_cached(url: str, cache_dir: Path, get: Get | None) -> tuple[bytes, bool]:
    """``url``'s body, revalidated by ETag; the cached copy if offline. Returns (body, offline).

    ``get=None`` never goes online (``kodesk --offline``).
    """
    name = url.rsplit("/", 1)[-1]
    cached, meta = cache_dir / name, cache_dir / f"{name}.etag"
    etag = meta.read_text(encoding="utf-8").strip() if meta.is_file() and cached.is_file() else ""
    try:
        if get is None:
            raise CatalogueError("offline mode")
        answer = get(url, {"If-None-Match": etag} if etag else {}, MAX_INDEX_BYTES)
    except (OSError, CatalogueError) as error:
        if cached.is_file():
            return cached.read_bytes(), True
        raise CatalogueError(f"Could not load the catalogue ({error}).") from error
    if answer.status == 304 and cached.is_file():
        return cached.read_bytes(), False
    cache_dir.mkdir(parents=True, exist_ok=True)
    for target, data in ((cached, answer.body), (meta, (answer.etag or "").encode())):
        temp = target.with_name(target.name + TEMP_SUFFIX)
        temp.write_bytes(data)
        temp.replace(target)
    return answer.body, False


# -- parsing ------------------------------------------------------------------------------


def _text(value: object) -> str:
    return value.strip()[:MAX_FIELD] if isinstance(value, str) else ""


def _words(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(_text(v) for v in value if isinstance(v, str) and v.strip())


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _load_json(data: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError) as error:
        raise CatalogueError(f"the catalogue is not valid JSON ({error})") from error
    if not isinstance(parsed, dict):
        raise CatalogueError("the catalogue has an unexpected format")
    return parsed


def _plugin(raw: object) -> Entry | None:
    if not isinstance(raw, dict):
        return None
    match = _REPO_URL.match(_text(raw.get("url")))
    if not match:
        return None
    owner, repo = match["owner"], match["repo"]
    branch = _text(raw.get("default_branch"))
    readme = None
    if _BRANCH.match(branch) and ".." not in branch:
        readme = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
    return Entry(
        kind=Kind.PLUGIN,
        id=f"{owner}/{repo}",
        name=repo.removesuffix(".koplugin"),
        owner=owner,
        repo=repo,
        url=f"https://github.com/{owner}/{repo}",
        description=_text(raw.get("description")) or _text(raw.get("purpose")),
        categories=_words(raw.get("categories")),
        keywords=_words(raw.get("keywords")) + _words(raw.get("topics")),
        stars=_count(raw.get("stars")),
        license=_text(raw.get("license")) or None,
        activity=_text(raw.get("activity")),
        pushed_at=_text(raw.get("pushed_at")),
        archived=raw.get("archived") is True,
        text_url=readme,
    )


def _patch(raw: object) -> Entry | None:
    if not isinstance(raw, dict):
        return None
    owner, repo, path = _text(raw.get("owner")), _text(raw.get("repo")), _text(raw.get("path"))
    if not _REPO_URL.match(f"https://github.com/{owner}/{repo}"):
        return None
    name = path.rsplit("/", 1)[-1]
    url = _text(raw.get("url"))
    if not name.endswith(".lua") or not url.startswith(f"https://github.com/{owner}/{repo}/"):
        return None
    try:
        text_url: str | None = check_url(_text(raw.get("raw_url")))
    except CatalogueError:
        text_url = None
    return Entry(
        kind=Kind.PATCH,
        id=f"{owner}/{repo}:{path}",
        name=name,
        owner=owner,
        repo=repo,
        url=url,
        description=_text(raw.get("purpose")),
        categories=_words(raw.get("categories")),
        keywords=_words(raw.get("keywords")),
        stars=_count(raw.get("repo_stars")),
        activity=_text(raw.get("activity")),
        pushed_at=_text(raw.get("file_modified_at")) or _text(raw.get("repo_pushed_at")),
        archived=raw.get("archived") is True,
        text_url=text_url,
    )


def parse(index: bytes, patches: bytes, offline: bool = False) -> Catalogue:
    """Both index files -> one catalogue, most-starred first; invalid entries are dropped."""
    plugins_data, patches_data = _load_json(index), _load_json(patches)
    raw_plugins, raw_patches = plugins_data.get("plugins"), patches_data.get("patches")
    if not isinstance(raw_plugins, list) or not isinstance(raw_patches, list):
        raise CatalogueError("the catalogue has an unexpected format")
    entries = [e for e in map(_plugin, raw_plugins) if e] + [
        e for e in map(_patch, raw_patches) if e
    ]
    categories = {}
    for category in plugins_data.get("categories") or ():
        if isinstance(category, dict) and _text(category.get("id")):
            categories[_text(category.get("id"))] = _text(category.get("label")) or category["id"]
    entries.sort(key=lambda e: (-e.stars, e.name.lower()))
    return Catalogue(
        tuple(entries), categories, _text(plugins_data.get("generated_at")), offline=offline
    )


def load(cache_dir: Path, get: Get | None = https_get) -> Catalogue:
    """Fetch (or revalidate) both index files and parse them."""
    index, offline = fetch_cached(INDEX_URL, cache_dir, get)
    # Offline after the first file: don't wait for a second timeout.
    patches, offline_patches = fetch_cached(PATCHES_URL, cache_dir, None if offline else get)
    return parse(index, patches, offline=offline or offline_patches)


README_NAMES = ("README.md", "readme.md", "Readme.md")


def fetch_text(entry: Entry, get: Get = https_get) -> str:
    """The plugin's README or the patch's code (not cached; one request per try)."""
    if entry.text_url is None:
        raise CatalogueError("no address for this entry in the catalogue")
    urls = [entry.text_url]
    if entry.kind is Kind.PLUGIN:
        base = entry.text_url.rsplit("/", 1)[0]
        urls = [f"{base}/{name}" for name in README_NAMES]
    for url in urls:
        try:
            answer = get(url, {}, MAX_TEXT_BYTES)
        except CatalogueError as error:
            if "HTTP 404" in str(error) and url != urls[-1]:
                continue
            raise
        return answer.body.decode("utf-8", errors="replace")
    raise CatalogueError("not found")  # unreachable: the last try returns or raises


def search(
    entries: Iterable[Entry],
    text: str = "",
    kind: Kind | None = None,
    category: str | None = None,
) -> list[Entry]:
    """Entries that contain every word of ``text`` and match the filters, order kept."""
    words = text.lower().split()
    return [
        e
        for e in entries
        if (kind is None or e.kind == kind)  # a QComboBox hands the StrEnum back as str
        and (category is None or category in e.categories)
        and e.matches(words)
    ]


def is_installed(entry: Entry, plugins: frozenset[str], patches: frozenset[str]) -> bool:
    """Plain name match: ``plugins`` are lower-case folder names without ``.koplugin``,
    ``patches`` file names. B-08 adds a proper repo mapping."""
    if entry.kind is Kind.PLUGIN:
        return entry.name.lower() in plugins
    return entry.name in patches


def built(catalogue: Catalogue) -> str:
    """The index build time for the credit line, e.g. '26 Nov 2026'."""
    try:
        when = datetime.fromisoformat(catalogue.generated_at.replace("Z", "+00:00"))
    except ValueError:
        return "unknown date"
    return when.astimezone(UTC).strftime("%d %b %Y")
