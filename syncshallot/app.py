"""Native twin-pane Qt interface. All filesystem work runs off the GUI thread."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSettings,
                            QSortFilterProxyModel, Qt, QThread, QTimer, QUrl, Signal)
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTableView,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import __version__, engine
from .i18n import LANGUAGES, Translator

ICON = Path(__file__).parent / "assets" / "syncshallot.png"
MODES = ("left_to_right", "right_to_left", "two_way")


def allowed_actions(plan, row):
    actions = ["skip"]
    if row.status in ("unsupported", "blocked", "type_conflict", "same"):
        return actions
    if row.left and plan.mode != "right_to_left":
        actions.append("copy_right")
    if row.right and plan.mode != "left_to_right":
        actions.append("copy_left")
    if engine.is_extra(plan, row):
        actions.extend(("isolate", "delete"))
    return actions


def readable_size(entry, tr):
    if entry is None:
        return "—"
    if entry.kind == "dir":
        return tr("folder")
    value = entry.size
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.2f} {unit}"
        value /= 1024


class PlanModel(QAbstractTableModel):
    def __init__(self, tr, parent=None):
        super().__init__(parent)
        self.tr = tr
        self.plan = None

    def set_plan(self, plan):
        self.beginResetModel()
        self.plan = plan
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or not self.plan else len(self.plan.rows)

    def columnCount(self, parent=QModelIndex()):
        return 8

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.tr(("path", "size", "date", "path", "size", "date", "status", "action")[section])

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not self.plan:
            return None
        row = self.plan.rows[index.row()]
        col = index.column()
        entry = row.left if col < 3 else row.right
        if role == Qt.ItemDataRole.DisplayRole:
            if col in (0, 3):
                return row.path + ("/" if entry and entry.kind == "dir" else "") if entry else "—"
            if col in (1, 4):
                return readable_size(entry, self.tr)
            if col in (2, 5):
                return datetime.fromtimestamp(entry.sig[4] / 1e9).strftime("%Y-%m-%d %H:%M:%S") if entry else "—"
            if col == 6:
                return self.tr(row.status)
            return self.tr(row.action)
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.path + "\n" + self.tr(row.status) + "\n" + self.tr(row.action)
        if role == Qt.ItemDataRole.ForegroundRole:
            palette = QApplication.palette()
            if col == 7 and row.action == "delete":
                return QColor("#d75261") if palette.window().color().lightness() < 128 else QColor("#a8162e")
            if (col < 3 and row.left is None) or (3 <= col < 6 and row.right is None) or row.status == "same":
                text, base = palette.text().color(), palette.base().color()
                return QColor.fromRgbF(*(a * .72 + b * .28 for a, b in zip(text.getRgbF()[:3], base.getRgbF()[:3])))
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (1, 4):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


class PlanFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.category = "all"

    def filterAcceptsRow(self, source_row, source_parent):
        plan = self.sourceModel().plan
        if not plan:
            return False
        row = plan.rows[source_row]
        if self.query not in row.path.casefold():
            return False
        return (self.category == "all" or
                (self.category == "changes" and row.status != "same") or
                (self.category == "extras" and engine.is_extra(plan, row)) or
                (self.category == "conflicts" and (row.status in ("unsupported", "blocked", "type_conflict") or
                    (plan.mode == "two_way" and row.status == "different" and row.action == "skip"))))


class Worker(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.cancel = threading.Event()
        self.last_progress = 0

    def report(self, *args):
        now = time.monotonic()
        completed, total, detail = args if len(args) == 3 else (None, None, args[0])
        if now - self.last_progress > .15 or (total and completed == total):
            self.last_progress = now
            self.progress.emit((completed, total, detail))

    def run(self):
        try:
            self.result.emit(self.function(self.cancel, self.report))
        except Exception as error:
            self.failed.emit(str(error))


class Review(QDialog):
    def __init__(self, plan, recovery, tr, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("review_title"))
        self.resize(1000, 650)
        layout = QVBoxLayout(self)
        selected = [r for r in plan.rows if r.action != "skip"]
        info = QLabel(tr(plan.mode) + "\n" + str(plan.left) + "\n" + str(plan.right))
        info.setTextFormat(Qt.TextFormat.PlainText)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(info)
        help_label = QLabel(tr("review_help"))
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        counts = Counter()
        for row in selected:
            if row.action.startswith("copy_"):
                source = row.left if row.action == "copy_right" else row.right
                dest = row.right if row.action == "copy_right" else row.left
                key = "new_dirs" if source.kind == "dir" else "overwrite" if dest else "new_files"
            else:
                key = row.action
            counts[key] += 1
        counter = QLabel(" · ".join(f"{tr(k)}: {v}" for k, v in counts.items()))
        counter.setWordWrap(True)
        layout.addWidget(counter)
        table = QTableWidget(len(selected), 3)
        table.setHorizontalHeaderLabels([tr("path"), tr("action"), tr("overwrite")])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, row in enumerate(selected):
            dest = row.right if row.action == "copy_right" else row.left
            overwrite = bool(row.action.startswith("copy_") and dest and dest.kind == "file")
            for j, text in enumerate((row.path, tr(row.action), "✓" if overwrite else "—")):
                table.setItem(i, j, QTableWidgetItem(text))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(0, 550)
        table.resizeColumnToContents(1)
        table.resizeColumnToContents(2)
        layout.addWidget(table)
        recovery_label = QLabel(tr("recovery") + ": " + recovery)
        recovery_label.setTextFormat(Qt.TextFormat.PlainText)
        recovery_label.setWordWrap(True)
        layout.addWidget(recovery_label)
        unresolved = sum(r.status in ("unsupported", "blocked", "type_conflict") or
                         (plan.mode == "two_way" and r.status == "different" and r.action == "skip") for r in plan.rows)
        if unresolved:
            label = QLabel(tr("unresolved", count=unresolved))
            label.setWordWrap(True)
            layout.addWidget(label)
        self.ack = QCheckBox(tr("acknowledge"))
        layout.addWidget(self.ack)
        self.deletion = QLineEdit()
        if counts["delete"]:
            warning = QLabel(tr("delete_warning", count=counts["delete"]))
            warning.setWordWrap(True)
            layout.addWidget(warning)
            self.deletion.setPlaceholderText("DELETE")
            layout.addWidget(self.deletion)
        else:
            self.deletion.hide()
        buttons = QDialogButtonBox()
        self.run_button = buttons.addButton(tr("execute"), QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_button = buttons.addButton(tr("cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        cancel_button.setDefault(True)
        self.run_button.setAutoDefault(False)
        self.run_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        def enable():
            self.run_button.setEnabled(self.ack.isChecked() and (not counts["delete"] or self.deletion.text() == "DELETE"))
        self.ack.toggled.connect(enable)
        self.deletion.textChanged.connect(enable)


class MainWindow(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings or QSettings("SyncShallot", "SyncShallot")
        self.tr = Translator(self.settings.value("language", "nl"))
        self.plan = None
        self.worker = None
        self.pending_rescan = False
        self._columns_initialized = False
        self.last_recovery = ""
        self.manual_window = None
        self.setWindowTitle("SyncShallot")
        self.setWindowIcon(QIcon(str(ICON)))
        self.resize(1440, 900)
        self.model = PlanModel(self.tr, self)
        self.proxy = PlanFilter(self)
        self.proxy.setSourceModel(self.model)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        header = QHBoxLayout()
        mascot = QLabel()
        mascot.setPixmap(QPixmap(str(ICON)).scaled(66, 66, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        header.addWidget(mascot)
        brand = QLabel("<b style='font-size:22px'>SyncShallot</b><br>" + self.tr("tagline"))
        header.addWidget(brand, 1)
        outer.addLayout(header)

        self.config = QWidget()
        options = QVBoxLayout(self.config)
        options.setContentsMargins(0, 0, 0, 0)
        paths = QHBoxLayout()
        self.left = QLineEdit(str(self.settings.value("left", "")))
        self.right = QLineEdit(str(self.settings.value("right", "")))
        self.path_groups = []
        for name, field in (("left", self.left), ("right", self.right)):
            group = QGroupBox(self.tr(name))
            self.path_groups.append(group)
            box = QHBoxLayout(group)
            box.addWidget(field, 1)
            button = QPushButton(self.tr("browse"))
            button.clicked.connect(lambda checked=False, field=field: self.browse(field))
            box.addWidget(button)
            paths.addWidget(group, 1)
        options.addLayout(paths)
        choices = QHBoxLayout()
        choices.addWidget(QLabel(self.tr("direction")))
        self.mode = QComboBox()
        for key in MODES:
            self.mode.addItem(self.tr(key), key)
        # Left-side write protection is reset every launch and never loaded from profiles.
        self.mode.model().item(1).setEnabled(False)
        self.mode.model().item(2).setEnabled(False)
        choices.addWidget(self.mode, 2)
        choices.addWidget(QLabel(self.tr("scan_method")))
        self.method = QComboBox()
        self.method.addItem(self.tr("thorough"), True)
        self.method.addItem(self.tr("quick"), False)
        self.method.setCurrentIndex(int(self.settings.value("quick", 0)))
        choices.addWidget(self.method, 2)
        options.addLayout(choices)
        outer.addWidget(self.config)

        self.options_dialog = QDialog(self)
        self.options_dialog.setWindowTitle(self.tr("preferences"))
        self.options_dialog.resize(820, 620)
        preferences = QVBoxLayout(self.options_dialog)
        self.unlock = QCheckBox(self.tr("unlock"))
        preferences.addWidget(self.unlock)
        self.direction_note = QLabel()
        self.direction_note.setWordWrap(True)
        self.direction_note.setFrameStyle(QFrame.Shape.StyledPanel)
        self.direction_note.setMargin(8)
        preferences.addWidget(self.direction_note)
        self.scan_note = QLabel(self.tr("scan_help"))
        self.scan_note.setWordWrap(True)
        preferences.addWidget(self.scan_note)
        self.advanced = QGroupBox(self.tr("advanced"))
        adv_layout = QVBoxLayout(self.advanced)
        self.advanced_body = QWidget()
        form = QFormLayout(self.advanced_body)
        self.recovery = QLineEdit(str(self.settings.value("recovery", "")))
        recovery_row = QWidget()
        line = QHBoxLayout(recovery_row)
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(self.recovery)
        recovery_browse = QPushButton(self.tr("browse"))
        recovery_browse.clicked.connect(lambda: self.browse(self.recovery))
        line.addWidget(recovery_browse)
        form.addRow(self.tr("recovery"), recovery_row)
        recovery_help = QLabel(self.tr("recovery_help"))
        recovery_help.setWordWrap(True)
        form.addRow(recovery_help)
        self.excludes = QPlainTextEdit(str(self.settings.value("excludes", "")))
        self.excludes.setMaximumHeight(65)
        form.addRow(self.tr("excludes"), self.excludes)
        exclude_help = QLabel(self.tr("exclude_help"))
        exclude_help.setWordWrap(True)
        form.addRow(exclude_help)
        adv_layout.addWidget(self.advanced_body)
        preferences.addWidget(self.advanced)
        preferences.addStretch(1)
        options_buttons = QDialogButtonBox()
        options_buttons.addButton(self.tr("close"), QDialogButtonBox.ButtonRole.RejectRole)
        options_buttons.rejected.connect(self.options_dialog.reject)
        preferences.addWidget(options_buttons)

        self.action_bar = QWidget()
        actions = QHBoxLayout(self.action_bar)
        actions.setContentsMargins(0, 0, 0, 0)
        self.compare_button = QPushButton(self.tr("compare"))
        self.compare_button.setProperty("primary", True)
        self.review_button = QPushButton(self.tr("review"))
        self.review_button.setEnabled(False)
        self.compare_button.clicked.connect(self.start_compare)
        self.review_button.clicked.connect(self.review)
        actions.addWidget(self.compare_button)
        actions.addWidget(self.review_button)
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("search"))
        self.search.textChanged.connect(self.filter_rows)
        actions.addWidget(self.search, 1)
        self.filter = QComboBox()
        for category in ("all", "changes", "extras", "conflicts"):
            self.filter.addItem(self.tr(category), category)
        self.filter.currentIndexChanged.connect(self.filter_rows)
        actions.addWidget(self.filter)
        outer.addWidget(self.action_bar)
        self.summary = QLabel(self.tr("empty"))
        self.summary.setWordWrap(True)
        outer.addWidget(self.summary)

        self.splitter = QSplitter()
        self.views = []
        for side in range(2):
            view = QTableView()
            view.setModel(self.proxy)
            view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            view.setAlternatingRowColors(True)
            view.setWordWrap(False)
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
            view.verticalHeader().setDefaultSectionSize(27)
            view.verticalHeader().hide()
            view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            visible = (0, 1, 2, 6) if side == 0 else (3, 4, 5, 7)
            for col in range(8):
                view.setColumnHidden(col, col not in visible)
            view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            view.horizontalHeader().setMinimumSectionSize(48)
            view.setColumnWidth(visible[0], 320)
            for col in visible[1:]:
                view.setColumnWidth(col, 105 if col in (1, 4) else 150 if col in (2, 5) else 190)
            self.splitter.addWidget(view)
            self.views.append(view)
        self.views[1].setSelectionModel(self.views[0].selectionModel())
        self.views[0].verticalScrollBar().valueChanged.connect(self.views[1].verticalScrollBar().setValue)
        self.views[1].verticalScrollBar().valueChanged.connect(self.views[0].verticalScrollBar().setValue)
        outer.addWidget(self.splitter, 1)
        self.batch_bar = QWidget()
        batch = QHBoxLayout(self.batch_bar)
        batch.setContentsMargins(0, 0, 0, 0)
        batch.addWidget(QLabel(self.tr("selected")))
        self.batch_action = QComboBox()
        for key in ("skip", "copy_right", "copy_left", "isolate", "delete"):
            self.batch_action.addItem(self.tr(key), key)
        batch.addWidget(self.batch_action, 1)
        self.batch_apply = QPushButton(self.tr("apply"))
        self.batch_apply.clicked.connect(self.apply_actions)
        batch.addWidget(self.batch_apply)
        outer.addWidget(self.batch_bar)
        bottom = QHBoxLayout()
        progress_box = QVBoxLayout()
        self.progress_label = QLabel()
        self.progress_label.hide()
        progress_box.addWidget(self.progress_label)
        self.progress = QProgressBar()
        self.progress.hide()
        progress_box.addWidget(self.progress)
        self.file_progress = QProgressBar()
        self.file_progress.hide()
        progress_box.addWidget(self.file_progress)
        bottom.addLayout(progress_box, 1)
        self.stop_button = QPushButton(self.tr("stop"))
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        bottom.addWidget(self.stop_button)
        outer.addLayout(bottom)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(250)
        self.log.setMaximumHeight(85)
        self.log.hide()
        outer.addWidget(self.log)
        file_menu = self.menuBar().addMenu(self.tr("file"))
        self.file_actions = []
        for key, callback in (("save_profile", self.save_profile), ("load_profile", self.load_profile), ("export", self.export), ("open_recovery", self.open_recovery)):
            action = QAction(self.tr(key), self)
            action.triggered.connect(callback)
            file_menu.addAction(action)
            self.file_actions.append(action)
        self.options_menu = self.menuBar().addMenu(self.tr("options"))
        self.settings_action = self.options_menu.addAction(self.tr("preferences"))
        self.settings_action.triggered.connect(self.open_options)
        self.language_menu = self.options_menu.addMenu(self.tr("language"))
        self.language_group = QActionGroup(self)
        self.language_group.setExclusive(True)
        self.language_actions = {}
        for code, name in LANGUAGES.items():
            action = QAction(name, self)
            action.setCheckable(True)
            action.setData(code)
            action.setChecked(code == self.tr.language)
            self.language_group.addAction(action)
            self.language_menu.addAction(action)
            self.language_actions[code] = action
        self.language_group.triggered.connect(lambda action: self.change_language(action.data()))
        help_menu = self.menuBar().addMenu(self.tr("help"))
        self.manual_action = help_menu.addAction(self.tr("manual"))
        self.manual_action.setShortcut(QKeySequence("F1"))
        self.manual_action.triggered.connect(self.manual)
        self.about_action = help_menu.addAction(self.tr("about"))
        self.about_action.triggered.connect(self.about)
        for field in (self.left, self.right):
            field.textChanged.connect(self.invalidate)
        self.excludes.textChanged.connect(self.invalidate)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.method.currentIndexChanged.connect(self.invalidate)
        self.unlock.toggled.connect(self.unlock_changed)
        self.mode_changed()
        self.statusBar().showMessage(self.tr("ready"))

    def browse(self, field):
        path = QFileDialog.getExistingDirectory(self, self.tr("browse"), field.text() or str(Path.home()))
        if path:
            field.setText(path)

    def open_options(self):
        self.options_dialog.exec()
        self.save_settings()

    def invalidate(self, *_):
        if self.plan:
            self.plan = None
            self.model.set_plan(None)
            self.summary.setText(self.tr("stale"))
        self.review_button.setEnabled(False)

    def unlock_changed(self, enabled):
        self.mode.model().item(1).setEnabled(enabled)
        self.mode.model().item(2).setEnabled(enabled)
        if not enabled:
            self.mode.setCurrentIndex(0)
        self.invalidate()

    def mode_changed(self, *_):
        key = {"left_to_right": "protected_left", "right_to_left": "protected_right", "two_way": "two_way_warning"}[self.mode.currentData()]
        self.direction_note.setText(self.tr(key))
        mode = self.mode.currentData()
        for side, group in enumerate(self.path_groups):
            role = "both_role" if mode == "two_way" else "protected_role" if (side == 0) == (mode == "left_to_right") else "target_role"
            group.setTitle(self.tr("left" if side == 0 else "right") + " · " + self.tr(role))
        self.invalidate()

    def filter_rows(self, *_):
        modern = hasattr(self.proxy, "endFilterChange")
        if modern:
            self.proxy.beginFilterChange()
        self.proxy.query = self.search.text().casefold()
        self.proxy.category = self.filter.currentData()
        if modern:
            self.proxy.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.proxy.invalidateFilter()

    def update_summary(self):
        if not self.plan:
            return
        counts = Counter(r.status for r in self.plan.rows)
        self.summary.setText(self.tr("summary", total=len(self.plan.rows), same=counts["same"], different=counts["different"],
            extras=sum(engine.is_extra(self.plan, r) for r in self.plan.rows),
            conflicts=sum(r.status in ("unsupported", "blocked", "type_conflict") or (self.plan.mode == "two_way" and r.status == "different" and r.action == "skip") for r in self.plan.rows),
            actions=sum(r.action != "skip" for r in self.plan.rows)))

    def apply_actions(self):
        if not self.plan:
            return
        action = self.batch_action.currentData()
        indices = self.views[0].selectionModel().selectedRows()
        selected = {self.proxy.mapToSource(index).row() for index in indices}
        paths = {self.plan.rows[i].path for i in selected}
        # Folder operations include descendants so isolation preserves whole trees.
        rows = [r for r in self.plan.rows if r.path in paths or any(str(p) in paths for p in Path(r.path).parents)]
        lookup = {r.path: r for r in self.plan.rows}
        for row in rows:
            if action in allowed_actions(self.plan, row):
                row.action = action
                if action.startswith("copy_"):
                    # Include missing ancestors if the user previously disabled them.
                    for ancestor in Path(row.path).parents:
                        parent = lookup.get(str(ancestor))
                        if parent and action in allowed_actions(self.plan, parent):
                            parent.action = action
        if self.plan.rows:
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(len(self.plan.rows)-1, 7))
        self.filter_rows()
        self.update_summary()

    def set_busy(self, busy):
        for widget in (self.config, self.action_bar, self.batch_bar):
            widget.setEnabled(not busy)
        self.options_menu.setEnabled(not busy)
        for action in self.file_actions:
            action.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.progress.setVisible(busy)
        self.progress_label.setVisible(busy)
        self.file_progress.hide()
        if busy:
            self.progress.setRange(0, 0)
            self.progress_label.setText(self.tr("scan_progress"))
        self.review_button.setEnabled(not busy and self.plan is not None)

    def launch(self, function, callback, execution_total=None):
        self.set_busy(True)
        if execution_total is not None:
            self.show_progress((0, execution_total, engine.FileProgress("check", "")))
        self.worker = Worker(function, self)
        self.worker.result.connect(callback)
        self.worker.failed.connect(self.failed)
        self.worker.progress.connect(self.show_progress)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def show_progress(self, update):
        completed, total, detail = update
        if total is not None and total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(completed)
            self.progress.setFormat(self.tr("action_progress", completed=completed, total=total) + " · %p%")
            self.progress_label.hide()
        if isinstance(detail, engine.FileProgress):
            phase = self.tr("phase_" + detail.phase)
            message = phase + (" · " + detail.path if detail.path else "")
            if detail.total > 0:
                self.file_progress.setRange(0, 1000)
                self.file_progress.setValue(min(1000, detail.processed * 1000 // detail.total))
                self.file_progress.setFormat(phase + " · " + self.tr("byte_progress",
                    done=f"{detail.processed / (1024 * 1024):.1f}", total=f"{detail.total / (1024 * 1024):.1f}") + " · %p%")
                self.file_progress.show()
            else:
                self.file_progress.hide()
            self.statusBar().showMessage(message)
        else:
            self.file_progress.hide()
            self.statusBar().showMessage(str(detail))

    def start_compare(self):
        if self.worker and self.worker.isRunning():
            return
        left, right = self.left.text().strip(), self.right.text().strip()
        if not left or not right:
            QMessageBox.information(self, "SyncShallot", self.tr("empty"))
            return
        mode, thorough = self.mode.currentData(), self.method.currentData()
        if mode != "left_to_right" and not self.unlock.isChecked():
            return
        excludes = tuple(p.strip() for p in self.excludes.toPlainText().splitlines() if p.strip())
        self.invalidate()
        self.summary.setText(self.tr("scanning"))
        self.statusBar().showMessage(self.tr("scanning"))
        self.save_settings()
        self.launch(lambda cancel, progress: engine.compare(left, right, mode, thorough, excludes, cancel, progress), self.compared)

    def compared(self, plan):
        self.plan = plan
        self.model.set_plan(plan)
        self.update_summary()
        if not self.recovery.text().strip():
            self.recovery.setText(str(Path.home() / ".local/share/SyncShallot/recovery"))
        self.statusBar().showMessage(self.tr("ready"))

    def review(self):
        if not self.plan:
            return
        if not any(r.action != "skip" for r in self.plan.rows):
            QMessageBox.information(self, "SyncShallot", self.tr("no_actions"))
            return
        plan = self.plan
        recovery = self.recovery.text().strip()
        if Review(plan, recovery, self.tr, self).exec() != QDialog.DialogCode.Accepted:
            return
        if plan.mode != "left_to_right" and not self.unlock.isChecked():
            return
        self.summary.setText(self.tr("working"))
        self.save_settings()
        allow_left = self.unlock.isChecked()
        self.launch(lambda cancel, progress: engine.execute(plan, recovery, cancel, progress,
            allow_permanent_delete=any(r.action == "delete" for r in plan.rows), allow_left_changes=allow_left),
            self.executed, sum(r.action != "skip" for r in plan.rows))

    def executed(self, result):
        self.last_recovery = result["recovery"]
        self.log.show()
        self.log.appendPlainText(self.tr("done", count=result["completed"], path=result["recovery"]))
        self.invalidate()
        self.pending_rescan = True

    def failed(self, message):
        self.pending_rescan = False
        self.invalidate()
        self.summary.setText(self.tr("error"))
        self.log.show()
        self.log.appendPlainText(message)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("SyncShallot")
        box.setText(self.tr("error"))
        box.setInformativeText(self.tr("error_help") + "\n\n" + message)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.exec()

    def finished(self):
        self.set_busy(False)
        self.worker.deleteLater()
        self.worker = None
        if self.pending_rescan:
            self.pending_rescan = False
            QTimer.singleShot(0, self.start_compare)

    def stop(self):
        if self.worker:
            self.worker.cancel.set()
            self.stop_button.setEnabled(False)

    def profile(self):
        return {"schema": 1, "left": self.left.text(), "right": self.right.text(),
                "mode": self.mode.currentData(), "thorough": self.method.currentData(),
                "excludes": self.excludes.toPlainText(), "recovery": self.recovery.text()}

    def save_settings(self):
        if self._columns_initialized:
            for side, view in enumerate(self.views):
                self.settings.setValue(f"columns/{side}", view.horizontalHeader().saveState())
        for key, value in self.profile().items():
            if key not in ("mode", "schema", "thorough"):
                self.settings.setValue(key, value)
        self.settings.setValue("quick", self.method.currentIndex())
        self.settings.setValue("language", self.tr.language)
        self.settings.sync()

    def save_profile(self):
        path, _ = QFileDialog.getSaveFileName(self, self.tr("save_profile"), "", "JSON (*.json)")
        if path:
            try:
                Path(path).write_text(json.dumps(self.profile(), indent=2, ensure_ascii=True), encoding="utf-8")
            except OSError as error:
                self.failed(str(error))

    def load_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("load_profile"), "", "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if data.get("schema") != 1 or not all(isinstance(data.get(k), str) for k in ("left", "right", "recovery", "excludes")) or not isinstance(data.get("thorough"), bool):
                raise ValueError("Invalid SyncShallot profile")
            self.unlock.setChecked(False)
            self.mode.setCurrentIndex(0)
            self.left.setText(data["left"])
            self.right.setText(data["right"])
            self.recovery.setText(data["recovery"])
            self.excludes.setPlainText(data["excludes"])
            self.method.setCurrentIndex(0 if data["thorough"] else 1)
            # Imported profiles never unlock source writes or restore destructive actions.
        except (ValueError, OSError, TypeError, AttributeError) as error:
            self.failed(str(error))

    def export(self):
        if not self.plan:
            return
        path, _ = QFileDialog.getSaveFileName(self, self.tr("export"), "", "JSON (*.json)")
        if path:
            try:
                data = {"left": str(self.plan.left), "right": str(self.plan.right), "mode": self.plan.mode,
                        "thorough": self.plan.thorough, "rows": [asdict(r) for r in self.plan.rows]}
                Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
            except OSError as error:
                self.failed(str(error))

    def open_recovery(self):
        path = self.last_recovery or self.recovery.text()
        if path and Path(path).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def manual(self):
        from .manual import ManualWindow
        if self.manual_window is None:
            self.manual_window = ManualWindow(self, self.tr)
        self.manual_window.show()
        self.manual_window.raise_()
        self.manual_window.activateWindow()

    def about(self):
        from .about import AboutDialog
        AboutDialog(self, self.tr).exec()

    def change_language(self, language):
        if language == self.tr.language:
            return
        self.save_settings()
        self.settings.setValue("language", language)
        new_window = MainWindow(self.settings)
        QApplication.instance().window = new_window
        new_window.show()
        self.close()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "SyncShallot", self.tr("busy_close"))
            event.ignore()
        else:
            if self.manual_window is not None:
                self.manual_window.close()
                self.manual_window.deleteLater()
                self.manual_window = None
            # Do not overwrite a just-selected language during window replacement.
            language = self.settings.value("language", self.tr.language)
            self.save_settings()
            self.settings.setValue("language", language)
            event.accept()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._columns_initialized:
            for side, view in enumerate(self.views):
                header = view.horizontalHeader()
                saved = self.settings.value(f"columns/{side}")
                if not saved or not header.restoreState(saved):
                    view.setColumnWidth(0 if side == 0 else 3,
                                        max(220, view.viewport().width() - 105 - 150 - 190 - 2))
                # A restored layout must never lock the path column again.
                header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            self._columns_initialized = True


def install_style(app):
    """Use the system palette for both light and dark desktops, with familiar compact controls."""
    app.setStyle("Fusion")
    app.setStyleSheet("""
        QPushButton { padding: 5px 10px; border-radius: 4px; min-height: 20px;
            border: 1px solid palette(mid); background: palette(button); color: palette(button-text); }
        QPushButton:hover:enabled { background: palette(midlight); border-color: palette(highlight); }
        QPushButton:pressed:enabled { background: palette(mid); }
        QPushButton:focus { border: 1px dashed palette(highlight); }
        QPushButton:disabled { color: palette(disabled, button-text); border-color: palette(mid); }
        QToolButton { padding: 5px 10px; }
        QGroupBox { font-weight: 600; margin-top: 9px; padding-top: 9px; }
        QGroupBox::title { subcontrol-origin: margin; left: 8px; }
        QLineEdit, QComboBox { padding: 4px; min-height: 22px; }
        QTableView { gridline-color: palette(mid); }
        QHeaderView::section { padding: 5px; }
        QPushButton[primary="true"]:enabled { background: palette(highlight); color: palette(highlighted-text); }
        QSplitter::handle { background: palette(mid); width: 3px; }
    """)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SyncShallot")
    app.setOrganizationName("SyncShallot")
    app.setWindowIcon(QIcon(str(ICON)))
    install_style(app)
    if "--selftest" in sys.argv[1:]:
        from .selftest import run
        return run(app)
    window = MainWindow()
    app.window = window
    window.show()
    return app.exec()
