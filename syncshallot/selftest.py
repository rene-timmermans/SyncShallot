"""Installed-package check; creates only isolated temporary configuration and data."""
from pathlib import Path
import tempfile
from PySide6.QtCore import QSettings
from . import __version__, engine
from .app import MainWindow
from .about import AboutDialog
from .manual import ManualWindow, load_manual
from .i18n import LANGUAGES


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
            if len(load_manual(language)['sections']) != 12:
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
        app.processEvents()
        window.close()
        window.deleteLater()
        app.processEvents()
    print(f"SyncShallot {__version__}: installed interface, source protection, synchronization, isolation, recovery and offline help OK")
    return 0
