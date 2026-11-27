"""Store tab (B-07) with a fake catalogue and fake fetches: never the network."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextDocument
from pytestqt.qtbot import QtBot

from kodesk.core import catalogue as cat
from kodesk.core.catalogue import Catalogue, CatalogueError, Entry, Kind
from kodesk.core.extensions import NOT_SUPPORTED, SUPPORTED
from kodesk.ui import store as store_module
from kodesk.ui.app import MainWindow, demo_context
from kodesk.ui.store import StorePage, entry_title, garage_support, open_external
from tests.conftest import THIRD_PARTY_PLUGIN, USER_PATCH, fake_catalogue, write_fake_catalogue


class Fake:
    """Loader and fetcher that count their calls."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.loads = 0
        self.fetched: list[str] = []

    def load(self) -> Catalogue:
        self.loads += 1
        if self.fail:
            raise CatalogueError("Could not load the catalogue (network unreachable).")
        return cat.parse(*fake_catalogue())

    def fetch(self, entry: Entry) -> str:
        self.fetched.append(entry.name)
        return f"# {entry.name}\n\nSee [the docs](https://github.com/{entry.owner}).\n"


@pytest.fixture(name="page")
def fixture_page(qtbot: QtBot) -> tuple[StorePage, Fake]:
    fake = Fake()
    page = StorePage(fake.load, fake.fetch, None)
    qtbot.addWidget(page)
    page.ensure_loaded()
    qtbot.waitUntil(lambda: page.catalogue is not None)
    return page, fake


def _names(page: StorePage) -> list[str]:
    return [page.list.item(row).text().split("\n")[0] for row in range(page.list.count())]


def test_lists_the_catalogue_with_credit(page: tuple[StorePage, Fake]) -> None:
    store, fake = page
    assert _names(store) == [
        THIRD_PARTY_PLUGIN,
        "dictsync",
        f"Patch {USER_PATCH}",
        "readingclock",
        "nolicence",
    ]
    assert "omer-faruq/koreader-plugin-index" in store.credit.text()
    assert "26 Nov 2026" in store.credit.text() and "offline" not in store.credit.text()
    assert store.current() is None and not fake.fetched  # nothing fetched before a selection
    store.ensure_loaded()
    assert fake.loads == 1


def test_search_and_filters_fetch_nothing(page: tuple[StorePage, Fake], qtbot: QtBot) -> None:
    store, fake = page
    qtbot.keyClicks(store.search, "clock")
    assert _names(store) == ["readingclock"]
    store.search.clear()
    store.kind.setCurrentIndex(store.kind.findText("Patches"))
    assert _names(store) == [f"Patch {USER_PATCH}"]
    store.kind.setCurrentIndex(0)
    store.category.setCurrentIndex(store.category.findText("Sync"))
    assert _names(store) == ["dictsync"]
    qtbot.keyClicks(store.search, "zzz")
    assert _names(store) == ["Nothing matches."]
    qtbot.wait(store_module.FETCH_DELAY_MS + 100)
    assert not fake.fetched


def test_readme_once_the_selection_rests(page: tuple[StorePage, Fake], qtbot: QtBot) -> None:
    store, fake = page
    store.list.setFocus()
    for _ in range(3):  # arrowing through the list: one request, for where it stops
        qtbot.keyClick(store.list, Qt.Key.Key_Down)
    current = store.current()
    assert current is not None and store.details.title.text() == entry_title(current)
    qtbot.waitUntil(lambda: "the docs" in store.details.readme.toPlainText())
    assert fake.fetched == [current.name]
    assert "Compatibility not declared" in store.details.badges.text()
    row = store.list.currentRow()
    store.list.setCurrentRow(0)
    store.list.setCurrentRow(row)
    qtbot.wait(store_module.FETCH_DELAY_MS + 100)
    assert fake.fetched == [current.name]  # kept for this session, not fetched again


def test_readme_error_keeps_the_description(qtbot: QtBot) -> None:
    def fail(_entry: Entry) -> str:
        raise CatalogueError("kodesk runs offline")

    store = StorePage(Fake().load, fail, None)
    qtbot.addWidget(store)
    store.ensure_loaded()
    qtbot.waitUntil(lambda: store.catalogue is not None)
    store.list.setCurrentRow(0)
    qtbot.waitUntil(lambda: "could not be loaded" in store.details.readme.toPlainText())
    current = store.current()
    assert current is not None and current.description in store.details.readme.toPlainText()


def test_keyboard_only(page: tuple[StorePage, Fake], qtbot: QtBot) -> None:
    store, _fake = page
    with qtbot.waitActive(store):  # shortcuts and focus need the active window
        store.show()
        store.activateWindow()
    store.list.setFocus()
    qtbot.keyClick(store.list, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert store.search.hasFocus()
    qtbot.keyClicks(store.search, "clock")
    qtbot.keyClick(store.search, Qt.Key.Key_Return)
    current = store.current()
    assert store.list.hasFocus() and current is not None and current.name == "readingclock"
    qtbot.keyClick(store.list, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    store.search.setText("zzz")
    qtbot.keyClick(store.search, Qt.Key.Key_Return)  # "Nothing matches." is not a result
    assert store.search.hasFocus() and store.current() is None


def test_installed_entries_are_marked(page: tuple[StorePage, Fake]) -> None:
    store, _fake = page
    store.set_installed(frozenset({"ProjectTitle"}), frozenset({USER_PATCH}))
    marked = [n for n in _names(store) if n.endswith("✓ installed")]
    assert marked == [f"{THIRD_PARTY_PLUGIN}  ✓ installed", f"Patch {USER_PATCH}  ✓ installed"]
    store.list.setCurrentRow(0)
    assert store.details.badges.text().startswith("Installed")


def test_failed_load_says_so_and_reload_works(qtbot: QtBot) -> None:
    fake = Fake(fail=True)
    store = StorePage(fake.load, fake.fetch, None)
    qtbot.addWidget(store)
    store.ensure_loaded()
    qtbot.waitUntil(store.reload_button.isEnabled)
    assert "could not be loaded" in _names(store)[0] and "network unreachable" in _names(store)[0]
    fake.fail = False
    qtbot.mouseClick(store.reload_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: store.catalogue is not None)
    assert len(_names(store)) == 5


def test_index_text_is_never_markup(page: tuple[StorePage, Fake]) -> None:
    store, _fake = page
    details = store.details
    for label in (details.title, details.facts, details.badges):
        assert label.textFormat() == Qt.TextFormat.PlainText


def test_readme_loads_no_images_or_local_files(page: tuple[StorePage, Fake]) -> None:
    store, _fake = page
    view = store.details.readme
    entry = cat.parse(*fake_catalogue()).entries[0]
    view.show_text(entry, "![x](https://tracker.example/p.png)\n![y](file:///etc/hostname)\n")
    image = QTextDocument.ResourceType.ImageResource.value
    assert view.loadResource(image, QUrl("file:///etc/hostname")) is None
    assert "<script" not in view.toHtml()


def test_only_https_links_open(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def open_url(url: QUrl) -> bool:
        opened.append(url.toString())
        return True

    monkeypatch.setattr(QDesktopServices, "openUrl", open_url)
    assert not open_external("http://example.com/")
    assert not open_external("file:///etc/passwd")
    assert open_external("https://github.com/alice")
    assert opened == ["https://github.com/alice"]


def test_no_network_before_the_store_tab_opens(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    QStandardPaths.setTestModeEnabled(True)
    calls: list[str] = []
    monkeypatch.setattr(cat, "https_get", lambda url, *_args: calls.append(url))
    cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    write_fake_catalogue(Path(cache) / "catalogue")
    window = MainWindow(demo_context(), offline=True)
    qtbot.addWidget(window)
    assert window.tabs.currentWidget() is window.garage and window.store.catalogue is None
    window.tabs.setCurrentWidget(window.store)
    assert window.focusWidget() is window.store.search
    qtbot.waitUntil(lambda: window.store.catalogue is not None)
    assert window.store.catalogue is not None and window.store.catalogue.offline
    assert "offline" in window.store.credit.text()
    assert not calls


def test_garage_support_badge() -> None:
    plugin = cat.parse(*fake_catalogue()).entries[0]
    assert garage_support(plugin) == NOT_SUPPORTED
    listed = replace(plugin, owner="glaucon1984", repo="pbfixes.koplugin", name="pbfixes")
    assert garage_support(listed) == SUPPORTED
    patch = next(e for e in cat.parse(*fake_catalogue()).entries if e.kind is Kind.PATCH)
    assert garage_support(patch) == ""


def test_search_for_fills_in_and_selects(page: tuple[StorePage, Fake]) -> None:
    store, _fake = page
    store.kind.setCurrentIndex(2)  # patches only: reset by search_for
    store.search_for("clock", "readingclock")
    assert store.search.text() == "clock" and store.kind.currentIndex() == 0
    current = store.current()
    assert current is not None and current.name == "readingclock"
    assert NOT_SUPPORTED in store.details.badges.text()
