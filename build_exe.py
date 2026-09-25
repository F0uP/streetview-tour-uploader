"""
Build the Windows app and its installer.

Steps:
  1. PyInstaller bundles run_editor.py plus the editor/ files into
     dist/StreetviewTourEditor/ (StreetviewTourEditor.exe + _internal/). The
     app runs from a tray icon, without a console window.
  2. That folder is zipped as the portable build:
     dist/StreetviewTourEditor-<version>-portable.zip
  3. If Inno Setup 6 is installed, installer/StreetviewTourEditor.iss turns it
     into a regular Windows installer:
     dist/StreetviewTourEditor-<version>-Setup.exe
  4. dist/SHA256SUMS.txt lists the checksums of both; the in-app updater
     refuses a downloaded Setup.exe that doesn't match it.

Nothing user-specific lives in the build: the app keeps each user's Google
credentials (client_secrets.json / token.json) in
%APPDATA%\\StreetviewTourEditor, so rebuilding, updating or reinstalling
never touches anyone's sign-in.

Code signing (optional): set SIGN_CMD to a command that signs one file, with
{file} where the path goes, e.g.
  set SIGN_CMD=signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a "{file}"
The app .exe is signed before packaging, and Inno Setup signs the Setup.exe
and its uninstaller with the same command. See README ("Code signing").

Usage:
  pip install -r requirements.txt pyinstaller
  python build_exe.py                  # version from `git describe`
  python build_exe.py --version 1.2.0  # explicit version (CI passes the tag)

Inno Setup (optional, only for the Setup.exe): https://jrsoftware.org/isdl.php
or `winget install JRSoftware.InnoSetup`.
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
APP_NAME = "StreetviewTourEditor"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
APP_DIR = DIST_DIR / APP_NAME
ICON = ROOT / "installer" / "app.ico"
ISS = ROOT / "installer" / f"{APP_NAME}.iss"
VERSION_MODULE = ROOT / "_app_version.py"


def detect_version():
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Without any tag, describe prints just the commit hash.
        return out.lstrip("v") if re.match(r"v?\d+\.\d+", out) else f"0.0.0-dev+{out}"
    except (OSError, subprocess.CalledProcessError):
        return "0.0.0-dev"


def find_iscc():
    found = shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
            if candidate.exists():
                return str(candidate)
    return None


def sign(path):
    cmd = os.environ.get("SIGN_CMD")
    if not cmd:
        return
    print(f"Signing {path.name} ...")
    subprocess.run(cmd.replace("{file}", str(path)), shell=True, check=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="version string, e.g. 1.2.0 (default: git describe)")
    parser.add_argument("--no-installer", action="store_true", help="skip the Inno Setup step")
    parser.add_argument("--console", action="store_true", help="build with a console window (debugging)")
    args = parser.parse_args()
    version = (args.version or detect_version()).lstrip("v")

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found. Install it first:")
        print("  pip install pyinstaller")
        sys.exit(1)

    print(f"Building {APP_NAME} {version} ...")
    VERSION_MODULE.write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    try:
        subprocess.run([
            sys.executable, "-m", "PyInstaller",
            "--onedir",
            "--console" if args.console else "--windowed",
            "--name", APP_NAME,
            "--icon", str(ICON),
            "--add-data", f"{ROOT / 'editor'}{os.pathsep}editor",
            "--add-data", f"{ICON}{os.pathsep}.",
            # pystray picks its platform backend at runtime, invisible to PyInstaller.
            "--hidden-import", "pystray._win32",
            "--distpath", str(DIST_DIR),
            "--workpath", str(BUILD_DIR),
            "--specpath", str(BUILD_DIR),
            "--noconfirm",
            str(ROOT / "run_editor.py"),
        ], check=True)
    finally:
        VERSION_MODULE.unlink(missing_ok=True)

    sign(APP_DIR / f"{APP_NAME}.exe")

    for old in DIST_DIR.glob(f"{APP_NAME}-*"):
        old.unlink()
    outputs = []
    portable = DIST_DIR / f"{APP_NAME}-{version}-portable"
    print(f"Zipping {portable.name}.zip ...")
    outputs.append(Path(shutil.make_archive(str(portable), "zip", DIST_DIR, APP_NAME)))

    iscc = None if args.no_installer else find_iscc()
    if iscc:
        print("Building installer ...")
        cmd = [
            iscc,
            f"/DAppVersion={version}",
            f"/DSourceDir={APP_DIR}",
            f"/DOutputDir={DIST_DIR}",
            f"/DIconFile={ICON}",
        ]
        if os.environ.get("SIGN_CMD"):
            # Inno Setup's sign tool syntax: $f is the (already quoted) file, $q a quote.
            inno_cmd = (os.environ["SIGN_CMD"].replace('"{file}"', "$f").replace("{file}", "$f")
                        .replace('"', "$q"))
            cmd += [f"/Ssigncmd={inno_cmd}", "/DSign"]
        subprocess.run(cmd + [str(ISS)], check=True)
        outputs.append(DIST_DIR / f"{APP_NAME}-{version}-Setup.exe")
    elif not args.no_installer:
        print("\nInno Setup not found - skipped the Setup.exe.")
        print("Install it (winget install JRSoftware.InnoSetup) and rerun to build the installer.")

    sums = DIST_DIR / "SHA256SUMS.txt"
    sums.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in outputs), encoding="utf-8")

    print("\nDone:")
    print(f"  App:  {APP_DIR / (APP_NAME + '.exe')}")
    for p in outputs + [sums]:
        print(f"  {p.name}")


if __name__ == "__main__":
    main()
