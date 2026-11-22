"""Installed-package check; creates only isolated temporary configuration and data."""
from pathlib import Path
import tempfile
from PySide6.QtCore import QSettings
from . import __version__, engine
from .app import MainWindow
from .about import AboutDialog
from .manual import ManualWindow, load_manual
from .i18n import LANGUAGES
from . import duplicates
from .duplicates_ui import DuplicateDialog


def run(app):
    with tempfile.TemporaryDirectory(prefix="syncshallot-selftest-") as temporary:
        base = Path(temporary)
        source, target = base / "source", base / "target"
        source.mkdir()
        target.mkdir()
        (source / "movie-4k.mkv").write_bytes(b"new media")
        (source / "updated.mkv").write_bytes(b"new master")
        (target / "updated.mkv").write_bytes(b"old master")
        (target / "movie-720p.mkv").write_bytes(b"old media")
        original = engine.scan(source, True)
        plan = engine.compare(source, target)
        for row in plan.rows:
            if engine.is_extra(plan, row):
                row.action = "isolate"
        result = engine.execute(plan, base / "recovery")
        if engine.scan(source, True) != original:
            raise RuntimeError("Selftest: source was changed")
        if not all(r.status == "same" for r in engine.compare(source, target).rows):
            raise RuntimeError("Selftest: synchronized data differs")
        recovery = Path(result["recovery"])
        if (recovery / "isolated/right/movie-720p.mkv").read_bytes() != b"old media":
            raise RuntimeError("Selftest: isolated file missing")
        if (recovery / "overwritten/right/updated.mkv").read_bytes() != b"old master":
            raise RuntimeError("Selftest: recovery copy missing")
        window = MainWindow(QSettings(str(base / "settings.ini"), QSettings.Format.IniFormat))
        window.show()
        app.processEvents()
        if window.windowIcon().isNull() or window.unlock.isChecked() or window.mode.currentData() != "left_to_right":
            raise RuntimeError("Selftest: icon or default protection failed")
        window.compared(engine.compare(source, target))
        for language in LANGUAGES:
            if len(load_manual(language)['sections']) != 13:
                raise RuntimeError("Selftest: offline manual is incomplete")
        manual = ManualWindow(window, window.tr)
        manual.search.setText('SHA-256')
        if not manual.find():
            raise RuntimeError("Selftest: manual search failed")
        about = AboutDialog(window, window.tr)
        if about.mascot.pixmap().isNull() or __version__ not in about.description.text():
            raise RuntimeError("Selftest: About window failed")
        manual.close()
        manual.deleteLater()
        about.close()
        about.deleteLater()
        photos = base / 'photos'
        (photos / 'album').mkdir(parents=True)
        (photos / 'imports').mkdir()
        (photos / 'album/original.jpg').write_bytes(b'identical photo')
        (photos / 'imports/renamed.jpg').write_bytes(b'identical photo')
        duplicate_plan = duplicates.find(photos)
        if len(duplicate_plan.groups) != 1 or any(a != 'skip' for a in duplicate_plan.actions.values()):
            raise RuntimeError('Selftest: duplicate groups or safe defaults failed')
        duplicate_dialog = DuplicateDialog(window, window.tr, QSettings(str(base / 'duplicate-settings.ini'), QSettings.Format.IniFormat))
        duplicate_dialog.root.setText(str(photos))
        duplicate_dialog.scanned(duplicate_plan)
        if duplicate_dialog.tree.topLevelItemCount() != 1:
            raise RuntimeError('Selftest: duplicate interface failed')
        duplicate_dialog.reject()
        duplicate_dialog.deleteLater()
        duplicate_plan.actions['imports/renamed.jpg'] = 'isolate'
        duplicate_result = duplicates.execute(duplicate_plan, base / 'duplicate-recovery')
        if not (photos / 'album/original.jpg').exists() or (photos / 'imports/renamed.jpg').exists():
            raise RuntimeError('Selftest: duplicate cleanup failed')
        if (Path(duplicate_result['recovery']) / 'isolated/imports/renamed.jpg').read_bytes() != b'identical photo':
            raise RuntimeError('Selftest: duplicate recovery failed')
        app.processEvents()
        window.close()
        window.deleteLater()
        app.processEvents()
    print(f"SyncShallot {__version__}: installed interface, source protection, synchronization, isolation, recovery, duplicates and offline help OK")
    return 0
