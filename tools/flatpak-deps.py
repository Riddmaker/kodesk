#!/usr/bin/env python3
"""Refresh data/flatpak/python-deps.json: kodesk's Python dependencies as a Flatpak module.

flatpak-builder works offline, so every wheel is listed with its PyPI URL and SHA-256 (HABIT 1:
no unverified downloads; flatpak-builder checks each digest). The pins come from
data/flatpak/requirements.in, resolved by uv for Linux; only binary wheels for the
runtime's architectures are taken (manylinux, x86_64 and aarch64) and, for packages built per
Python version, the versions in PYTHONS — pip in the build picks the one that fits.

Usage: tools/flatpak-deps.py   (needs network and uv; run it after changing requirements.in)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data/flatpak/requirements.in"
OUT = ROOT / "data/flatpak/python-deps.json"
ARCHES = {"x86_64": "x86_64", "aarch64": "aarch64"}  # wheel platform tag → Flatpak arch
PYTHONS = ("cp312", "cp313", "cp314")  # KDE runtime 6.x ships one of these
PYPI = "https://pypi.org/pypi/{name}/{version}/json"


def pins() -> list[tuple[str, str]]:
    """name==version for every package, resolved for Linux."""
    result = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [
            shutil.which("uv") or "uv",
            "pip",
            "compile",
            "--quiet",
            "--no-header",
            "--no-annotate",
            "--python-platform",
            "x86_64-manylinux_2_28",
            "--python-version",
            "3.13",
            str(SOURCE),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    found = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", result.stdout, re.MULTILINE)
    return list(found)


def files(name: str, version: str) -> list[dict[str, object]]:
    """The wheel files of one release; checks every URL is https on PyPI's file host."""
    url = PYPI.format(name=name, version=version)
    with urllib.request.urlopen(url, timeout=30) as reply:  # noqa: S310 - fixed https URL
        data = json.load(reply)
    sources: list[dict[str, object]] = []
    best: dict[tuple[str, str], tuple[tuple[int, int], dict[str, object]]] = {}
    for item in data["urls"]:
        filename, url = item["filename"], item["url"]
        if item["packagetype"] != "bdist_wheel" or item.get("yanked"):
            continue
        if not url.startswith("https://files.pythonhosted.org/"):
            raise SystemExit(f"unexpected download host: {url}")
        python, abi, platform = filename[:-4].split("-")[-3:]
        if platform == "any":
            sources.append({"type": "file", "url": url, "sha256": item["digests"]["sha256"]})
            continue
        if "manylinux" not in platform:
            continue
        if abi != "abi3" and python not in PYTHONS:
            continue
        glibc = re.search(r"manylinux_(\d+)_(\d+)", platform)
        oldest = (int(glibc[1]), int(glibc[2])) if glibc else (2, 17)  # manylinux2014 = 2.17
        for tag, arch in ARCHES.items():
            key = (arch, python if abi != "abi3" else "abi3")
            if platform.endswith(tag) and (key not in best or oldest < best[key][0]):
                source = {
                    "type": "file",
                    "url": url,
                    "sha256": item["digests"]["sha256"],
                    "only-arches": [arch],
                }
                best[key] = (oldest, source)
    sources += [source for _glibc, source in best.values()]
    if not sources:
        raise SystemExit(f"no usable wheel for {name}=={version}")
    return sources


def main() -> int:
    """Resolve, collect the wheels and write the module."""
    packages = pins()
    sources = [source for name, version in packages for source in files(name, version)]
    names = " ".join(f'"{name}=={version}"' for name, version in packages)
    module = {
        "name": "python-deps",
        "buildsystem": "simple",
        "build-commands": [
            'pip3 install --verbose --exists-action=i --no-index --find-links="file://${PWD}" '
            f"--prefix=${{FLATPAK_DEST}} --no-build-isolation --only-binary=:all: {names}"
        ],
        "sources": sources,
    }
    OUT.write_text(json.dumps(module, indent=2) + "\n", encoding="utf-8")  # JSON: no comments
    print(f"{OUT.relative_to(ROOT)}: {len(packages)} packages, {len(sources)} wheels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
