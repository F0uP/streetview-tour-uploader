"""
Write winget manifests for a published release, for the first submission to
microsoft/winget-pkgs (later versions are submitted by the release workflow).

Usage (after the GitHub Release for the version exists):
  python packaging/winget/make_manifests.py 1.2.0
  winget validate packaging/winget/manifests/f/F0uP/StreetviewTourEditor/1.2.0
  winget install --manifest packaging/winget/manifests/f/F0uP/StreetviewTourEditor/1.2.0

Then open a pull request to https://github.com/microsoft/winget-pkgs adding
that folder under manifests/ (or run `wingetcreate submit <folder>`).
"""

import sys
from pathlib import Path
from urllib.request import urlopen

IDENTIFIER = "F0uP.StreetviewTourEditor"
REPO = "https://github.com/F0uP/streetview-tour-uploader"
MANIFEST_VERSION = "1.10.0"
SCHEMA = "https://aka.ms/winget-manifest.{kind}.%s.schema.json" % MANIFEST_VERSION


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    version = sys.argv[1].lstrip("v")
    setup = f"StreetviewTourEditor-{version}-Setup.exe"
    base = f"{REPO}/releases/download/v{version}"
    with urlopen(f"{base}/SHA256SUMS.txt", timeout=30) as r:
        sums = dict(reversed(line.split()) for line in r.read().decode().splitlines() if line.strip())
    sha = sums[setup].upper()

    out = Path(__file__).parent / "manifests" / "f" / "F0uP" / "StreetviewTourEditor" / version
    out.mkdir(parents=True, exist_ok=True)
    head = f"PackageIdentifier: {IDENTIFIER}\nPackageVersion: {version}\n"

    (out / f"{IDENTIFIER}.yaml").write_text(
        f"# yaml-language-server: $schema={SCHEMA.format(kind='version')}\n{head}"
        f"DefaultLocale: en-US\nManifestType: version\nManifestVersion: {MANIFEST_VERSION}\n", encoding="utf-8")

    (out / f"{IDENTIFIER}.installer.yaml").write_text(
        f"# yaml-language-server: $schema={SCHEMA.format(kind='installer')}\n{head}"
        "InstallerType: inno\n"
        "Scope: user\n"
        "UpgradeBehavior: install\n"
        "FileExtensions:\n- vrtour\n"
        "Installers:\n"
        "- Architecture: x64\n"
        f"  InstallerUrl: {base}/{setup}\n"
        f"  InstallerSha256: {sha}\n"
        f"ManifestType: installer\nManifestVersion: {MANIFEST_VERSION}\n", encoding="utf-8")

    (out / f"{IDENTIFIER}.locale.en-US.yaml").write_text(
        f"# yaml-language-server: $schema={SCHEMA.format(kind='defaultLocale')}\n{head}"
        "PackageLocale: en-US\n"
        "Publisher: F0uP\n"
        f"PublisherUrl: https://github.com/F0uP\n"
        f"PublisherSupportUrl: {REPO}/issues\n"
        "PackageName: Streetview Tour Editor\n"
        f"PackageUrl: {REPO}\n"
        "License: MIT\n"
        f"LicenseUrl: {REPO}/blob/master/LICENSE\n"
        "ShortDescription: Plan 360° walkthrough tours on a map and publish them to Google Street View.\n"
        "Tags:\n- 360\n- panorama\n- street-view\n- virtual-tour\n"
        f"ReleaseNotesUrl: {REPO}/releases/tag/v{version}\n"
        f"ManifestType: defaultLocale\nManifestVersion: {MANIFEST_VERSION}\n", encoding="utf-8")

    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
