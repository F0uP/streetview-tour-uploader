"""
Build the Linux package: dist/streetview-tour-editor_<version>_all.deb

One package for every architecture (amd64, arm64, ...): it contains only
Python code and runs on the system's python3. What has compiled parts comes
from the distribution instead - GTK and WebKitGTK for the app window
(python3-gi, gir1.2-webkit2-4.1) and python3-cryptography for the Google
sign-in - so it needs Debian 12, Ubuntu 24.04 or newer. The other Python
libraries are bundled (pure-Python wheels only; the build fails if one has
no such wheel).

Installs to:
  /usr/bin/streetview-tour-editor               start script
  /usr/lib/streetview-tour-editor/              the app and its bundled libraries
  /usr/share/applications, mime, icons, metainfo  menu entry, .vrtour file type, icons

Usage (on Linux, with dpkg-deb; the Python running this decides which
backports get bundled, so use the oldest one the package supports, 3.11):
  python3 packaging/linux/build_deb.py --version 1.2.0
  sudo apt install ./dist/streetview-tour-editor_1.2.0_all.deb
"""

import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = ROOT / "dist"
PACKAGE = "streetview-tour-editor"
APP_ID = "io.github.f0up.streetviewtoureditor"
LIB = Path("usr/lib") / PACKAGE
HOMEPAGE = "https://github.com/F0uP/streetview-tour-uploader"
MAINTAINER = "Streetview Tour Editor <F0uP@users.noreply.github.com>"
# Only needed for the tray icon (Windows) and the command-line tools.
NOT_BUNDLED = {"pystray", "pillow", "piexif"}
# Compiled; Depends on the distribution's packages instead.
FROM_SYSTEM = {"cryptography", "cffi", "pycparser"}
DEPENDS = ("python3 (>= 3.11), python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1 | gir1.2-webkit2-4.0, "
           "python3-cryptography (>= 38.0.3), xdg-utils")
APP_FILES = ["run_editor.py", "google_api.py", "places.py", "panoramax.py", "bug_report.py", "app_window.py", "app_update.py", "app_tray.py"]
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256, 512]


def deb_version(version):
    # Debian sorts "~" before everything: 2.3.0~rc1 < 2.3.0. A "-" would mean a Debian revision.
    return version.lstrip("v").replace("-", "~")


def pip(*args, **kwargs):
    return subprocess.run([sys.executable, "-m", "pip", "--disable-pip-version-check", *args], check=True, **kwargs)


def resolve(requirements, workdir):
    """Name and version of every library the app needs, as pip would install them."""
    report = workdir / "report.json"
    pip("install", "--dry-run", "--ignore-installed", "--quiet", "--report", str(report), "-r", str(requirements))
    items = json.loads(report.read_text(encoding="utf-8"))["install"]
    return [(i["metadata"]["name"], i["metadata"]["version"]) for i in items]


def pure_wheel(name, version, wheels):
    """Fetch (or build) a py3-none-any wheel of name==version into wheels/."""
    spec = f"{name}=={version}"
    before = set(wheels.iterdir())
    try:
        pip("download", "--quiet", "--no-deps", "--only-binary=:all:", "--platform", "any",
            "--implementation", "py", "--abi", "none", "--python-version", "3.11", "--dest", str(wheels), spec,
            stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        pip("wheel", "--quiet", "--no-deps", "--no-binary", ":all:", "--wheel-dir", str(wheels), spec)
    new = [p for p in set(wheels.iterdir()) - before if p.suffix == ".whl"]
    if not new or not all(p.name.endswith("-none-any.whl") for p in new):
        sys.exit(f"{spec} has no pure-Python wheel - it can't go into an architecture-independent package.")


def vendor(target, workdir):
    reqs = workdir / "requirements.txt"
    lines = [line for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")
             and re.split(r"[<>=!~;\[ ]", line.strip(), maxsplit=1)[0].lower() not in NOT_BUNDLED]
    reqs.write_text("\n".join(lines) + "\n", encoding="utf-8")
    wheels = workdir / "wheels"
    wheels.mkdir()
    bundled = []
    for name, version in resolve(reqs, workdir):
        if name.lower().replace("_", "-") in FROM_SYSTEM:
            continue
        pure_wheel(name, version, wheels)
        bundled.append(f"{name} {version}")
    pip("install", "--quiet", "--no-deps", "--no-compile", "--target", str(target),
        *[str(w) for w in sorted(wheels.glob("*.whl"))])

    shutil.rmtree(target / "bin", ignore_errors=True)
    # pywebview's Windows and Android binaries.
    shutil.rmtree(target / "webview" / "lib", ignore_errors=True)
    compiled = [p for p in target.rglob("*") if p.suffix in (".so", ".pyd", ".dll", ".dylib", ".exe")]
    if compiled:
        sys.exit("Compiled files ended up in the package:\n  " + "\n  ".join(map(str, compiled)))
    return bundled


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def desktop_files(pkg, version):
    write(pkg / "usr/bin" / PACKAGE, f"""#!/bin/sh
# Streetview Tour Editor: the system's python3, without the user's own site-packages
exec /usr/bin/python3 -s /{LIB}/run_editor.py "$@"
""", 0o755)

    write(pkg / "usr/share/applications" / f"{PACKAGE}.desktop", f"""[Desktop Entry]
Type=Application
Name=Streetview Tour Editor
GenericName=360° Tour Editor
GenericName[de]=360°-Tour-Editor
Comment=Plan 360° walkthrough tours on a map and publish them to Google Street View
Comment[de]=360°-Rundgänge auf der Karte planen und auf Google Street View veröffentlichen
Exec={PACKAGE} %f
Icon={PACKAGE}
Terminal=false
Categories=Graphics;Photography;
Keywords=360;panorama;street view;virtual tour;
MimeType=application/x-streetview-tour;
StartupNotify=true
StartupWMClass={PACKAGE}
""")

    write(pkg / "usr/share/mime/packages" / f"{PACKAGE}.xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-streetview-tour">
    <comment>Streetview tour project</comment>
    <comment xml:lang="de">Streetview-Tour-Projekt</comment>
    <sub-class-of type="application/zip"/>
    <icon name="{PACKAGE}"/>
    <glob pattern="*.vrtour"/>
  </mime-type>
</mime-info>
""")

    today = datetime.date.today().isoformat()
    write(pkg / "usr/share/metainfo" / f"{APP_ID}.metainfo.xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>{APP_ID}</id>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>MIT</project_license>
  <name>Streetview Tour Editor</name>
  <summary>Plan 360° walkthrough tours and publish them to Google Street View</summary>
  <description>
    <p>Drop your 360° photos on a map, link them into a walkable tour, align north, blur faces
    and licence plates, preview the tour and publish it to Google Street View - or export it as
    a website you can host anywhere.</p>
  </description>
  <launchable type="desktop-id">{PACKAGE}.desktop</launchable>
  <url type="homepage">{HOMEPAGE}</url>
  <url type="bugtracker">{HOMEPAGE}/issues</url>
  <provides>
    <binary>{PACKAGE}</binary>
    <mediatype>application/x-streetview-tour</mediatype>
  </provides>
  <releases>
    <release version="{version}" date="{today}"/>
  </releases>
  <content_rating type="oars-1.1"/>
</component>
""")

    icons = ROOT / "packaging" / "icons"
    for size in ICON_SIZES:
        dest = pkg / f"usr/share/icons/hicolor/{size}x{size}/apps/{PACKAGE}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(icons / "png" / f"{size}.png", dest)
    dest = pkg / f"usr/share/icons/hicolor/scalable/apps/{PACKAGE}.svg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(icons / "icon.svg", dest)


def copyright_file(pkg, bundled):
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8").strip()
    body = "\n".join(" " + (line if line.strip() else ".") for line in license_text.splitlines())
    write(pkg / "usr/share/doc" / PACKAGE / "copyright", f"""Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: {PACKAGE}
Source: {HOMEPAGE}

Files: *
Copyright: Streetview Tour Editor contributors
License: MIT
{body}

Files: {LIB}/vendor/*
Comment: Bundled Python libraries, each under its own license (see the
 license files in its .dist-info folder): {", ".join(bundled)}
""")


def maintainer_scripts(debian):
    # Byte-compile for the installed python3 (the program folder is read-only for users).
    write(debian / "postinst", f"""#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    python3 -m compileall -q /{LIB} >/dev/null 2>&1 || true
fi
""", 0o755)
    write(debian / "prerm", f"""#!/bin/sh
set -e
find /{LIB} -type d -name __pycache__ -prune -exec rm -rf {{}} + 2>/dev/null || true
""", 0o755)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="e.g. 1.2.0")
    parser.add_argument("--stage-only", action="store_true", help="prepare the package tree without dpkg-deb")
    args = parser.parse_args()
    version = args.version.lstrip("v")

    work = Path(tempfile.mkdtemp(prefix="svt-deb-"))
    pkg = work / "pkg"
    app = pkg / LIB
    print(f"Building {PACKAGE} {version} in {pkg} ...")

    # The app: same layout as the repository, so run_editor.py finds everything.
    app.mkdir(parents=True)
    for name in APP_FILES:
        shutil.copy(ROOT / name, app / name)
    shutil.copytree(ROOT / "editor", app / "editor")
    (app / "packaging" / "icons" / "png").mkdir(parents=True)
    shutil.copy(ROOT / "packaging" / "icons" / "png" / "256.png", app / "packaging" / "icons" / "png" / "256.png")
    write(app / "_app_version.py", f'VERSION = "{version}"\nPACKAGED = True\n')
    bundled = vendor(app / "vendor", work)
    print("Bundled:", ", ".join(bundled))

    desktop_files(pkg, version)
    copyright_file(pkg, bundled)
    debian = pkg / "DEBIAN"
    maintainer_scripts(debian)

    executables = {pkg / "usr/bin" / PACKAGE, debian / "postinst", debian / "prerm"}
    for path in pkg.rglob("*"):  # predictable permissions, whatever the checkout's umask
        path.chmod(0o755 if path.is_dir() or path in executables else 0o644)
    size_kb = sum(p.stat().st_size for p in pkg.rglob("*") if p.is_file() and debian not in p.parents) // 1024 + 1
    write(debian / "control", f"""Package: {PACKAGE}
Version: {deb_version(version)}
Architecture: all
Maintainer: {MAINTAINER}
Installed-Size: {size_kb}
Depends: {DEPENDS}
Section: graphics
Priority: optional
Homepage: {HOMEPAGE}
Description: Plan 360° walkthrough tours and publish them to Google Street View
 Drop your 360° photos on a map, link them into a walkable tour, align north,
 blur faces and licence plates, preview the tour and publish it to Google
 Street View - or export it as a website. Runs in its own window.
""")
    md5 = []
    for path in sorted(p for p in pkg.rglob("*") if p.is_file() and debian not in p.parents):
        md5.append(f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(pkg).as_posix()}")
    write(debian / "md5sums", "\n".join(md5) + "\n")

    if args.stage_only:
        print(f"Staged in {pkg}")
        return
    DIST_DIR.mkdir(exist_ok=True)
    deb = DIST_DIR / f"{PACKAGE}_{deb_version(version)}_all.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(pkg), str(deb)], check=True)
    shutil.rmtree(work, ignore_errors=True)
    print(f"\nDone: {deb}")


if __name__ == "__main__":
    main()
