"""HTTPS downloads for the store and installs (B-07, B-08). Everything fetched is untrusted.

One rule set for every request kodesk makes: verified TLS, a fixed list of GitHub hosts (the
redirects of a download are checked against the same list), a generic User-Agent without
personal data, a timeout and a size cap. Callers check what they receive (JSON shape, hashes).
"""

from __future__ import annotations

import hashlib
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kodesk import __version__

ALLOWED_HOSTS = frozenset(
    {
        "omer-faruq.github.io",  # the plugin index (GitHub Pages)
        "raw.githubusercontent.com",  # READMEs and patch files
        "api.github.com",  # latest release, branch head
        "codeload.github.com",  # tag and commit archives
        "github.com",  # release asset links (redirect to the host below)
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",  # older name of the asset host
    }
)
TIMEOUT = 20
CHUNK = 1 << 20
USER_AGENT = f"kodesk/{__version__} (+https://github.com/Riddmaker/kodesk)"


class NetError(Exception):
    """A request was refused, failed, or returned something kodesk does not accept."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Response:
    status: int  # 200 or 304
    etag: str | None
    body: bytes


Get = Callable[[str, Mapping[str, str], int], Response]
# Save url to a file, at most ``limit`` bytes; returns the SHA-256 of what was written.
# The last argument is asked between chunks: True cancels the download.
Save = Callable[[str, Path, int, Callable[[], bool]], str]


class CancelledError(Exception):
    """The user cancelled a download."""


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
        raise NetError(f"refusing to load {url!r}: only HTTPS from {sorted(ALLOWED_HOSTS)}")
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


def _http_error(url: str, error: urllib.error.HTTPError) -> NetError:
    headers = error.headers
    if error.code in (403, 429) and headers.get("X-RateLimit-Remaining") == "0":
        reset = headers.get("X-RateLimit-Reset", "")
        when = (
            datetime.fromtimestamp(int(reset)).strftime(" after %H:%M") if reset.isdigit() else ""
        )
        return NetError(
            f"GitHub allows 60 requests per hour without an account; try again{when}.",
            error.code,
        )
    return NetError(f"{url}: HTTP {error.code}", error.code)


def _open(url: str, headers: Mapping[str, str]) -> Any:
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), _CheckedRedirects()
    )
    headers = {"User-Agent": USER_AGENT, **headers}
    request = urllib.request.Request(check_url(url), headers=headers)  # noqa: S310  # https only
    return opener.open(request, timeout=TIMEOUT)


def https_get(url: str, headers: Mapping[str, str], limit: int) -> Response:
    """GET ``url`` over verified TLS; 304 is a valid answer; at most ``limit`` bytes."""
    try:
        with _open(url, headers) as answer:
            body = answer.read(limit + 1)
            etag = answer.headers.get("ETag")
            status = answer.status
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return Response(304, error.headers.get("ETag"), b"")
        raise _http_error(url, error) from error
    except (urllib.error.URLError, OSError) as error:
        raise NetError(f"{url}: {getattr(error, 'reason', error)}") from error
    if len(body) > limit:
        raise NetError(f"{url}: larger than {max(1, limit // 2**20)} MB")
    return Response(status, etag, body)


def https_save(url: str, target: Path, limit: int, cancelled: Callable[[], bool]) -> str:
    """Stream ``url`` into the new file ``target`` (at most ``limit`` bytes); its SHA-256.

    A large download (a KOReader release, ~40 MB) never sits in memory. ``target`` must not
    exist; on any problem, and on cancel, the partial file is removed.
    """
    sha = hashlib.sha256()
    written = 0
    with target.open("xb") as out:  # only a file created here is ever removed below
        try:
            with _open(url, {}) as answer:
                while chunk := answer.read(CHUNK):
                    if cancelled():
                        raise CancelledError("download cancelled")
                    written += len(chunk)
                    if written > limit:
                        raise NetError(f"{url}: larger than {max(1, limit // 2**20)} MB")
                    sha.update(chunk)
                    out.write(chunk)
        except BaseException as error:
            out.close()
            target.unlink(missing_ok=True)
            if isinstance(error, urllib.error.HTTPError):
                raise _http_error(url, error) from error
            if isinstance(error, (urllib.error.URLError, OSError)):
                raise NetError(f"{url}: {getattr(error, 'reason', error)}") from error
            raise
    return sha.hexdigest()
