"""Hot corners in an open book (B-22): tap and long press on the four corners; no Qt.

KOReader's Gestures plugin keeps every gesture in ``settings/gestures.lua``; the open book uses
the table ``gesture_reader`` (gestures.koplugin/main.lua:32, 248). On its first start the plugin
copies its defaults into that file (main.lua:236-240), so afterwards a missing entry means
"Pass through" (nothing), and one action is stored as ``{action_id = true}``
(dispatcher.lua _addItem). An entry with several actions or other values is shown as it is and
never touched unless the user picks something else for that corner (hard rule: gestures are
sacred; only the visibly changed entry differs, every other byte stays the same).

Checked against KOReader v2026.07.2 (plugins/gestures.koplugin, frontend/dispatcher.lua).
"""

from __future__ import annotations

from dataclasses import dataclass

from kodesk.core.luadump import KeyPath, LuaValue, SettingsDocument

GESTURES_FILE = "settings/gestures.lua"
SECTION = "gesture_reader"
CORNER_ZONE = 1 / 8  # DTAP_ZONE_TOP_LEFT etc. (defaults.lua:66-69): 1/8 of width and height
HOLD_IGNORED_KEY = "ignore_hold_corners"  # settings.reader.lua (gestures main.lua:245)


@dataclass(frozen=True)
class Corner:
    """One corner of the screen: KOReader's names for tapping and holding it."""

    id: str
    label: str
    tap: str
    hold: str
    right: bool
    bottom: bool


CORNERS = (  # gestures.koplugin/main.lua:57-73; KOReader spells the tap names this way
    Corner("top-left", "Top left", "tap_top_left_corner", "hold_top_left_corner", False, False),
    Corner("top-right", "Top right", "tap_top_right_corner", "hold_top_right_corner", True, False),
    Corner(
        "bottom-left",
        "Bottom left",
        "tap_left_bottom_corner",
        "hold_bottom_left_corner",
        False,
        True,
    ),
    Corner(
        "bottom-right",
        "Bottom right",
        "tap_right_bottom_corner",
        "hold_bottom_right_corner",
        True,
        True,
    ),
)

# The actions kodesk offers, with KOReader's own titles (dispatcher.lua settingsList).
ACTIONS = (
    ("toggle_frontlight", "Toggle frontlight"),
    ("show_frontlight_dialog", "Show frontlight dialog"),
    ("night_mode", "Toggle night mode"),
    ("toggle_bookmark", "Toggle bookmark"),
    ("bookmarks", "Bookmarks"),
    ("toc", "Table of contents"),
    ("go_to", "Go to page"),
    ("skim", "Skim document"),
    ("toggle_page_flipping", "Toggle page flipping"),
    ("toggle_status_bar", "Toggle status bar"),
    ("full_refresh", "Full screen refresh"),
    ("filemanager", "File browser"),
)
TITLES = dict(ACTIONS)
NOTHING = "Pass through"  # KOReader's label for "no action" (main.lua:328)

# gestures.koplugin/defaults.lua gesture_reader: what the reader starts with (frontlight only
# where the device has one; every device kodesk knows has).
DEFAULTS: dict[str, str] = {
    "tap_top_left_corner": "toggle_page_flipping",
    "tap_top_right_corner": "toggle_bookmark",
    "tap_left_bottom_corner": "toggle_frontlight",
}


def path(gesture: str) -> KeyPath:
    return (SECTION, gesture)


def action_of(value: LuaValue | None) -> str | None:
    """The offered action an entry holds, None for nothing; raises KeyError for anything else
    (several actions, an action kodesk does not offer, other values)."""
    if value is None:
        return None
    if isinstance(value, dict):
        actions = {k: v for k, v in value.items() if k != "settings"}
        if len(actions) == 1 and "settings" not in value:
            ((key, flag),) = actions.items()
            if isinstance(key, str) and key in TITLES and flag is True:
                return key
    raise KeyError(value)


def describe(value: LuaValue | None) -> str:
    """How an entry reads in the panel: KOReader's title, or the actions it holds."""
    try:
        action = action_of(value)
    except KeyError:
        if not isinstance(value, dict):
            return "Something kodesk does not know"
        names = [TITLES.get(str(k), str(k)) for k in value if k != "settings"]
        return " + ".join(names) or NOTHING
    return NOTHING if action is None else TITLES[action]


def current(gestures: SettingsDocument | None, gesture: str) -> LuaValue | None:
    """The entry on the reader; KOReader's default while it has no gestures file yet."""
    if gestures is None or not isinstance(gestures.data.get(SECTION), dict):
        default = DEFAULTS.get(gesture)
        return {default: True} if default else None
    return gestures.get(path(gesture))


def with_action(gestures: SettingsDocument, gesture: str, action: str | None) -> SettingsDocument:
    """``gestures`` with one entry replaced (None: removed, i.e. pass through)."""
    if action is None:
        return gestures.delete(path(gesture))
    if action not in TITLES:
        raise ValueError(f"not an offered action: {action}")
    return gestures.set(path(gesture), {action: True})
