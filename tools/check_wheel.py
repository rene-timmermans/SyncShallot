"""Smoke-test an unpacked wheel in an isolated process without a package installer."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile
import tomllib

project = Path(__file__).resolve().parents[1]
version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
wheel = project / "dist" / f"syncshallot-{version}-py3-none-any.whl"
with tempfile.TemporaryDirectory(prefix="syncshallot-wheel-") as temporary:
    target = Path(temporary)
    with ZipFile(wheel) as archive:
        archive.extractall(target)
    environment = dict(os.environ, PYTHONPATH=str(target), QT_QPA_PLATFORM="offscreen",
                       XDG_CONFIG_HOME=str(target / "config"))
    subprocess.run([sys.executable, "-c", """
from pathlib import Path
from PySide6.QtWidgets import QApplication
from syncshallot.app import MainWindow, install_style, ICON
assert ICON.is_file()
app = QApplication([])
install_style(app)
window = MainWindow()
window.show()
app.processEvents()
assert not window.windowIcon().isNull()
assert window.mode.currentData() == 'left_to_right'
assert not window.unlock.isChecked()
window.close()
from syncshallot.selftest import run
assert run(app) == 0
print('Unpacked wheel: startup, source protection, offline manuals and About window OK')
"""], cwd=target, env=environment, check=True)
