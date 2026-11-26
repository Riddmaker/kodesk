"""Where a plugin or patch from the index is downloaded from, and checking what arrives (B-08).

The index has no versions, download links or archive hashes (research: plugin-sources), so
kodesk asks GitHub (owner decision A, 2026-11-26):

1. the latest release (``/releases/latest`` skips drafts and pre-releases): its one ``.zip``
   asset, checked against the SHA-256 GitHub publishes for it; without exactly one such asset
   the tag's source archive from codeload;
2. no release: the default branch at its current commit, so the version shown is exact.

That is one or two API requests per plugin (GitHub allows 60 per hour without an account, owner
decision D: no token). Patches are single files: the raw file, compared with the git blob SHA-1
in the index; a newer file is allowed after the code preview, with a note (owner decision B).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote

from kodesk.core.catalogue import Entry, Kind
from kodesk.core.net import Get, NetError, check_url, https_get

API = "https://api.github.com/repos/{owner}/{repo}"
CODELOAD = "https://codeload.github.com/{owner}/{repo}/zip/{ref}"
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
MAX_API_BYTES = 2 * 2**20
MAX_ARCHIVE_BYTES = 64 * 2**20
MAX_PATCH_BYTES = 2 * 2**20
_REF = re.compile(r"^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Download:
    """One exact version of a plugin: where it comes from and how it is checked."""

    owner: str
    repo: str
    url: str
    tag: str | None = None  # release tag, if installed from a release
    commit: str | None = None  # branch head, if there is no release
    sha256: str | None = None  # published by GitHub for release assets

    @property
    def version(self) -> str:
        """``v1.15.0`` or ``main @ 2a9577b`` (no release)."""
        return self.tag or f"no release, commit {(self.commit or '?')[:7]}"

    @property
    def check(self) -> str:
        """How the download is verified, for the preview."""
        if self.sha256:
            return "checked against the SHA-256 GitHub publishes for the release file"
        return "GitHub publishes no checksum for this archive; it is fetched over HTTPS only"


def _api_json(url: str, get: Get) -> object:
    answer = get(url, API_HEADERS, MAX_API_BYTES)
    try:
        return json.loads(answer.body)
    except (ValueError, UnicodeDecodeError) as error:
        raise NetError(f"{url}: GitHub's answer is not valid JSON") from error


def _ref(value: object) -> str | None:
    return value if isinstance(value, str) and _REF.match(value) and ".." not in value else None


def _asset(release: dict[str, object], owner: str, repo: str) -> tuple[str, str] | None:
    """The release's single ``.zip`` asset with a SHA-256 digest: (url, sha256)."""
    found = []
    prefix = f"https://github.com/{owner}/{repo}/releases/download/"
    assets = release.get("assets")
    for asset in assets if isinstance(assets, list) else ():
        if not isinstance(asset, dict) or not str(asset.get("name", "")).endswith(".zip"):
            continue
        url, digest = str(asset.get("browser_download_url", "")), str(asset.get("digest") or "")
        sha = digest.removeprefix("sha256:")
        if url.startswith(prefix) and digest.startswith("sha256:") and _SHA256.match(sha):
            found.append((check_url(url), sha))
    return found[0] if len(found) == 1 else None


def latest(entry: Entry, get: Get = https_get) -> Download:
    """The version kodesk would install now for the plugin ``entry``."""
    if entry.kind is not Kind.PLUGIN:
        raise ValueError("patches are single files, see fetch_patch")
    owner, repo = entry.owner, entry.repo
    base = API.format(owner=owner, repo=repo)
    try:
        release = _api_json(f"{base}/releases/latest", get)
    except NetError as error:
        if error.status != 404:
            raise
        release = None  # the repo has no (non-draft, non-pre-) release
    if isinstance(release, dict) and (tag := _ref(release.get("tag_name"))):
        asset = _asset(release, owner, repo)
        if asset:
            return Download(owner, repo, asset[0], tag=tag, sha256=asset[1])
        url = CODELOAD.format(owner=owner, repo=repo, ref="refs/tags/" + quote(tag))
        return Download(owner, repo, url, tag=tag)
    if not entry.branch:
        raise NetError(f"{owner}/{repo} has no release and no usable default branch")
    answer = get(
        f"{base}/commits/{quote(entry.branch)}",
        {**API_HEADERS, "Accept": "application/vnd.github.sha"},
        MAX_API_BYTES,
    )
    commit = answer.body.decode("ascii", errors="replace").strip()
    if not _COMMIT.match(commit):
        raise NetError(f"{owner}/{repo}: GitHub did not name the current commit")
    return Download(owner, repo, CODELOAD.format(owner=owner, repo=repo, ref=commit), commit=commit)


def fetch(download: Download, get: Get = https_get) -> bytes:
    """The archive's bytes; a SHA-256 mismatch discards it."""
    data = get(download.url, {}, MAX_ARCHIVE_BYTES).body
    if download.sha256 and hashlib.sha256(data).hexdigest() != download.sha256:
        raise NetError(
            f"{download.owner}/{download.repo} {download.version}: the download does not match "
            "the checksum GitHub publishes, so it was discarded"
        )
    return data


def git_blob_sha1(data: bytes) -> str:
    """Git's object id of a file, as the index's ``file_sha`` names it."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class PatchFile:
    data: bytes
    matches_index: bool | None  # None: the index has no file_sha for it


def fetch_patch(entry: Entry, get: Get = https_get) -> PatchFile:
    """The patch file as it is now in its repo, and whether it is what the index saw."""
    if entry.kind is not Kind.PATCH or entry.text_url is None:
        raise ValueError("not a patch with a download address")
    data = get(entry.text_url, {}, MAX_PATCH_BYTES).body
    if not entry.file_sha:
        return PatchFile(data, None)
    return PatchFile(data, git_blob_sha1(data) == entry.file_sha)


def probable_sources(entries: Iterable[Entry], kind: Kind, name: str) -> tuple[Entry, ...]:
    """Index entries whose name matches an installed plugin folder or patch file."""
    if kind is Kind.PLUGIN:
        return tuple(e for e in entries if e.kind is kind and e.name.lower() == name.lower())
    return tuple(e for e in entries if e.kind is kind and e.name == name)


def is_newer(installed_tag: str | None, installed_commit: str | None, download: Download) -> bool:
    """Whether ``download`` differs from the recorded version (tags and commits are compared as
    names; a repo that moves from commits to releases counts as newer)."""
    if download.tag:
        return download.tag != installed_tag
    return download.commit != installed_commit
