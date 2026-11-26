"""The Store tab: browse the community plugins and patches (B-07). Browsing only; no install.

The catalogue and each README are loaded in a worker thread (``QThreadPool``); results come
back through a queued signal, so the window never freezes on a slow network. Nothing is
loaded before the user opens the tab.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontDatabase, QKeySequence, QShortcut, QTextDocument
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from kodesk.core import catalogue as cat
from kodesk.core.catalogue import Catalogue, Entry, Kind

LoadCatalogue = Callable[[], Catalogue]
FetchText = Callable[[Entry], str]
MAX_LIST_TEXT = 90
FETCH_DELAY_MS = 400  # README fetch waits until the selection rests (arrowing = 1 request)
MARKDOWN = (
    QTextDocument.MarkdownFeature.MarkdownDialectGitHub
    | QTextDocument.MarkdownFeature.MarkdownNoHTML  # README HTML is shown as text, never run
)


def open_external(url: QUrl | str) -> bool:
    """Open a link in the browser, HTTPS only (README links are third-party content)."""
    url = QUrl(url)
    return url.scheme() == "https" and QDesktopServices.openUrl(url)


class Results(QObject):
    """Carries a worker's result back to the GUI thread (signals across threads are queued)."""

    done = Signal(str, object)  # token, result or the exception


def run_in_background(results: Results, token: str, work: Callable[[], object]) -> None:
    """Run ``work`` in the thread pool; ``results.done`` gets (token, result or exception)."""

    def run() -> None:
        try:
            result: object = work()
        except Exception as error:  # pylint: disable=broad-exception-caught  # shown to the user
            result = error
        results.done.emit(token, result)

    QThreadPool.globalInstance().start(run)


class ReadmeView(QTextBrowser):
    """README or patch code. Loads nothing by itself: no images, no local files, no links."""

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setObjectName("store-readme")
        self.setOpenLinks(False)
        self.anchorClicked.connect(open_external)

    def loadResource(self, _type: int, _name: QUrl | str) -> Any:  # Qt API name
        """Refuse every resource: remote images would track the user, local paths leak files."""
        return None

    def show_text(self, entry: Entry, text: str) -> None:
        """Markdown for a README; plain monospace for patch code."""
        if entry.kind is Kind.PATCH:
            self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
            self.setPlainText(text)
        else:
            self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
            self.document().setMarkdown(text, MARKDOWN)


def _date(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return "unknown"


def entry_title(entry: Entry) -> str:
    return f"Patch {entry.name}" if entry.kind is Kind.PATCH else entry.name


def entry_facts(entry: Entry) -> str:
    licence = entry.license or "no licence given"
    parts = [f"{entry.owner}/{entry.repo}", f"★ {entry.stars}", licence]
    parts += [entry.activity or "activity unknown", f"updated {_date(entry.pushed_at)}"]
    return "  ·  ".join(parts)


class EntryDetails(QWidget):
    """The selected entry: name, facts, badges, the repo link and the README or code."""

    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.title = QLabel(self)
        self.title.setObjectName("store-title")
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        self.title.setFont(font)
        self.facts = QLabel(self)
        self.badges = QLabel(self)
        self.badges.setObjectName("store-badges")
        self.link = QLabel(self)
        self.link.setObjectName("store-link")
        self.link.linkActivated.connect(open_external)
        self.install_button = QPushButton("Ins&tall…", self)
        self.install_button.setObjectName("store-install")
        self.readme = ReadmeView(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for label in (self.title, self.facts, self.badges, self.link):
            label.setWordWrap(True)
            layout.addWidget(label)
        row = QHBoxLayout()
        row.addWidget(self.install_button)
        row.addStretch(1)
        layout.addLayout(row)
        for label in (self.title, self.facts, self.badges):  # index text is data, never markup
            label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.readme, 1)

    def show_entry(self, entry: Entry | None, installed: bool) -> None:
        """Fill in ``entry``, or show a hint when nothing is selected."""
        for widget in (self.facts, self.badges, self.link, self.readme, self.install_button):
            widget.setVisible(entry is not None)
        if entry is None:
            self.title.setText("Select a plugin or patch to see its details.")
            return
        self.title.setText(entry_title(entry))
        self.facts.setText(entry_facts(entry))
        badges = ["Installed"] if installed else []
        badges += ["Archived"] if entry.archived else []
        badges.append("Compatibility not declared")
        self.badges.setText("  ·  ".join(badges))
        self.install_button.setText("&Update…" if installed else "Ins&tall…")
        url = html.escape(entry.url, quote=True)
        self.link.setText(f'<a href="{url}">{url}</a>')
        what = "code" if entry.kind is Kind.PATCH else "README"
        self.readme.setPlainText(f"{entry.description}\n\nLoading the {what}…")


class StorePage(QWidget):
    """Search and filter on top, the list on the left, the selected entry on the right."""

    install_requested = Signal(object)  # Entry
    loaded = Signal()  # a catalogue is shown

    def __init__(self, load: LoadCatalogue, fetch: FetchText, parent: QWidget | None) -> None:
        super().__init__(parent)
        self._load, self._fetch = load, fetch
        self.catalogue: Catalogue | None = None
        self._loading = False
        self._texts: dict[str, str] = {}
        self._installed: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
        self._results = Results(self)
        self._results.done.connect(self._finished)
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setSingleShot(True)
        self._fetch_timer.setInterval(FETCH_DELAY_MS)
        self._fetch_timer.timeout.connect(self._fetch_current)

        top = self._filters()

        self.list = QListWidget(self)
        self.list.setObjectName("store-list")
        self.details = EntryDetails(self)
        split = QSplitter(self)
        split.addWidget(self.list)
        split.addWidget(self.details)
        split.setStretchFactor(1, 2)
        self.credit = QLabel(self)
        self.credit.setObjectName("store-credit")
        self.credit.linkActivated.connect(open_external)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        layout.addWidget(self.credit)

        self.search.textChanged.connect(self.refilter)
        self.kind.currentIndexChanged.connect(self.refilter)
        self.category.currentIndexChanged.connect(self.refilter)
        self.list.currentItemChanged.connect(lambda item, _old: self.show_entry(self._entry(item)))
        self.reload_button.clicked.connect(self.reload)
        self.details.install_button.clicked.connect(self._install_clicked)
        self.search.returnPressed.connect(self.enter_results)
        find = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        find.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find.activated.connect(self.search.setFocus)
        self._message("The catalogue loads when you open this tab.")

    def _filters(self) -> QHBoxLayout:
        """Search field, kind and category filters and the reload button, in one row."""
        self.search = QLineEdit(self)
        self.search.setObjectName("store-search")
        self.search.setPlaceholderText("Search plugins and patches")
        self.search.setClearButtonEnabled(True)
        self.kind = QComboBox(self)
        self.kind.setObjectName("store-kind")
        for text, kind in (("Plugins and patches", None), ("Plugins", Kind.PLUGIN)):
            self.kind.addItem(text, kind)
        self.kind.addItem("Patches", Kind.PATCH)
        self.category = QComboBox(self)
        self.category.setObjectName("store-category")
        self.category.addItem("All categories", None)
        self.reload_button = QPushButton("Re&load", self)
        self.reload_button.setObjectName("store-reload")
        top = QHBoxLayout()
        for widget in (self.search, self.kind, self.category, self.reload_button):
            top.addWidget(widget, 1 if widget is self.search else 0)
        return top

    # -- loading -------------------------------------------------------------------------

    def ensure_loaded(self) -> None:
        """Load the catalogue once, the first time the tab is shown."""
        if self.catalogue is None and not self._loading:
            self.reload()

    def reload(self) -> None:
        """Fetch (or revalidate) the catalogue in the background."""
        if self._loading:
            return
        self._loading = True
        self.reload_button.setEnabled(False)
        self._message("Loading the catalogue…")
        run_in_background(self._results, "", self._load)

    def _finished(self, token: str, result: object) -> None:
        if not token:  # the catalogue
            self._loading = False
            self.reload_button.setEnabled(True)
            if isinstance(result, Catalogue):
                self.set_catalogue(result)
            else:
                self._message(f"The catalogue could not be loaded: {result}")
            return
        if isinstance(result, str):
            self._texts[token] = result
        current = self.current()
        if current is not None and current.id == token:
            if isinstance(result, str):
                self.details.readme.show_text(current, result)
            else:  # keep the description, say why there is no more
                what = "code" if current.kind is Kind.PATCH else "README"
                self.details.readme.setPlainText(
                    f"{current.description}\n\nThe {what} could not be loaded: {result}"
                )

    def set_catalogue(self, catalogue: Catalogue) -> None:
        """Show a loaded catalogue: categories, the credit line, the list."""
        self.catalogue = catalogue
        self.category.blockSignals(True)
        self.category.clear()
        self.category.addItem("All categories", None)
        for key, label in sorted(catalogue.categories.items(), key=lambda kv: kv[1]):
            self.category.addItem(label, key)
        self.category.blockSignals(False)
        offline = "  ·  offline: showing the copy from the last visit" if catalogue.offline else ""
        self.credit.setText(
            f'Index by <a href="{cat.SOURCE_URL}">{cat.SOURCE_NAME}</a>, built '
            f"{cat.built(catalogue)}{offline}"
        )
        self.refilter()
        self.loaded.emit()

    def set_installed(self, plugins: frozenset[str], patches: frozenset[str]) -> None:
        """Names on the connected reader, to mark entries as installed."""
        self._installed = (frozenset(p.lower() for p in plugins), patches)
        if self.catalogue is not None:
            self.refilter()

    # -- list and details ----------------------------------------------------------------

    def installed(self, entry: Entry) -> bool:
        """Whether ``entry`` is on the connected reader (name match)."""
        return cat.is_installed(entry, *self._installed)

    def refilter(self) -> None:
        """Rebuild the list from search text and filters; the selection stays if still shown."""
        if self.catalogue is None:
            return
        shown = cat.search(
            self.catalogue.entries,
            self.search.text(),
            self.kind.currentData(),
            self.category.currentData(),
        )
        if not shown:
            self._message("Nothing matches.")
            return
        kept = self.current()
        self.list.clear()
        for entry in shown:
            mark = "  ✓ installed" if self.installed(entry) else ""
            description = entry.description[:MAX_LIST_TEXT]
            if len(entry.description) > MAX_LIST_TEXT:
                description += "…"
            item = QListWidgetItem(f"{entry_title(entry)}{mark}\n{description}", self.list)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(entry.description)
            if kept is not None and entry.id == kept.id:
                self.list.setCurrentItem(item)  # e.g. after an install: stay on it
        if self.list.currentItem() is None:
            self.show_entry(None)

    def enter_results(self) -> None:
        """Enter in the search field: go to the first result (keyboard only)."""
        if self.list.count() and self._entry(self.list.item(0)) is not None:
            self.list.setFocus()
            self.list.setCurrentRow(0)

    def _message(self, text: str) -> None:
        self.list.clear()
        QListWidgetItem(text, self.list).setFlags(Qt.ItemFlag.NoItemFlags)
        self.show_entry(None)

    @staticmethod
    def _entry(item: QListWidgetItem | None) -> Entry | None:
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return data if isinstance(data, Entry) else None

    def current(self) -> Entry | None:
        """The selected entry, if any."""
        return self._entry(self.list.currentItem())

    def show_entry(self, entry: Entry | None) -> None:
        """Show ``entry`` now; its README follows once the selection rests."""
        self._fetch_timer.stop()
        self.details.show_entry(entry, entry is not None and self.installed(entry))
        if entry is None:
            return
        if entry.id in self._texts:
            self.details.readme.show_text(entry, self._texts[entry.id])
        else:
            self._fetch_timer.start()

    def _install_clicked(self) -> None:
        entry = self.current()
        if entry is not None:
            self.install_requested.emit(entry)

    def _fetch_current(self) -> None:
        entry = self.current()
        if entry is not None and entry.id not in self._texts:
            run_in_background(self._results, entry.id, lambda: self._fetch(entry))
