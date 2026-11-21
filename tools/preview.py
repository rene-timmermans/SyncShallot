"""Render an offscreen UI preview using synthetic media files only."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from syncshallot.app import MainWindow, install_style
from syncshallot import engine

app = QApplication([])
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
    folder = Path(__file__).resolve().parents[1] / 'build'
    folder.mkdir(exist_ok=True)
    window.grab().save(str(folder / 'syncshallot-preview.png'))
    window.close()
