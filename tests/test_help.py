import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from syncshallot import __version__
from syncshallot.about import AboutDialog, WEBSITE
from syncshallot.i18n import LANGUAGES, Translator
from syncshallot.manual import ManualWindow, load_manual


class HelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_all_languages_have_complete_offline_topics_and_working_anchors(self):
        reference = [section['anchor'] for section in load_manual('nl')['sections']]
        self.assertEqual(len(reference), len(set(reference)))
        for language in LANGUAGES:
            data = load_manual(language)
            self.assertEqual([s['anchor'] for s in data['sections']], reference)
            self.assertTrue(all(len(s['body']) > 100 for s in data['sections']))
            manual = ManualWindow(tr=Translator(language))
            self.assertEqual(manual.topics, reference)
            self.assertEqual(manual.positions, sorted(manual.positions))
            self.assertIn('SHA-256', manual.browser.toPlainText())
            self.assertIn('DELETE', manual.browser.toPlainText())
            self.assertIn('isolated/right', manual.browser.toPlainText())
            self.assertFalse(manual.isModal())
            manual.close()
            manual.deleteLater()
        self.app.processEvents()

    def test_contents_links_and_previous_next_navigate_topics(self):
        manual = ManualWindow(tr=Translator('nl'))
        manual.show()
        self.app.processEvents()
        manual.browser.anchorClicked.emit(QUrl('#recovery'))
        self.assertEqual(manual.topics[manual.index], 'recovery')
        manual.next_button.click()
        self.assertEqual(manual.topics[manual.index], 'options')
        manual.previous_button.click()
        self.assertEqual(manual.topics[manual.index], 'recovery')
        manual.contents_button.click()
        self.assertEqual(manual.index, -1)
        self.assertFalse(manual.previous_button.isEnabled())
        manual.next_button.click()
        self.assertEqual(manual.index, 0)
        manual.go('problems')
        self.assertFalse(manual.next_button.isEnabled())
        manual.close()
        manual.deleteLater()
        self.app.processEvents()

    def test_search_highlights_and_wraps_in_both_directions(self):
        manual = ManualWindow(tr=Translator('nl'))
        manual.search.setText('herstelkopie')
        self.assertGreater(len(manual.browser.extraSelections()), 1)
        manual.browser.moveCursor(QTextCursor.MoveOperation.End)
        self.assertTrue(manual.find())
        first = manual.browser.textCursor().selectionStart()
        self.assertTrue(manual.find(True))
        last = manual.browser.textCursor().selectionStart()
        self.assertGreater(last, first)
        manual.search.setText('this-term-does-not-exist-42')
        self.assertFalse(manual.find())
        self.assertIn('Niet gevonden', manual.status.text())
        manual.search.clear()
        self.assertEqual(len(manual.browser.extraSelections()), 0)
        manual.close()
        manual.deleteLater()
        self.app.processEvents()

    def test_about_matches_family_information_in_all_languages(self):
        for language in LANGUAGES:
            tr = Translator(language)
            dialog = AboutDialog(tr=tr)
            self.assertFalse(dialog.mascot.pixmap().isNull())
            text = dialog.description.text()
            for required in (__version__, 'René Timmermans', 'Codex (OpenAI)', 'GNU General Public License', WEBSITE, 'Python', 'Qt', 'PySide6'):
                self.assertIn(required, text)
            self.assertIn(tr('about_author'), text)
            self.assertTrue(dialog.description.openExternalLinks())
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()
