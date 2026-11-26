"""What kodesk installed on a reader, kept on the PC (B-08): source repo, version, file hashes.

KOReader itself records nothing about where a plugin came from. kodesk writes one JSON file per
reader beside its snapshots (``<snapshots>/<device id>/installs.json``), never on the reader.
A record exists for everything kodesk installed and for sources the user confirmed by hand
("Set source…"). The file hashes tell an update which files the old version shipped, so files
the plugin created itself are kept (owner decision C).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kodesk.core.archive import ArchiveError, safe_name
from kodesk.core.catalogue import Kind

FILE_NAME = "installs.json"
TEMP_SUFFIX = ".kodesk-tmp"
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RecordsError(Exception):
    """The record file exists but cannot be read."""


@dataclass(frozen=True)
class Record:
    """Where one installed plugin or patch came from, and the files it brought."""

    kind: Kind
    name: str  # plugin folder name without .koplugin, or the patch file name
    owner: str
    repo: str
    version: str = ""  # "v1.2.0", "no release, commit abc1234"; "" = not installed by kodesk
    tag: str | None = None
    commit: str | None = None
    path: str | None = None  # patches: the file's path in the repo
    files: Mapping[str, str] = field(default_factory=dict)  # path in the folder -> SHA-256
    installed_at: str = ""

    @property
    def source(self) -> str:
        return f"{self.owner}/{self.repo}"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _record(raw: object) -> Record | None:
    if not isinstance(raw, dict):
        return None
    try:
        kind = Kind(str(raw.get("kind")))
    except ValueError:
        return None
    texts = {key: raw.get(key) for key in ("name", "owner", "repo")}
    if not all(isinstance(v, str) and _NAME.match(v) for v in (texts["owner"], texts["repo"])):
        return None
    try:
        safe_name(str(texts["name"]))
    except ArchiveError:
        return None
    files = raw.get("files")
    hashes = {
        str(k): v
        for k, v in (files.items() if isinstance(files, dict) else ())
        if isinstance(v, str) and _SHA256.match(v)
    }
    return Record(
        kind=kind,
        name=str(texts["name"]),
        owner=str(texts["owner"]),
        repo=str(texts["repo"]),
        version=_text(raw.get("version")) or "",
        tag=_text(raw.get("tag")),
        commit=_text(raw.get("commit")),
        path=_text(raw.get("path")),
        files=hashes,
        installed_at=_text(raw.get("installed_at")) or "",
    )


class Records:
    """The records of one reader, loaded from and saved to ``base / installs.json``."""

    def __init__(self, base: Path) -> None:
        self.path = base / FILE_NAME
        self._items: dict[tuple[Kind, str], Record] = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise RecordsError(f"{self.path} cannot be read ({error})") from error
            for item in raw.get("records", []) if isinstance(raw, dict) else []:
                if record := _record(item):
                    self._items[(record.kind, record.name)] = record

    def all(self) -> tuple[Record, ...]:
        return tuple(self._items.values())

    def get(self, kind: Kind, name: str) -> Record | None:
        return self._items.get((kind, name))

    def put(self, record: Record) -> None:
        self._items[(record.kind, record.name)] = record
        self._save()

    def drop(self, kind: Kind, name: str) -> None:
        if self._items.pop((kind, name), None) is not None:
            self._save()

    def _save(self) -> None:
        records = [
            {**asdict(r), "kind": str(r.kind), "files": dict(r.files)}
            for _key, r in sorted(self._items.items())
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + TEMP_SUFFIX)
        temp.write_text(json.dumps({"records": records}, indent=1), encoding="utf-8")
        temp.replace(self.path)
