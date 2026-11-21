"""Shared package payload for Debian, Fedora and Arch; only writes into DESTDIR."""
from pathlib import Path
import shutil
import sys

project = Path(__file__).resolve().parents[1]
destination = Path(sys.argv[1]).resolve()
if destination == Path("/") or destination == project or destination in project.parents:
    raise SystemExit("Choose a separate package staging directory")


def install(relative, source, mode=0o644):
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(project / source, target)
    target.chmod(mode)


package = destination / "usr/share/syncshallot/syncshallot"
package.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(project / "syncshallot", package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
for file in package.rglob("*"):
    file.chmod(0o755 if file.is_dir() else 0o644)
install("usr/bin/syncshallot", "packaging/syncshallot-launcher.sh", 0o755)
install("usr/share/applications/syncshallot.desktop", "packaging/syncshallot.desktop")
install("usr/share/icons/hicolor/256x256/apps/syncshallot.png", "syncshallot/assets/syncshallot.png")
install("usr/share/doc/syncshallot/README.md", "README.md")
install("usr/share/doc/syncshallot/CHANGELOG.md", "CHANGELOG.md")
install("usr/share/licenses/syncshallot/LICENSE", "LICENSE")
