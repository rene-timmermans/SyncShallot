import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import time
from PySide6.QtCore import QSettings, Qt, QItemSelectionModel
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from syncshallot.app import MainWindow, Review, install_style
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


if __name__ == "__main__":
    unittest.main()
