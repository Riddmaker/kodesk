"""Extensions that unlock Garage settings: kodesk's fixed, curated list (B-23, D-21); no Qt.

Some things KOReader cannot do by itself, a plugin can. The Garage shows such a place as locked
with the extensions that unlock it; once one of them is installed, the place works normally.
Only extensions on this list are ever edited: an unknown plugin's settings are never guessed
(hard rule: plugin settings are sacred). New entries come from users (B-24) and are added here
by hand after checking the extension's code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlencode

from kodesk.core.catalogue import Entry, Kind
from kodesk.core.luadump import KeyPath

NOT_SUPPORTED = "Not adjustable in kodesk — set it on the reader as usual"
SUPPORTED = "Adjustable in the Garage"
# B-24: GitHub's issue form (.github/ISSUE_TEMPLATE/gui-support.yml); fields are prefilled by id.
ISSUE_FORM = "https://github.com/Riddmaker/kodesk/issues/new"
ISSUE_TEMPLATE = "gui-support.yml"


class Area(StrEnum):
    """A place in the Garage that an extension can unlock."""

    HOME_KEY = "Home button"
    ICONS = "Icons"
    LIBRARY = "Library"


# What "You may find more for this in the Store" searches for, per area (one word: the Store
# needs every word to match).
AREA_SEARCH = {Area.HOME_KEY: "button", Area.ICONS: "icon", Area.LIBRARY: "library"}


@dataclass(frozen=True)
class Extension:
    """One supported extension: where it comes from and which settings keys it owns."""

    plugin: str  # folder name without .koplugin, as on the reader
    title: str
    owner: str  # github.com/<owner>/<repo>
    repo: str
    area: Area
    keys: tuple[KeyPath, ...] = ()  # settings.reader.lua keys kodesk may write for it
    unlocks: str = ""  # one line: what it adds in the Garage

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"


EXTENSIONS = (
    # pbfixes.koplugin 0.4.1 (main.lua): HOME_SETTING = "pbfixes_home_back", read with
    # G_reader_settings:nilOrTrue (absent = on).
    Extension(
        "pbfixes",
        "pbfixes",
        "glaucon1984",
        "pbfixes.koplugin",
        Area.HOME_KEY,
        (("pbfixes_home_back",),),
        "Home acts as Back (closes menus, goes up a folder)",
    ),
    # iconschanger.koplugin: copies the chosen pack into <data dir>/icons/, which KOReader
    # prefers over its own icons (iconwidget.lua:21). kodesk only draws them; packs are chosen
    # on the reader (no own icon packs, B-23 "not in").
    Extension(
        "iconschanger",
        "Icon Pack Changer",
        "ebanDev",
        "iconschanger.koplugin",
        Area.ICONS,
        (),
        "Other icon packs, chosen on the reader (Tools → More tools → Icon Pack Changer)",
    ),
    # Project: Title draws the library from its own cache, settings/PT_bookinfo_cache.sqlite3
    # (projecttitle.koplugin/main.lua:147); its display mode is B-21.
    Extension(
        "projecttitle",
        "Project: Title",
        "joshuacant",
        "ProjectTitle",
        Area.LIBRARY,
        (),
        "Another look for the library, with covers",
    ),
)


def for_area(area: Area) -> tuple[Extension, ...]:
    """The extensions that unlock ``area``, in list order."""
    return tuple(e for e in EXTENSIONS if e.area is area)


def installed_for(area: Area, plugins: tuple[str, ...] | frozenset[str]) -> Extension | None:
    """The first extension for ``area`` that is installed (plugin folder names)."""
    names = {p.lower() for p in plugins}
    return next((e for e in for_area(area) if e.plugin.lower() in names), None)


def supported(plugin: str) -> Extension | None:
    """The list entry for an installed plugin folder name, if kodesk supports it."""
    return next((e for e in EXTENSIONS if e.plugin.lower() == plugin.lower()), None)


def for_repo(owner: str, repo: str) -> Extension | None:
    """The list entry for a Store entry's GitHub repo (case-insensitive), if any."""
    key = (owner.lower(), repo.lower().removesuffix(".koplugin"))
    return next(
        (
            e
            for e in EXTENSIONS
            if (e.owner.lower(), e.repo.lower().removesuffix(".koplugin")) == key
        ),
        None,
    )


def gui_support_url(entry: Entry) -> str:
    """B-24: the prefilled issue form asking for ``entry`` in the Garage. Only what the Store
    shows anyway (name, kind, source, version); nothing about the user or the reader."""
    changed = entry.pushed_at[:10] or "unknown"
    if entry.kind is Kind.PATCH:
        version = f"file {entry.file_sha[:12] or 'unknown'}, last change {changed}"
    else:
        version = f"{entry.branch or 'default'} branch, last change {changed}"
    fields = {
        "template": ISSUE_TEMPLATE,
        "title": f"GUI support: {entry.name}",
        "extension": entry.name,
        "kind": entry.kind.value,
        "source": entry.url,
        "version": version,
    }
    return f"{ISSUE_FORM}?{urlencode(fields)}"
