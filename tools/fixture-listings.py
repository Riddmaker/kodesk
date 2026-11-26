#!/usr/bin/env python3
"""Refresh tests/fixtures/listings/ from real KOReader release archives (B-05).

Downloads each archive over HTTPS, checks it against the SHA-256 digest GitHub publishes for the
asset, and writes only its member names (one per line, sorted). Nothing is extracted. The tests
build fake reader trees from these listings, so the archives themselves are never committed.

Usage: tools/fixture-listings.py [TAG]   (default: the tag below)
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

TAG = "v2026.07.1"
PLATFORMS = ("pocketbook", "kobo", "kindle")
API = "https://api.github.com/repos/koreader/koreader/releases/tags/{tag}"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "listings"
CHUNK = 1 << 20


def _get(url: str) -> urllib.request.Request:
    """A request for an https URL only (hence the S310 waivers below)."""
    if not url.startswith("https://"):
        raise SystemExit(f"refusing non-TLS URL: {url}")
    headers = {"User-Agent": "kodesk-fixture-listings"}
    return urllib.request.Request(url, headers=headers)  # noqa: S310


def _assets(tag: str) -> dict[str, dict[str, object]]:
    with urllib.request.urlopen(_get(API.format(tag=tag)), timeout=30) as response:  # noqa: S310
        release = json.load(response)
    return {asset["name"]: asset for asset in release["assets"]}


def _download(asset: dict[str, object], target: Path) -> None:
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise SystemExit(f"{asset['name']}: GitHub publishes no SHA-256 digest, not using it")
    sha = hashlib.sha256()
    request = _get(str(asset["browser_download_url"]))
    with (
        urllib.request.urlopen(request, timeout=120) as response,  # noqa: S310
        target.open("wb") as out,
    ):
        while chunk := response.read(CHUNK):
            sha.update(chunk)
            out.write(chunk)
    if sha.hexdigest() != digest.removeprefix("sha256:"):
        raise SystemExit(f"{asset['name']}: SHA-256 mismatch, download discarded")


def main() -> None:
    """Write one listing per platform."""
    tag = sys.argv[1] if len(sys.argv) > 1 else TAG
    assets = _assets(tag)
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for platform in PLATFORMS:
            name = f"koreader-{platform}-{tag}.zip"
            if name not in assets:
                raise SystemExit(f"{name} is not an asset of {tag}")
            archive = Path(tmp) / name
            _download(assets[name], archive)
            with zipfile.ZipFile(archive) as zf:
                members = sorted(info.filename for info in zf.infolist() if not info.is_dir())
            listing = OUT / f"koreader-{platform}-{tag}.txt"
            listing.write_text("\n".join(members) + "\n", encoding="utf-8")
            archive.unlink()
            print(f"{listing.relative_to(OUT.parent.parent.parent)}: {len(members)} files")


if __name__ == "__main__":
    main()
