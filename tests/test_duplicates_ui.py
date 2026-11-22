import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QTableWidget

from syncshallot import duplicates, engine
from syncshallot.app import MainWindow, install_style
from syncshallot.duplicates_ui import DuplicateDialog, DuplicateReview
from syncshallot.i18n import LANGUAGES, Translator


class DuplicateInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        install_style(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'photos'
        self.root.mkdir()
        for name, data in (('album/a.jpg', b'photo-one'), ('imports/copy-a.jpg', b'photo-one'),
                           ('album/b.jpg', b'photo-two'), ('imports/copy-b.jpg', b'photo-two'),
                           ('unique.jpg', b'a unique photo')):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.settings = QSettings(str(self.base / 'settings.ini'), QSettings.Format.IniFormat)
        self.main = MainWindow(self.settings)
        self.dialog = DuplicateDialog(self.main, Translator('nl'), self.settings)
        self.dialog.root.setText(str(self.root))
        self.dialog.recovery.setText(str(self.base / 'recovery'))
        self.plan = duplicates.find(self.root)
        self.dialog.scanned(self.plan)

    def tearDown(self):
        if self.dialog.worker is not None:
            self.dialog.stop()
            self.wait_worker()
        self.dialog.reject()
        self.dialog.deleteLater()
        self.main.close()
        self.main.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_worker(self):
        deadline = time.monotonic() + 5
        while self.dialog.worker is not None or self.dialog.pending_rescan:
            self.app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                self.fail('Duplicate worker did not finish')

    def test_scan_results_start_without_cleanup_actions(self):
        self.assertEqual(self.dialog.tree.topLevelItemCount(), 2)
        self.assertTrue(all(a == 'skip' for a in self.plan.actions.values()))
        self.assertFalse(self.dialog.review_button.isEnabled())
        self.assertIn('0 acties', self.dialog.summary.text())
        self.assertFalse(any((self.base / 'recovery').glob('*')))

    def test_select_all_and_isolate_keeps_one_protected_file_in_each_group(self):
        self.dialog.tree.selectAll()
        self.dialog.batch_action.setCurrentIndex(1)
        self.dialog.apply_button.click()
        self.assertEqual(sum(a == 'isolate' for a in self.plan.actions.values()), 2)
        for group in self.plan.groups:
            self.assertEqual(self.plan.actions[group.keeper], 'skip')
            self.assertEqual(self.dialog.items[group.keeper].text(3), 'Bewaren · beschermd')
        self.assertTrue(self.dialog.review_button.isEnabled())
        self.assertIn('2 bewaarde exemplaren', self.dialog.status.text())

    def test_chosen_keeper_cannot_keep_its_previous_delete_action(self):
        group = self.plan.groups[0]
        old = group.keeper
        new = next(p for p in group.files if p != old)
        self.plan.actions[new] = 'delete'
        self.dialog.items[new].setSelected(True)
        self.dialog.keep_button.click()
        self.assertEqual(group.keeper, new)
        self.assertEqual(self.plan.actions[new], 'skip')
        self.assertEqual(self.plan.actions[old], 'skip')
        self.assertEqual(self.dialog.items[new].text(3), 'Bewaren · beschermd')

    def test_select_other_copies_respects_search_filter(self):
        self.dialog.search.setText('copy-a')
        self.dialog.select_other_copies()
        self.assertEqual(self.dialog.selected_paths(), {'imports/copy-a.jpg'})
        self.dialog.batch_action.setCurrentIndex(1)
        self.dialog.apply_actions()
        self.assertEqual(self.plan.actions['imports/copy-a.jpg'], 'isolate')
        self.assertEqual(self.plan.actions['imports/copy-b.jpg'], 'skip')

    def test_review_names_the_keeper_and_requires_acknowledgment_and_delete_phrase(self):
        self.plan.actions['imports/copy-a.jpg'] = 'delete'
        review = DuplicateReview(self.plan, str(self.base / 'recovery'), Translator('nl'))
        table = review.findChild(QTableWidget)
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 2).text(), 'album/a.jpg')
        self.assertFalse(review.run_button.isEnabled())
        review.ack.setChecked(True)
        self.assertFalse(review.run_button.isEnabled())
        review.deletion.setText('DELETE')
        self.assertTrue(review.run_button.isEnabled())
        review.deletion.setText('delete')
        self.assertFalse(review.run_button.isEnabled())
        review.close()
        review.deleteLater()

    def test_full_async_isolation_rescan_and_main_plan_invalidation(self):
        target = self.base / 'backup'
        target.mkdir()
        self.main.compared(engine.compare(self.root, target))
        self.dialog.changed.connect(self.main.duplicates_changed)
        with patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Ok):
            self.dialog.start_scan()
            self.wait_worker()
            self.assertIsNotNone(self.dialog.plan)
            self.dialog.select_other_copies()
            self.dialog.batch_action.setCurrentIndex(1)
            self.dialog.apply_actions()
            with patch.object(DuplicateReview, 'exec', return_value=QDialog.DialogCode.Accepted):
                self.dialog.review()
            self.wait_worker()
        self.assertIsNotNone(self.dialog.plan)
        self.assertEqual(self.dialog.plan.groups, [])
        self.assertTrue((self.root / 'album/a.jpg').exists())
        self.assertTrue((self.root / 'album/b.jpg').exists())
        self.assertTrue((self.root / 'unique.jpg').exists())
        self.assertFalse((self.root / 'imports/copy-a.jpg').exists())
        self.assertTrue((Path(self.dialog.last_recovery) / 'isolated/imports/copy-a.jpg').exists())
        self.assertIsNone(self.main.plan)
        self.assertEqual(list(target.iterdir()), [])

    def test_unrelated_duplicate_cleanup_does_not_clear_unrelated_comparison(self):
        a, b = self.base / 'other-a', self.base / 'other-b'
        a.mkdir()
        b.mkdir()
        plan = engine.compare(a, b)
        self.main.compared(plan)
        self.main.duplicates_changed(str(self.root))
        self.assertIs(self.main.plan, plan)

    def test_close_and_escape_cannot_abandon_running_worker(self):
        gate = threading.Event()
        self.dialog.show()
        self.dialog.launch('scan', lambda *_: (gate.wait(3), self.plan)[1], self.dialog.scanned)
        try:
            with patch.object(QMessageBox, 'information'):
                self.dialog.reject()
                self.assertFalse(self.dialog._closing)
                self.dialog.close()
                self.assertFalse(self.dialog._closing)
        finally:
            gate.set()
            self.wait_worker()

    def test_main_menu_and_button_open_separate_tool_and_disable_during_sync(self):
        self.assertIn(self.main.duplicates_action, self.main.file_actions)
        with patch.object(DuplicateDialog, 'exec', return_value=QDialog.DialogCode.Rejected) as opened:
            self.main.duplicates_button.click()
            opened.assert_called_once()
        self.main.set_busy(True)
        self.assertFalse(self.main.duplicates_button.isEnabled())
        self.assertFalse(self.main.duplicates_action.isEnabled())
        self.main.set_busy(False)

    def test_changing_single_root_clears_only_duplicate_results(self):
        self.dialog.root.setText(str(self.base))
        self.assertIsNone(self.dialog.plan)
        self.assertEqual(self.dialog.tree.topLevelItemCount(), 0)

    def test_duplicate_help_opens_its_own_manual_topic(self):
        self.dialog.show_help()
        manual = self.dialog.manual_window
        self.assertEqual(manual.topics[manual.index], 'duplicates')
        self.assertFalse(manual.isModal())

    def test_all_five_languages_can_render_duplicate_scan_and_review(self):
        for language in LANGUAGES:
            dialog = DuplicateDialog(self.main, Translator(language), self.settings)
            dialog.scanned(self.plan)
            self.assertEqual(dialog.tree.topLevelItemCount(), 2)
            self.assertIn('2', dialog.summary.text())
            dialog.reject()
            dialog.deleteLater()
        self.app.processEvents()
