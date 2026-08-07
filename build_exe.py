"""
Build a standalone Windows .exe of the tour editor with PyInstaller.

Layout of the result:

  dist/
  |-- editor/                     (plain files, safe to overwrite each build)
  |-- uploader/                   (your client_secrets.json / token.json)
  `-- StreetviewTourEditor/
      |-- StreetviewTourEditor.exe
      `-- _internal/              (PyInstaller runtime files)

editor/ and uploader/ are kept OUTSIDE the StreetviewTourEditor/ folder on
purpose: PyInstaller clears everything inside the app folder it builds into
on every run, so anything placed there - including a real client_secrets.json
- would be deleted the next time this script runs. Living one level up keeps
them untouched by rebuilds.

Usage:
  pip install pyinstaller
  python build_exe.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
APP_NAME = "StreetviewTourEditor"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
PYINSTALLER_OUT = DIST_DIR / APP_NAME
EDITOR_DEST = DIST_DIR / "editor"
UPLOADER_DEST = DIST_DIR / "uploader"


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found. Install it first:")
        print("  pip install pyinstaller")
        sys.exit(1)

    print("Building executable...")
    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--onedir",
        "--name", APP_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        "--noconfirm",
        str(ROOT / "run_editor.py"),
    ], check=True)

    print("Refreshing editor/ ...")
    if EDITOR_DEST.exists():
        shutil.rmtree(EDITOR_DEST)
    shutil.copytree(ROOT / "editor", EDITOR_DEST)

    UPLOADER_DEST.mkdir(exist_ok=True)
    readme = UPLOADER_DEST / "PUT_client_secrets.json_HERE.txt"
    if not readme.exists():
        readme.write_text(
            "Save your OAuth desktop client credentials (from Google Cloud Console)\n"
            "in this folder as client_secrets.json - see the README's\n"
            "'One-time Google setup' section for how to create them.\n\n"
            "After you sign in once through the app, token.json also appears here.\n"
            "Both files are specific to you - do not share or commit them.\n",
            encoding="utf-8",
        )

    print(f"\nDone. Run: {PYINSTALLER_OUT / (APP_NAME + '.exe')}")
    print(f"Put client_secrets.json in: {UPLOADER_DEST}")


if __name__ == "__main__":
    main()
