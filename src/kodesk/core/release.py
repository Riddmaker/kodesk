"""KOReader's stable releases on GitHub: which one is latest, and fetching it safely (B-06).

Source: GitHub releases only (owner decision A, 2026-11-26). ``releases/latest`` skips drafts
and pre-releases, so it is the latest stable release. The asset is found by listing, with
exactly the name ``koreader-<platform>-<tag>.zip``; GitHub's SHA-256 ``digest`` of the asset is
required, and the download must match it (KOReader publishes no other checksum or signature).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kodesk.core import koversion
from kodesk.core.net import Get, NetError, Save

API = "https://api.github.com/repos/koreader/koreader/releases"
DOWNLOADS = "https://github.com/koreader/koreader/releases/download/"
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
MAX_API_ANSWER = 2 * 2**20
MAX_RELEASE = 300 * 2**20  # the PocketBook zip is 41 MB (v2026.07.1)
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


@dataclass(frozen=True)
class Release:
    """One release archive for one platform, with the checksum GitHub publishes for it."""

    tag: str  # e.g. "v2026.07.1"
    name: str  # e.g. "koreader-pocketbook-v2026.07.1.zip"
    url: str
    sha256: str
    size: int

    @property
    def megabytes(self) -> int:
        return max(1, round(self.size / 2**20))


def _parse(body: bytes, platform: str) -> Release:
    try:
        data = json.loads(body)
    except ValueError as error:
        raise NetError("GitHub sent an unreadable answer about KOReader releases") from error
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str) or not koversion.is_stable(tag):
        raise NetError(f"GitHub named no usable KOReader release ({str(tag)[:40]!r})")
    if data.get("draft") or data.get("prerelease"):
        raise NetError(f"KOReader {tag} is not a stable release")
    wanted = f"koreader-{platform}-{tag}.zip"
    assets = data.get("assets")
    found = [
        a
        for a in (assets if isinstance(assets, list) else [])
        if isinstance(a, dict) and a.get("name") == wanted
    ]
    if len(found) != 1:
        raise NetError(f"KOReader {tag} has no {wanted}")
    asset = found[0]
    url, digest, size = asset.get("browser_download_url"), asset.get("digest"), asset.get("size")
    if not isinstance(url, str) or url != f"{DOWNLOADS}{tag}/{wanted}":
        raise NetError(f"unexpected download address for {wanted}")
    match = _DIGEST.match(digest) if isinstance(digest, str) else None
    if match is None:
        raise NetError(f"GitHub publishes no SHA-256 for {wanted}, so kodesk does not use it")
    if not isinstance(size, int) or not 0 < size <= MAX_RELEASE:
        raise NetError(f"{wanted}: unexpected size {size!r}")
    return Release(tag, wanted, url, match.group(1), size)


def latest(platform: str, get: Get) -> Release:
    """The latest stable release's archive for ``platform`` (e.g. ``pocketbook``)."""
    return _parse(get(f"{API}/latest", API_HEADERS, MAX_API_ANSWER).body, platform)


def for_tag(tag: str, platform: str, get: Get) -> Release | None:
    """The release archive of an installed version, or None if GitHub has no such release
    (e.g. v2026.07.2: a tag without a release) or the version is a nightly."""
    if not koversion.is_stable(tag):
        return None
    try:
        body = get(f"{API}/tags/{tag}", API_HEADERS, MAX_API_ANSWER).body
    except NetError as error:
        if error.status == 404:
            return None
        raise
    return _parse(body, platform)


def fetch(release: Release, save: Save, folder: Path, cancelled: Callable[[], bool]) -> Path:
    """Download into ``folder`` and check it against GitHub's SHA-256; the file's path."""
    target = folder / release.name
    sha = save(release.url, target, MAX_RELEASE, cancelled)
    if sha != release.sha256:
        target.unlink(missing_ok=True)
        raise NetError(
            f"{release.name} does not match the SHA-256 GitHub publishes for it; "
            "nothing was installed"
        )
    return target
