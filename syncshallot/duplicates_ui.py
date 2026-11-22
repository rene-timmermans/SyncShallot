"""Single-folder duplicate cleanup, with an explicit keeper in every group."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, QSignalBlocker
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout)
from PySide6.QtGui import QDesktopServices

from . import duplicates as core, engine
from .app import ICON, Worker


def size_text(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.2f} {unit}'
        value /= 1024


class DuplicateReview(QDialog):
    def __init__(self, plan, recovery, tr, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr('dup_review'))
        self.resize(1100, 650)
        layout = QVBoxLayout(self)
        for text in (tr('dup_review_help'), str(plan.root), tr('recovery') + ': ' + recovery):
            label = QLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
        selected = core.validate_actions(plan)
        table = QTableWidget(len(selected), 3)
        table.setHorizontalHeaderLabels([tr('path'), tr('action'), tr('dup_kept_file')])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, (path, action, group) in enumerate(selected):
            for j, text in enumerate((path, tr(action), group.keeper)):
                table.setItem(i, j, QTableWidgetItem(text))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(0, 390)
        table.setColumnWidth(1, 230)
        table.setColumnWidth(2, 390)
        layout.addWidget(table, 1)
        self.ack = QCheckBox(tr('acknowledge'))
        layout.addWidget(self.ack)
        deletions = sum(action == 'delete' for _, action, _ in selected)
        self.deletion = QLineEdit()
        if deletions:
            label = QLabel(tr('delete_warning', count=deletions))
            label.setWordWrap(True)
            layout.addWidget(label)
            self.deletion.setPlaceholderText('DELETE')
            layout.addWidget(self.deletion)
        else:
            self.deletion.hide()
        buttons = QDialogButtonBox()
        self.run_button = buttons.addButton(tr('execute'), QDialogButtonBox.ButtonRole.AcceptRole)
        self.run_button.setEnabled(False)
        self.run_button.setAutoDefault(False)
        cancel = buttons.addButton(tr('cancel'), QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        def enable():
            self.run_button.setEnabled(self.ack.isChecked() and (not deletions or self.deletion.text() == 'DELETE'))
        self.ack.toggled.connect(enable)
        self.deletion.textChanged.connect(enable)


class DuplicateDialog(QDialog):
    changed = Signal(str)

    def __init__(self, parent, tr, settings):
        super().__init__(parent)
        self.tr, self.settings = tr, settings
        self.setWindowTitle(tr('dup_title') + ' — SyncShallot')
        self.setWindowIcon(QIcon(str(ICON)))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(1200, 850)
        self.plan = None
        self.worker = None
        self.operation = None
        self.pending_rescan = False
        self._closing = False
        self.execution_root = None
        self.last_recovery = str(settings.value('duplicates/last_recovery', ''))
        self.manual_window = None
        self.items = {}
        layout = QVBoxLayout(self)
        intro = QLabel(tr('dup_intro'))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.inputs = QGroupBox(tr('dup_folder'))
        form = QFormLayout(self.inputs)
        self.root = QLineEdit(str(settings.value('duplicates/root', '')))
        self.recovery = QLineEdit(str(settings.value('duplicates/recovery', str(Path.home() / '.local/share/SyncShallot/recovery'))))
        for title, field in ((tr('dup_folder'), self.root), (tr('recovery'), self.recovery)):
            line = QHBoxLayout()
            line.addWidget(field, 1)
            button = QPushButton(tr('browse'))
            button.clicked.connect(lambda checked=False, field=field: self.browse(field))
            line.addWidget(button)
            form.addRow(title, line)
        layout.addWidget(self.inputs)
        excludes_group = QGroupBox(tr('excludes'))
        excludes_group.setCheckable(True)
        self.excludes = QPlainTextEdit(str(settings.value('duplicates/excludes', '')))
        self.excludes.setMaximumHeight(65)
        box = QVBoxLayout(excludes_group)
        box.addWidget(self.excludes)
        excludes_group.setChecked(bool(self.excludes.toPlainText().strip()))
        self.excludes.setVisible(excludes_group.isChecked())
        excludes_group.toggled.connect(self.excludes.setVisible)
        self.excludes_group = excludes_group
        layout.addWidget(excludes_group)
        bar = QHBoxLayout()
        self.scan_button = QPushButton(tr('dup_scan'))
        self.scan_button.setProperty('primary', True)
        self.scan_button.clicked.connect(self.start_scan)
        self.review_button = QPushButton(tr('review'))
        self.review_button.clicked.connect(self.review)
        self.review_button.setEnabled(False)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('search'))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_rows)
        bar.addWidget(self.scan_button)
        bar.addWidget(self.review_button)
        bar.addWidget(self.search, 1)
        layout.addLayout(bar)
        self.summary = QLabel(tr('dup_empty'))
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.skipped = QLabel()
        self.skipped.setWordWrap(True)
        self.skipped.hide()
        layout.addWidget(self.skipped)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels([tr('path'), tr('size'), tr('date'), tr('action')])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.header().setStretchLastSection(False)
        for i, width in enumerate((480, 120, 160, 260)):
            self.tree.setColumnWidth(i, width)
        self.tree.itemSelectionChanged.connect(self.update_selection)
        layout.addWidget(self.tree, 1)
        self.edit_bar = QGroupBox(tr('dup_choose'))
        edit = QHBoxLayout(self.edit_bar)
        self.keep_button = QPushButton(tr('dup_keep_this'))
        self.keep_button.clicked.connect(self.keep_selected)
        self.select_others = QPushButton(tr('dup_select_others'))
        self.select_others.clicked.connect(self.select_other_copies)
        self.batch_action = QComboBox()
        for action in ('skip', 'isolate', 'delete'):
            self.batch_action.addItem(tr(action), action)
        self.apply_button = QPushButton(tr('apply'))
        self.apply_button.clicked.connect(self.apply_actions)
        edit.addWidget(self.keep_button)
        edit.addWidget(self.select_others)
        edit.addWidget(self.batch_action, 1)
        edit.addWidget(self.apply_button)
        layout.addWidget(self.edit_bar)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.file_progress = QProgressBar()
        self.progress.hide()
        self.file_progress.hide()
        layout.addWidget(self.progress)
        layout.addWidget(self.file_progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(80)
        self.log.setMaximumBlockCount(150)
        self.log.hide()
        layout.addWidget(self.log)
        footer = QHBoxLayout()
        self.open_recovery_button = QPushButton(tr('open_recovery'))
        self.open_recovery_button.clicked.connect(self.open_recovery)
        help_button = QPushButton(tr('manual'))
        help_button.clicked.connect(self.show_help)
        footer.addWidget(self.open_recovery_button)
        footer.addWidget(help_button)
        footer.addStretch(1)
        self.stop_button = QPushButton(tr('stop'))
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        self.close_button = QPushButton(tr('close'))
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.stop_button)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.help_action = QAction(self)
        self.help_action.setShortcut(QKeySequence('F1'))
        self.help_action.triggered.connect(self.show_help)
        self.addAction(self.help_action)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.root.textChanged.connect(self.invalidate)
        self.excludes.textChanged.connect(self.invalidate)
        self.set_busy(False)

    def browse(self, field):
        path = QFileDialog.getExistingDirectory(self, self.tr('browse'), field.text() or str(Path.home()))
        if path:
            field.setText(path)

    def save_settings(self):
        for key, value in (('root', self.root.text()), ('recovery', self.recovery.text()),
                           ('excludes', self.excludes.toPlainText()), ('last_recovery', self.last_recovery)):
            self.settings.setValue('duplicates/' + key, value)
        self.settings.sync()

    def invalidate(self, *_):
        self.plan = None
        self.tree.clear()
        self.items = {}
        self.skipped.clear()
        self.skipped.hide()
        self.summary.setText(self.tr('dup_empty'))
        self.review_button.setEnabled(False)
        self.update_selection()

    def set_busy(self, busy):
        for widget in (self.inputs, self.excludes_group, self.scan_button, self.edit_bar, self.search):
            widget.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.close_button.setEnabled(not busy)
        self.review_button.setEnabled(not busy and self.plan is not None and any(a != 'skip' for a in self.plan.actions.values()))
        self.progress.setVisible(busy)
        self.file_progress.hide()
        if busy:
            self.progress.setRange(0, 0)
        self.update_selection()

    def launch(self, operation, function, callback):
        self.operation = operation
        self.set_busy(True)
        self.worker = Worker(function, self)
        self.worker.result.connect(callback)
        self.worker.progress.connect(self.show_progress)
        self.worker.failed.connect(self.failed)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def start_scan(self):
        if self._closing or self.worker is not None:
            return
        root = self.root.text().strip()
        if not root:
            QMessageBox.information(self, self.tr('dup_title'), self.tr('dup_empty'))
            return
        excludes = tuple(s.strip() for s in self.excludes.toPlainText().splitlines() if s.strip())
        self.invalidate()
        self.status.setText(self.tr('dup_scanning'))
        self.save_settings()
        self.launch('scan', lambda cancel, progress: core.find(root, excludes, cancel, progress), self.scanned)

    def scanned(self, plan):
        self.plan = plan
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        self.items = {}
        for index, group in enumerate(plan.groups):
            parent = QTreeWidgetItem(self.tree, [self.tr('dup_group', number=index + 1, count=len(group.files), size=size_text(group.size))])
            parent.setFirstColumnSpanned(True)
            parent.setData(0, Qt.ItemDataRole.UserRole, ('group', index))
            parent.setToolTip(0, 'SHA-256: ' + group.digest)
            for path in group.files:
                entry = plan.entries[path]
                item = QTreeWidgetItem(parent, [path, size_text(entry.size),
                    datetime.fromtimestamp(entry.sig[4] / 1e9).strftime('%Y-%m-%d %H:%M:%S'), ''])
                item.setData(0, Qt.ItemDataRole.UserRole, ('file', index, path))
                item.setToolTip(0, str(plan.root / path))
                self.items[path] = item
            parent.setExpanded(True)
        self.tree.setUpdatesEnabled(True)
        self.skipped.setText(self.tr('dup_skipped', links=plan.skipped_links, special=plan.skipped_special, hardlinks=plan.skipped_hardlinks))
        self.skipped.setVisible(bool(plan.skipped_links + plan.skipped_special + plan.skipped_hardlinks))
        self.refresh_actions()
        self.filter_rows()
        self.status.setText(self.tr('dup_ready') if plan.groups else self.tr('dup_none'))

    def refresh_actions(self):
        if self.plan is None:
            return
        keepers = {g.keeper for g in self.plan.groups}
        for path, item in self.items.items():
            keeper = path in keepers
            item.setText(3, self.tr('dup_keep') if keeper else self.tr(self.plan.actions[path]))
            font = item.font(0)
            font.setBold(keeper)
            item.setFont(0, font)
        copies = sum(len(g.files) - 1 for g in self.plan.groups)
        size = sum((len(g.files) - 1) * g.size for g in self.plan.groups)
        actions = sum(a != 'skip' for a in self.plan.actions.values())
        self.summary.setText(self.tr('dup_summary', files=sum(e.kind == 'file' for e in self.plan.entries.values()),
            groups=len(self.plan.groups), copies=copies, size=size_text(size), actions=actions))
        self.review_button.setEnabled(self.worker is None and actions > 0)
        self.update_selection()

    def filter_rows(self, *_):
        query = self.search.text().casefold()
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            any_visible = False
            for j in range(group.childCount()):
                item = group.child(j)
                visible = query in item.text(0).casefold()
                item.setHidden(not visible)
                any_visible |= visible
            group.setHidden(not any_visible)
        self.update_selection()

    def selected_paths(self):
        paths = set()
        for item in self.tree.selectedItems():
            if item.isHidden() or (item.parent() is not None and item.parent().isHidden()):
                continue
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data[0] == 'file':
                paths.add(data[2])
            else:
                for j in range(item.childCount()):
                    child = item.child(j)
                    if not child.isHidden():
                        paths.add(child.data(0, Qt.ItemDataRole.UserRole)[2])
        return paths

    def update_selection(self):
        paths = self.selected_paths() if self.plan else set()
        idle = self.worker is None
        self.keep_button.setEnabled(idle and len(paths) == 1)
        self.apply_button.setEnabled(idle and bool(paths))
        self.select_others.setEnabled(idle and self.plan is not None and bool(self.plan.groups))

    def keep_selected(self):
        paths = self.selected_paths()
        if self.plan is None or len(paths) != 1:
            return
        path = next(iter(paths))
        group = next(g for g in self.plan.groups if path in g.files)
        core.keep(self.plan, group, path)
        self.refresh_actions()

    def select_other_copies(self):
        if self.plan is None:
            return
        with QSignalBlocker(self.tree):
            self.tree.clearSelection()
            keepers = {g.keeper for g in self.plan.groups}
            for path, item in self.items.items():
                if path not in keepers and not item.isHidden() and not item.parent().isHidden():
                    item.setSelected(True)
        self.update_selection()

    def apply_actions(self):
        if self.plan is None:
            return
        keepers = {g.keeper for g in self.plan.groups}
        action = self.batch_action.currentData()
        count = protected = 0
        for path in self.selected_paths():
            if path in keepers and action != 'skip':
                protected += 1
            else:
                self.plan.actions[path] = action
                count += 1
        self.refresh_actions()
        self.status.setText(self.tr('dup_applied', count=count, protected=protected))

    def review(self):
        if self.plan is None or not any(a != 'skip' for a in self.plan.actions.values()):
            return
        plan, recovery = self.plan, self.recovery.text().strip()
        if DuplicateReview(plan, recovery, self.tr, self).exec() != QDialog.DialogCode.Accepted:
            return
        self.execution_root = str(plan.root)
        self.save_settings()
        self.launch('execute', lambda cancel, progress: core.execute(plan, recovery, cancel, progress,
            allow_permanent_delete=any(a == 'delete' for a in plan.actions.values())), self.executed)

    def show_progress(self, update):
        completed, total, detail = update
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(completed)
            key = 'action_progress' if self.operation == 'execute' else 'dup_scan_progress'
            self.progress.setFormat(self.tr(key, completed=completed, total=total) + ' · %p%')
        else:
            self.progress.setRange(0, 0)
        if isinstance(detail, engine.FileProgress):
            phase = self.tr('phase_' + detail.phase)
            self.status.setText(phase + (' · ' + detail.path if detail.path else ''))
            if detail.total > 0:
                self.file_progress.setRange(0, 1000)
                self.file_progress.setValue(min(1000, detail.processed * 1000 // detail.total))
                self.file_progress.setFormat(phase + ' · ' + self.tr('byte_progress', done=f'{detail.processed / 1024**2:.1f}', total=f'{detail.total / 1024**2:.1f}') + ' · %p%')
                self.file_progress.show()
            else:
                self.file_progress.hide()
        else:
            self.file_progress.hide()
            self.status.setText(str(detail))

    def executed(self, result):
        self.last_recovery = result['recovery']
        self.log.show()
        self.log.appendPlainText(self.tr('done', count=result['completed'], path=result['recovery']))
        self.changed.emit(self.execution_root)
        self.invalidate()
        self.pending_rescan = True

    def failed(self, error):
        self.pending_rescan = False
        if isinstance(error, core.CleanupError):
            self.last_recovery = error.recovery
        if self.operation == 'execute' and self.execution_root:
            self.changed.emit(self.execution_root)
        self.invalidate()
        if isinstance(error, engine.Cancelled) and self.operation == 'scan':
            self.status.setText(self.tr('dup_cancelled'))
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr('dup_title'))
        box.setTextFormat(Qt.TextFormat.PlainText)
        change = None
        if isinstance(error, engine.RecoveryFolderError):
            box.setText(self.tr('recovery_error'))
            box.setInformativeText(self.tr('dup_recovery_error', path=error.path or self.tr('recovery_not_set')))
            box.setDetailedText(error.detail)
            change = box.addButton(self.tr('recovery_change'), QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Ok)
        else:
            box.setText(self.tr('dup_scan_failed') if self.operation == 'scan' else self.tr('error'))
            box.setInformativeText(str(error))
        self.log.show()
        self.log.appendPlainText(str(error))
        box.exec()
        if change is not None and box.clickedButton() is change:
            self.recovery.setFocus()
            self.recovery.selectAll()

    def worker_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.operation = None
        self.set_busy(False)
        if self.pending_rescan:
            self.pending_rescan = False
            QTimer.singleShot(0, self.start_scan)

    def stop(self):
        if self.worker is not None:
            self.worker.cancel.set()
            self.stop_button.setEnabled(False)

    def open_recovery(self):
        path = self.last_recovery or self.recovery.text()
        if path and Path(path).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def show_help(self):
        from .manual import ManualWindow
        if self.manual_window is None:
            self.manual_window = ManualWindow(self, self.tr)
        self.manual_window.show()
        self.manual_window.go('duplicates')
        self.manual_window.raise_()
        self.manual_window.activateWindow()

    def _may_close(self):
        if self.worker is not None:
            QMessageBox.information(self, self.tr('dup_title'), self.tr('busy_close'))
            return False
        self._closing = True
        self.save_settings()
        if self.manual_window is not None:
            self.manual_window.close()
            self.manual_window.deleteLater()
            self.manual_window = None
        return True

    def reject(self):
        if self._may_close():
            super().reject()

    def accept(self):
        if self._may_close():
            super().accept()

    def closeEvent(self, event):
        if self._may_close():
            event.accept()
        else:
            event.ignore()
