"""
Build the desktop app for Windows or macOS, with its installer.
(The Linux package is built by packaging/linux/build_deb.py.)

Windows - run with the Python whose architecture you build for (x64 or x86):
  1. PyInstaller bundles run_editor.py plus the editor/ files into
     dist/StreetviewTourEditor/ (StreetviewTourEditor.exe + _internal/).
  2. That folder is zipped as the portable build:
     dist/StreetviewTourEditor-<version>-windows-<arch>-portable.zip
  3. If Inno Setup 6 is installed, installer/StreetviewTourEditor.iss turns it
     into a regular Windows installer:
     dist/StreetviewTourEditor-<version>-Setup.exe       (x64, also for ARM64 PCs)
     dist/StreetviewTourEditor-<version>-Setup-x86.exe   (32-bit Windows)
     The x64 name is the one the in-app updater of every version looks for.

macOS - run on a Mac of the architecture you build for (arm64 or x86_64):
  1. PyInstaller builds "dist/Streetview Tour Editor.app", which also opens
     .vrtour files from Finder.
  2. It goes into a disk image: dist/StreetviewTourEditor-<version>-macos-<arm64|x64>.dmg

dist/SHA256SUMS.txt lists the checksums; the in-app updater refuses a
downloaded Setup.exe that doesn't match the one published with the release.

Nothing user-specific lives in the build: each user's Google sign-in and
settings are kept in their own profile (see run_editor.py), so rebuilding,
updating or reinstalling never touches them.

Code signing (optional):
  Windows: set SIGN_CMD to a command that signs one file, with {file} where
    the path goes, e.g.
      set SIGN_CMD=signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a "{file}"
    The app .exe is signed before packaging, and Inno Setup signs the
    Setup.exe and its uninstaller with the same command.
  macOS: set MACOS_SIGN_IDENTITY to a "Developer ID Application: ..." identity
    in the keychain; with APPLE_ID, APPLE_TEAM_ID and APPLE_APP_PASSWORD also
    set, the disk image is notarized by Apple too. Without an identity the app
    gets an ad-hoc signature (Gatekeeper then asks once before opening it).

Usage:
  pip install -r requirements-build.txt
  python build_exe.py                  # version from `git describe`
  python build_exe.py --version 1.2.0  # explicit version (CI passes the tag)

Inno Setup (optional, only for the Setup.exe): https://jrsoftware.org/isdl.php
or `winget install JRSoftware.InnoSetup`.
"""

import argparse
import hashlib
import os
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
APP_NAME = "StreetviewTourEditor"
DISPLAY_NAME = "Streetview Tour Editor"
BUNDLE_ID = "io.github.f0up.streetviewtoureditor"
DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build"
ICONS = ROOT / "packaging" / "icons"
HOOKS = ROOT / "packaging" / "pyinstaller-hooks"
ISS = ROOT / "installer" / f"{APP_NAME}.iss"
ENTITLEMENTS = ROOT / "packaging" / "macos" / "entitlements.plist"
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


def pyinstaller(name, icon, extra):
    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--onedir",
        "--name", name,
        "--icon", str(icon),
        "--add-data", f"{ROOT / 'editor'}{os.pathsep}editor",
        # Only the Street View Publish part of the Google API client's data.
        "--additional-hooks-dir", str(HOOKS),
        # Other pywebview backends would pull in Qt / GTK / CEF if installed.
        "--exclude-module", "webview.platforms.qt",
        "--exclude-module", "webview.platforms.gtk",
        "--exclude-module", "webview.platforms.cef",
        "--exclude-module", "webview.platforms.android",
        # Optional imports of the libraries above that the app never uses.
        "--exclude-module", "tkinter",
        "--exclude-module", "numpy",
        "--exclude-module", "gevent",
        "--exclude-module", "geventwebsocket",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        "--noconfirm",
        *extra,
        str(ROOT / "run_editor.py"),
    ], check=True)


# ── Windows ──────────────────────────────────────────
def windows_arch():
    if struct.calcsize("P") == 4:
        return "x86"
    if platform.machine().lower() in ("arm64", "aarch64"):
        sys.exit("pythonnet, which the app window needs on Windows, has no ARM64 build. Build x64 instead:\n"
                 "Windows on ARM runs it through its x64 emulation.")
    return "x64"


def build_windows(version, args):
    arch = windows_arch()
    app_dir = DIST_DIR / APP_NAME
    pyinstaller(APP_NAME, ICONS / "app.ico", [
        "--console" if args.console else "--windowed",
        "--add-data", f"{ICONS / 'app.ico'}{os.pathsep}.",  # tray icon in browser mode
        # pywebview and pystray pick their platform backend at runtime, invisible to PyInstaller.
        "--hidden-import", "webview.platforms.winforms",
        "--hidden-import", "webview.platforms.edgechromium",
        "--hidden-import", "pystray._win32",
        "--exclude-module", "webview.platforms.cocoa",
    ])
    sign(app_dir / f"{APP_NAME}.exe")

    outputs = []
    portable = DIST_DIR / f"{APP_NAME}-{version}-windows-{arch}-portable"
    print(f"Zipping {portable.name}.zip ...")
    outputs.append(Path(shutil.make_archive(str(portable), "zip", DIST_DIR, APP_NAME)))

    iscc = None if args.no_installer else find_iscc()
    if iscc:
        print("Building installer ...")
        setup_name = f"{APP_NAME}-{version}-Setup" + ("" if arch == "x64" else f"-{arch}")
        cmd = [
            iscc,
            f"/DAppVersion={version}",
            f"/DArch={arch}",
            f"/DSourceDir={app_dir}",
            f"/DOutputDir={DIST_DIR}",
            f"/DOutputName={setup_name}",
            f"/DIconFile={ICONS / 'app.ico'}",
        ]
        if os.environ.get("SIGN_CMD"):
            # Inno Setup's sign tool syntax: $f is the (already quoted) file, $q a quote.
            inno_cmd = (os.environ["SIGN_CMD"].replace('"{file}"', "$f").replace("{file}", "$f")
                        .replace('"', "$q"))
            cmd += [f"/Ssigncmd={inno_cmd}", "/DSign"]
        subprocess.run(cmd + [str(ISS)], check=True)
        outputs.append(DIST_DIR / f"{setup_name}.exe")
    elif not args.no_installer:
        print("\nInno Setup not found - skipped the Setup.exe.")
        print("Install it (winget install JRSoftware.InnoSetup) and rerun to build the installer.")
    return app_dir / f"{APP_NAME}.exe", outputs


# ── macOS ────────────────────────────────────────────
def build_macos(version, args):
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    identity = os.environ.get("MACOS_SIGN_IDENTITY")
    pyinstaller(DISPLAY_NAME, ICONS / "app.icns", [
        "--windowed",
        "--osx-bundle-identifier", BUNDLE_ID,
        # A .vrtour double-clicked in Finder arrives as an Apple event, not as an argument.
        "--argv-emulation",
        "--hidden-import", "webview.platforms.cocoa",
        "--exclude-module", "webview.platforms.winforms",
        "--exclude-module", "webview.platforms.edgechromium",
        "--exclude-module", "webview.platforms.mshtml",
        *(["--codesign-identity", identity, "--osx-entitlements-file", str(ENTITLEMENTS)] if identity else []),
    ])
    app = DIST_DIR / f"{DISPLAY_NAME}.app"
    write_info_plist(app, version)
    # Info.plist changed after PyInstaller signed the bundle: seal it again (the
    # binaries inside keep their signatures). Apple Silicon runs no unsigned code.
    if identity:
        subprocess.run(["codesign", "--force", "--timestamp", "--options", "runtime",
                        "--entitlements", str(ENTITLEMENTS), "--sign", identity, str(app)], check=True)
    else:
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", "--verbose=2", str(app)], check=True)

    dmg = DIST_DIR / f"{APP_NAME}-{version}-macos-{arch}.dmg"
    make_dmg(app, dmg)
    if identity:
        subprocess.run(["codesign", "--force", "--timestamp", "--sign", identity, str(dmg)], check=True)
        notarize(dmg)
    return app / "Contents" / "MacOS" / DISPLAY_NAME, [dmg]


def write_info_plist(app, version):
    path = app / "Contents" / "Info.plist"
    info = plistlib.loads(path.read_bytes())
    numeric = re.match(r"\d+(?:\.\d+){0,2}", version)  # macOS wants plain numbers: 2.3.0-rc1 -> 2.3.0
    short = numeric.group(0) if numeric else "0.0.0"
    uti = f"{BUNDLE_ID}.vrtour"
    info.update({
        "CFBundleName": DISPLAY_NAME,
        "CFBundleDisplayName": DISPLAY_NAME,
        "CFBundleShortVersionString": short,
        "CFBundleVersion": short,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.graphics-design",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "MIT License - Streetview Tour Editor contributors",
        # The editor page comes from the app's own server on http://127.0.0.1.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "UTExportedTypeDeclarations": [{
            "UTTypeIdentifier": uti,
            "UTTypeDescription": "Streetview tour project",
            "UTTypeConformsTo": ["public.zip-archive", "public.data"],
            "UTTypeTagSpecification": {"public.filename-extension": ["vrtour"]},
        }],
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Streetview tour project",
            "CFBundleTypeRole": "Editor",
            "LSHandlerRank": "Owner",
            "LSItemContentTypes": [uti],
        }],
    })
    path.write_bytes(plistlib.dumps(info))


def make_dmg(app, dmg):
    """A disk image with the app and a shortcut to /Applications to drag it onto."""
    staging = BUILD_DIR / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    shutil.copytree(app, staging / app.name, symlinks=True)
    (staging / "Applications").symlink_to("/Applications")
    dmg.unlink(missing_ok=True)
    for attempt in range(4):  # hdiutil on CI machines now and then reports "Resource busy"
        result = subprocess.run(["hdiutil", "create", "-volname", DISPLAY_NAME, "-srcfolder", str(staging),
                                 "-ov", "-format", "UDZO", str(dmg)])
        if result.returncode == 0:
            return
        time.sleep(5)
    sys.exit("hdiutil could not create the disk image")


def notarize(dmg):
    creds = [os.environ.get(k) for k in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD")]
    if not all(creds):
        print("No APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD - signed, but not notarized.")
        return
    apple_id, team, password = creds
    print("Notarizing (takes a few minutes) ...")
    subprocess.run(["xcrun", "notarytool", "submit", str(dmg), "--apple-id", apple_id, "--team-id", team,
                    "--password", password, "--wait"], check=True)
    subprocess.run(["xcrun", "stapler", "staple", str(dmg)], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="version string, e.g. 1.2.0 (default: git describe)")
    parser.add_argument("--no-installer", action="store_true", help="Windows: skip the Inno Setup step")
    parser.add_argument("--console", action="store_true", help="Windows: build with a console window (debugging)")
    args = parser.parse_args()
    version = (args.version or detect_version()).lstrip("v")

    if sys.platform not in ("win32", "darwin"):
        sys.exit("On Linux, build the .deb package instead: python packaging/linux/build_deb.py")
    if importlib_missing("PyInstaller"):
        sys.exit("PyInstaller not found. Install the build requirements first:\n  pip install -r requirements-build.txt")

    print(f"Building {APP_NAME} {version} ...")
    for old in DIST_DIR.glob(f"{APP_NAME}-*"):
        old.unlink()
    VERSION_MODULE.write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    try:
        app, outputs = (build_windows if sys.platform == "win32" else build_macos)(version, args)
    finally:
        VERSION_MODULE.unlink(missing_ok=True)

    sums = DIST_DIR / "SHA256SUMS.txt"
    sums.write_text("".join(f"{sha256(p)}  {p.name}\n" for p in outputs), encoding="utf-8")

    print("\nDone:")
    print(f"  App:  {app}")
    for p in outputs + [sums]:
        print(f"  {p.name}")


def importlib_missing(module):
    import importlib.util
    return importlib.util.find_spec(module) is None


if __name__ == "__main__":
    main()
