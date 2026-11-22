"""Render an offscreen UI preview using synthetic media files only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QPalette
from syncshallot.app import MainWindow, install_style
from syncshallot import engine

app = QApplication([])
if '--dark' in sys.argv:
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, '#202427'),
                        (QPalette.ColorRole.WindowText, '#eeeeee'),
                        (QPalette.ColorRole.Base, '#141719'),
                        (QPalette.ColorRole.AlternateBase, '#252a2e'),
                        (QPalette.ColorRole.Text, '#eeeeee'),
                        (QPalette.ColorRole.Button, '#33383d'),
                        (QPalette.ColorRole.ButtonText, '#eeeeee'),
                        (QPalette.ColorRole.PlaceholderText, '#969ea6'),
                        (QPalette.ColorRole.Mid, '#747b82'),
                        (QPalette.ColorRole.Midlight, '#41484f'),
                        (QPalette.ColorRole.Highlight, '#2aa8d3'),
                        (QPalette.ColorRole.HighlightedText, '#ffffff')):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor('#90979d'))
    app.setPalette(palette)
install_style(app)
with tempfile.TemporaryDirectory(prefix='syncshallot-preview-') as temporary:
    base = Path(temporary)
    left, right = base / 'Media-bron', base / 'USB-backup'
    left.mkdir()
    right.mkdir()
    for root, name, data in [
        (left, 'Films/Aurora (2025)/Aurora.2160p.mkv', b'new 4K version'),
        (right, 'Films/Aurora (2025)/Aurora.1080p.mkv', b'older version'),
        (left, 'Films/De Oversteek (2024)/De Oversteek.mkv', b'identical'),
        (right, 'Films/De Oversteek (2024)/De Oversteek.mkv', b'identical'),
        (left, 'Series/Noorderlicht/Seizoen 02/S02E01.mkv', b'new episode'),
        (left, 'Series/Noorderlicht/Seizoen 02/S02E02.mkv', b'new episode'),
        (left, 'Muziek/Studio Sessions/01 - Opening.flac', b'new master'),
        (right, 'Muziek/Studio Sessions/01 - Opening.flac', b'old master'),
        (right, 'Films/Archief/Verouderde versie.mkv', b'old media'),
    ]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    settings = QSettings(str(base / 'preview.ini'), QSettings.Format.IniFormat)
    settings.setValue('language', 'nl')
    window = MainWindow(settings)
    window.left.setText(str(left))
    window.right.setText(str(right))
    window.recovery.setText(str(base / 'Herstel-en-isolatie'))
    plan = engine.compare(left, right)
    window.compared(plan)
    window.review_button.setEnabled(True)
    window.resize(1600, 920)
    window.show()
    app.processEvents()
    if '--progress' in sys.argv:
        window.set_busy(True)
        window.summary.setText(window.tr('working'))
        window.show_progress((417, 1355, engine.FileProgress('isolate', 'Films/Archief/Verouderde versie.mkv', 768 * 1024**2, 2048 * 1024**2)))
        app.processEvents()
    capture = window
    if '--settings' in sys.argv:
        window.options_dialog.show()
        app.processEvents()
        capture = window.options_dialog
    if '--about' in sys.argv:
        from syncshallot.about import AboutDialog
        capture = AboutDialog(window, window.tr)
        capture.show()
        app.processEvents()
    if '--manual' in sys.argv:
        window.manual()
        capture = window.manual_window
        app.processEvents()
    duplicate_dialog = None
    if '--duplicates' in sys.argv:
        from syncshallot.duplicates_ui import DuplicateDialog
        from syncshallot import duplicates
        photos = base / 'Fotos'
        examples = [
            ('Albums/Vakantie/IMG_2048.jpg', b'holiday-photo' * 65536),
            ('Import/foto-001.jpg', b'holiday-photo' * 65536),
            ('Albums/Familie/verjaardag.jpg', b'birthday-photo' * 32768),
            ('Archief/feestfoto.jpg', b'birthday-photo' * 32768),
            ('Albums/Rondreis/zonsondergang.png', b'sunset-photo' * 131072),
            ('Downloads/mijnfoto.png', b'sunset-photo' * 131072),
            ('Backups/extra-kopie.png', b'sunset-photo' * 131072),
            ('Favorieten/uniek.png', b'unique-photo' * 8192),
        ]
        for name, data in examples:
            path = photos / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        duplicate_dialog = DuplicateDialog(window, window.tr, settings)
        duplicate_dialog.root.setText(str(photos))
        duplicate_dialog.recovery.setText(str(base / 'Herstel-en-isolatie'))
        duplicate_dialog.scanned(duplicates.find(photos))
        duplicate_dialog.show()
        app.processEvents()
        capture = duplicate_dialog
    folder = Path(__file__).resolve().parents[1] / 'build'
    folder.mkdir(exist_ok=True)
    suffix = ''.join('-' + key for key in ('dark', 'progress', 'settings', 'about', 'manual', 'duplicates') if '--' + key in sys.argv)
    capture.grab().save(str(folder / f'syncshallot-preview{suffix}.png'))
    if duplicate_dialog is not None:
        duplicate_dialog.reject()
    window.close()
