"""KOReader version strings, compared the way KOReader compares them (B-06).

Port of ``Version:getNormalizedVersion`` in ``frontend/version.lua`` (v2026.07.1)::

    rev:match("v(%d%d%d%d)%.(%d%d)%.?(%d?%d?)-?(%d*)")
    ((year * 100 + month) * 1000000 + point * 10000 + revision), commit

so v2026.07 < v2026.07.1 < v2026.07.2 < v2026.07.2-353-g25183e2 (a nightly 353 commits later).
"""

from __future__ import annotations

import re

_VERSION = re.compile(r"v(\d{4})\.(\d{2})\.?(\d{0,2})-?(\d*)")
_COMMIT = re.compile(r"-\d*-g([0-9a-fA-F]*)")
# A stable release tag as GitHub lists it (the only kind kodesk installs).
STABLE_TAG = re.compile(r"^v\d{4}\.\d{2}(?:\.\d{1,2})?$")


def normalized(rev: str | None) -> int:
    """The number KOReader compares, 0 for an unreadable string."""
    match = _VERSION.search(rev or "")
    if match is None:
        return 0
    year, month, point, revision = (int(part or 0) for part in match.groups())
    return (year * 100 + month) * 1_000_000 + point * 10_000 + revision


def commit(rev: str | None) -> str | None:
    """The short commit hash of a nightly (``v2026.07.2-353-g25183e2`` → ``25183e2``)."""
    match = _COMMIT.search(rev or "")
    return match.group(1) or None if match else None


def is_newer(candidate: str, installed: str | None) -> bool:
    """True if ``candidate`` is a later version than ``installed`` (unknown counts as older)."""
    return normalized(candidate) > normalized(installed)


def is_stable(rev: str | None) -> bool:
    """A plain release tag such as ``v2026.07.1`` (not a nightly)."""
    return bool(rev and STABLE_TAG.match(rev))
