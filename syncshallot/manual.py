"""Bundled, searchable manual; no network or filesystem writes are needed to read it."""
from bisect import bisect_right
from html import escape
import json
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import (QColor, QDesktopServices, QIcon, QKeySequence,
                          QShortcut, QTextCharFormat, QTextCursor, QTextDocument)
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                              QLineEdit, QPushButton, QTextBrowser, QTextEdit, QVBoxLayout)

from .i18n import LANGUAGES, Translator

DATA = Path(__file__).parent / "assets" / "manual"
ICON = Path(__file__).parent / "assets" / "syncshallot.png"


def load_manual(language):
    language = language if language in LANGUAGES else "en"
    return json.loads((DATA / f"{language}.json").read_text(encoding="utf-8"))


class ManualWindow(QDialog):
    def __init__(self, parent=None, tr=None):
        # An independent native window can remain alongside the main app.
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowTitleHint |
                         Qt.WindowType.WindowSystemMenuHint | Qt.WindowType.WindowMinMaxButtonsHint |
                         Qt.WindowType.WindowCloseButtonHint)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        if parent is not None:
            parent.destroyed.connect(self.deleteLater)
        self.tr = tr or Translator()
        self.setWindowTitle(self.tr("manual_title") + " — SyncShallot")
        self.setWindowIcon(QIcon(str(ICON)))
        self.resize(900, 740)
        self.setMinimumSize(550, 400)
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("manual_search"))
        self.search.setAccessibleName(self.tr("manual_search"))
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.find)
        self.search.textChanged.connect(self.highlight_matches)
        bar.addWidget(self.search, 1)
        self.contents_button = QPushButton(self.tr("manual_contents"))
        self.previous_button = QPushButton(self.tr("manual_previous"))
        self.next_button = QPushButton(self.tr("manual_next"))
        for button in (self.contents_button, self.previous_button, self.next_button):
            button.setAutoDefault(False)
            bar.addWidget(button)
        self.contents_button.clicked.connect(lambda: self.go("contents"))
        self.previous_button.clicked.connect(lambda: self.step(-1))
        self.next_button.clicked.connect(lambda: self.step(1))
        layout.addLayout(bar)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.anchorClicked.connect(self.open_link)
        self.browser.verticalScrollBar().valueChanged.connect(self.track_topic)
        layout.addWidget(self.browser, 1)
        self.status = QLabel(self.tr("manual_search_hint"))
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        close = buttons.addButton(self.tr("close"), QDialogButtonBox.ButtonRole.RejectRole)
        close.setAutoDefault(False)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self._shortcuts = []
        for keys, callback in (("Ctrl+F", self.focus_search), ("F3", self.find), ("Shift+F3", lambda: self.find(True))):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self._jumping = True
        self.index = -1
        data = load_manual(self.tr.language)
        dark = self.browser.palette().base().color().lightness() < 128
        link = "#79bfff" if dark else "#0057b8"
        self.topics = [s["anchor"] for s in data["sections"]]
        out = [f"<h1>SyncShallot — {escape(self.tr('manual_title'))}</h1>",
               f"<p>{escape(data['intro'])}</p>",
               f"<p>{escape(self.tr('manual_navigation_hint'))}</p>",
               f"<h2><a name='contents'>{escape(self.tr('manual_contents'))}</a></h2><ul>"]
        for n, section in enumerate(data["sections"], 1):
            out.append(f"<li><a style='color:{link}' href='#{section['anchor']}'>{n}. {escape(section['title'])}</a></li>")
        out.append("</ul>")
        for n, section in enumerate(data["sections"], 1):
            out.append(f"<h2><a name='{section['anchor']}'>{n}. {escape(section['title'])}</a></h2>{section['body']}")
        self.browser.setHtml("\n".join(out))
        positions = {}
        block = self.browser.document().begin()
        while block.isValid():
            fragment = block.begin()
            while not fragment.atEnd():
                value = fragment.fragment()
                if value.isValid():
                    for name in value.charFormat().anchorNames():
                        positions.setdefault(name, block.position())
                fragment += 1
            block = block.next()
        self.positions = [positions[a] for a in self.topics]
        self._jumping = False
        self.update_buttons()

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def update_buttons(self):
        self.previous_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index + 1 < len(self.topics))

    def track_topic(self, *_):
        if self._jumping:
            return
        position = self.browser.cursorForPosition(QPoint(0, 0)).position()
        self.index = bisect_right(self.positions, position) - 1
        bar = self.browser.verticalScrollBar()
        if bar.maximum() and bar.value() == bar.maximum():
            self.index = len(self.topics) - 1
        self.update_buttons()

    def go(self, anchor):
        if anchor != "contents" and anchor not in self.topics:
            return
        self._jumping = True
        self.browser.scrollToAnchor(anchor)
        self._jumping = False
        self.index = self.topics.index(anchor) if anchor in self.topics else -1
        self.update_buttons()

    def step(self, direction):
        index = self.index + direction
        if 0 <= index < len(self.topics):
            self.go(self.topics[index])

    def open_link(self, url):
        if url.isRelative() and url.fragment():
            self.go(url.fragment())
        elif url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)

    def highlight_matches(self, *_):
        term = self.search.text().strip()
        matches = []
        if term:
            document = self.browser.document()
            cursor = document.find(term, 0)
            while not cursor.isNull() and len(matches) < 2000:
                match = QTextEdit.ExtraSelection()
                match.cursor = cursor
                match.format = QTextCharFormat()
                match.format.setBackground(QColor(255, 210, 0, 110))
                matches.append(match)
                cursor = document.find(term, cursor)
        self.browser.setExtraSelections(matches)
        self.status.setText(self.tr("manual_search_hint"))

    def find(self, backwards=False):
        term = self.search.text().strip()
        if not term:
            self.focus_search()
            return False
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        found = self.browser.find(term, flags)
        if not found:
            self.browser.moveCursor(QTextCursor.MoveOperation.End if backwards else QTextCursor.MoveOperation.Start)
            found = self.browser.find(term, flags)
        self.status.setText(self.tr("manual_found", term=term, count=len(self.browser.extraSelections()))
                            if found else self.tr("manual_not_found", term=term))
        return found
