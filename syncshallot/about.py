"""About window with the same information blocks as the other family apps."""
from html import escape
from pathlib import Path
import platform

import PySide6
from PySide6.QtCore import Qt, qVersion
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QVBoxLayout

from . import __version__
from .i18n import Translator

WEBSITE = "https://github.com/rene-timmermans/SyncShallot"
ICON = Path(__file__).parent / "assets" / "syncshallot.png"


class AboutDialog(QDialog):
    def __init__(self, parent=None, tr=None):
        super().__init__(parent)
        tr = tr or Translator()
        self.setWindowTitle(tr("about_title"))
        self.setWindowIcon(QIcon(str(ICON)))
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.mascot = QLabel()
        self.mascot.setObjectName("AboutMascot")
        self.mascot.setPixmap(QPixmap(str(ICON)).scaled(120, 120,
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.mascot.setAlignment(Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.mascot)
        palette = self.palette()
        background, text = palette.window().color(), palette.windowText().color()
        muted = QColor.fromRgbF(*(a * .72 + b * .28 for a, b in zip(text.getRgbF()[:3], background.getRgbF()[:3])))
        link = "#79bfff" if background.lightness() < 128 else "#0057b8"
        components = tr("about_components", python=platform.python_version(), qt=qVersion(), pyside=PySide6.__version__)
        self.description = QLabel(
            f"<h2 style='margin:0'>SyncShallot {__version__}</h2>"
            f"<p>{escape(tr('about_description'))}</p>"
            f"<p><b>{escape(tr('about_author'))}</b><br>René Timmermans<br>{escape(tr('about_credit'))}</p>"
            f"<p><b>{escape(tr('about_license'))}</b><br>{escape(tr('about_license_text'))}</p>"
            f"<p><b>{escape(tr('about_website'))}</b><br><a style='color:{link}' href='{WEBSITE}'>{WEBSITE}</a></p>"
            f"<p style='color:{muted.name()}'>{escape(components)}</p>")
        self.description.setWordWrap(True)
        self.description.setMinimumWidth(420)
        self.description.setOpenExternalLinks(True)
        self.description.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        top.addWidget(self.description, 1)
        layout.addLayout(top)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
