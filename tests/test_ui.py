import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import time
from PySide6.QtCore import QSettings, Qt, QItemSelectionModel, QPoint
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from syncshallot.app import MainWindow, Review, Worker, install_style
from syncshallot.i18n import LANGUAGES, TEXT, Translator
from syncshallot import engine


class InterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        install_style(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.settings = QSettings(str(self.base / "settings.ini"), QSettings.Format.IniFormat)
        self.window = MainWindow(self.settings)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def plan(self):
        a, b = self.base / "nas", self.base / "usb"
        a.mkdir()
        b.mkdir()
        (a / "film-4k").write_bytes(b"new")
        (b / "film-720p").write_bytes(b"old")
        return engine.compare(a, b)

    def test_all_languages_render_and_have_equal_format_fields(self):
        from string import Formatter
        for key, translations in TEXT.items():
            self.assertEqual(len(translations), 5, key)
            fields = [{f for _, f, _, _ in Formatter().parse(text) if f} for text in translations]
            self.assertTrue(all(f == fields[0] for f in fields), key)
        for lang in LANGUAGES:
            self.settings.setValue("language", lang)
            window = MainWindow(self.settings)
            window.show()
            self.app.processEvents()
            self.assertEqual(window.tr.language, lang)
            self.assertFalse(window.unlock.isChecked())
            window.close()
            window.deleteLater()

    def test_protection_defaults_and_unlock(self):
        self.assertFalse(self.window.mode.model().item(1).isEnabled())
        self.assertFalse(self.window.mode.model().item(2).isEnabled())
        self.window.unlock.setChecked(True)
        self.assertTrue(self.window.mode.model().item(2).isEnabled())
        self.window.mode.setCurrentIndex(2)
        self.window.unlock.setChecked(False)
        self.assertEqual(self.window.mode.currentData(), "left_to_right")

    def test_stable_window_has_no_preview_label_even_with_old_environment(self):
        with patch.dict(os.environ, {'SYNCSHALLOT_PREVIEW': '1'}):
            window = MainWindow(self.settings)
            self.assertEqual(window.windowTitle(), 'SyncShallot')
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def test_dual_panes_filter_and_selection_are_linked(self):
        plan = self.plan()
        self.window.compared(plan)
        self.assertIs(self.window.views[0].selectionModel(), self.window.views[1].selectionModel())
        self.window.filter.setCurrentIndex(2)
        self.assertEqual(self.window.proxy.rowCount(), 1)
        self.window.views[0].selectionModel().select(self.window.proxy.index(0, 0), QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self.window.batch_action.setCurrentIndex(3)
        self.window.apply_actions()
        self.assertEqual(next(r for r in plan.rows if r.path == "film-720p").action, "isolate")

    def test_review_cannot_execute_without_ack_or_delete_phrase(self):
        plan = self.plan()
        next(r for r in plan.rows if r.path == "film-720p").action = "delete"
        dialog = Review(plan, str(self.base / "history"), Translator("nl"))
        self.assertFalse(dialog.run_button.isEnabled())
        dialog.ack.setChecked(True)
        self.assertFalse(dialog.run_button.isEnabled())
        dialog.deletion.setText("DELETE")
        self.assertTrue(dialog.run_button.isEnabled())
        dialog.deletion.setText("")
        self.assertFalse(dialog.run_button.isEnabled())
        dialog.close()

    def test_settings_change_invalidates_old_plan(self):
        self.window.compared(self.plan())
        self.window.left.setText("/different/path")
        self.assertIsNone(self.window.plan)
        self.assertEqual(self.window.model.rowCount(), 0)
        self.assertFalse(self.window.review_button.isEnabled())

    def wait_worker(self):
        deadline = time.monotonic() + 5
        while self.window.worker is not None or self.window.pending_rescan:
            self.app.processEvents()
            QTest.qWait(5)
            if time.monotonic() > deadline:
                self.fail("Worker did not finish")

    def test_full_async_compare_execute_and_rescan(self):
        plan = self.plan()
        self.window.left.setText(str(plan.left))
        self.window.right.setText(str(plan.right))
        self.window.recovery.setText(str(self.base / "history"))
        self.window.start_compare()
        self.wait_worker()
        self.assertIsNotNone(self.window.plan)
        next(r for r in self.window.plan.rows if r.path == "film-720p").action = "isolate"
        with patch.object(Review, "exec", return_value=Review.DialogCode.Accepted):
            self.window.review()
        self.wait_worker()
        self.assertIsNotNone(self.window.plan)
        self.assertTrue(all(r.status == "same" for r in self.window.plan.rows))
        self.assertFalse((plan.right / "film-720p").exists())
        self.assertTrue((plan.right / "film-4k").exists())

    def test_path_columns_can_be_dragged_even_while_busy(self):
        self.window.show()
        self.app.processEvents()
        self.window.set_busy(True)
        for side, column in ((0, 0), (1, 3)):
            view = self.window.views[side]
            header = view.horizontalHeader()
            old = header.sectionSize(column)
            boundary = header.sectionViewportPosition(column) + old - 1
            start = QPoint(boundary, header.height() // 2)
            end = start + QPoint(70, 0)
            QTest.mousePress(header.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(header.viewport(), end)
            QTest.mouseRelease(header.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertGreater(header.sectionSize(column), old + 40)

    def test_column_widths_survive_restart(self):
        self.window.show()
        self.app.processEvents()
        self.window.views[0].setColumnWidth(0, 473)
        self.window.views[1].setColumnWidth(3, 527)
        self.window.save_settings()
        restored = MainWindow(self.settings)
        restored.show()
        self.app.processEvents()
        self.assertEqual(restored.views[0].columnWidth(0), 473)
        self.assertEqual(restored.views[1].columnWidth(3), 527)
        restored.close()
        restored.deleteLater()

    def test_execution_progress_has_real_counts_and_large_file_percentage(self):
        self.window.set_busy(True)
        gigabyte = 1024 ** 3
        detail = engine.FileProgress("isolate", "large.mkv", 3 * gigabyte, 12 * gigabyte)
        self.window.show_progress((17, 40, detail))
        self.assertEqual(self.window.progress.maximum(), 40)
        self.assertEqual(self.window.progress.value(), 17)
        self.assertIn("17 van 40", self.window.progress.format())
        self.assertEqual(self.window.file_progress.value(), 250)
        self.assertFalse(self.window.file_progress.isHidden())
        self.window.show_progress((40, 40, "last.mkv"))
        self.assertEqual(self.window.progress.value(), 40)
        self.assertTrue(self.window.file_progress.isHidden())

    def test_scan_has_no_invented_overall_percentage(self):
        self.window.set_busy(True)
        self.window.show_progress((None, None, engine.FileProgress("scan", "file", 25, 100)))
        self.assertEqual(self.window.progress.maximum(), 0)
        self.assertIn("totaal nog onbekend", self.window.progress_label.text())
        self.assertEqual(self.window.file_progress.value(), 250)

    def test_worker_preserves_numbers_and_always_reports_final_completion(self):
        worker = Worker(lambda *_: None)
        updates = []
        worker.progress.connect(updates.append)
        worker.last_progress = time.monotonic()
        worker.report(1355, 1355, "last")
        self.assertEqual(updates, [(1355, 1355, "last")])
        worker.deleteLater()

    def test_settings_and_language_are_in_options_menu_not_above_results(self):
        self.window.show()
        self.app.processEvents()
        self.assertTrue(self.window.options_dialog.isAncestorOf(self.window.unlock))
        self.assertTrue(self.window.options_dialog.isAncestorOf(self.window.recovery))
        self.assertTrue(self.window.options_dialog.isAncestorOf(self.window.excludes))
        self.assertFalse(self.window.unlock.isVisible())
        self.assertFalse(self.window.scan_note.isVisible())
        self.assertIn(self.window.settings_action, self.window.options_menu.actions())
        self.assertIn(self.window.language_menu.menuAction(), self.window.options_menu.actions())
        self.assertEqual(set(self.window.language_actions), set(LANGUAGES))
        self.window.set_busy(True)
        self.assertFalse(self.window.options_menu.isEnabled())
        self.window.set_busy(False)
        self.assertTrue(self.window.options_menu.isEnabled())

    def test_language_menu_changes_language_and_keeps_protection_default(self):
        self.window.language_actions['en'].trigger()
        replacement = self.app.window
        self.assertEqual(replacement.tr.language, 'en')
        self.assertFalse(replacement.unlock.isChecked())
        self.assertEqual(replacement.mode.currentData(), 'left_to_right')
        replacement.close()
        replacement.deleteLater()
        self.app.window = None

    def test_help_menu_f1_opens_reusable_nonmodal_manual_during_task(self):
        self.assertEqual(self.window.manual_action.shortcut().toString(), 'F1')
        self.window.set_busy(True)
        self.window.manual_action.trigger()
        manual = self.window.manual_window
        self.assertIsNotNone(manual)
        self.assertFalse(manual.isModal())
        self.assertTrue(manual.isVisible())
        manual.close()
        self.window.manual_action.trigger()
        self.assertIs(self.window.manual_window, manual)
        self.assertTrue(manual.isVisible())


if __name__ == "__main__":
    unittest.main()
