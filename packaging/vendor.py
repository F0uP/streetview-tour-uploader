"""
The third-party libraries of the editor, shipped with the app in editor/vendor/.

The editor used to load them from a CDN at runtime: without internet it didn't
start, and it trusted whatever that server delivered. Now only map tiles and
Google come from the internet. This script downloads the pinned versions below
and checks every file against packaging/vendor.lock.json (SHA-256).

  python packaging/vendor.py            # fetch what's missing or changed, verify everything
  python packaging/vendor.py --check    # only verify (CI)
  python packaging/vendor.py --update   # after changing a version: download again, rewrite the lock

editor/vendor/ is committed, so builds need no network either.
"""

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "editor" / "vendor"
LOCK = Path(__file__).with_name("vendor.lock.json")
NPM = "https://cdn.jsdelivr.net/npm"
LEAFLET = f"{NPM}/leaflet@1.9.4"
THREE = f"{NPM}/three@0.169.0"
PSV = f"{NPM}/@photo-sphere-viewer"
PSV_VERSION = "5.11.5"
MEDIAPIPE = f"{NPM}/@mediapipe/tasks-vision@1.0.1"
MODELS = "https://storage.googleapis.com/mediapipe-models/face_detector"
MANROPE = f"{NPM}/@fontsource-variable/manrope@5.0.19"

# path in editor/vendor -> source URL
FILES = {
    "leaflet/leaflet.js": f"{LEAFLET}/dist/leaflet.js",
    "leaflet/leaflet.css": f"{LEAFLET}/dist/leaflet.css",
    "leaflet/images/layers.png": f"{LEAFLET}/dist/images/layers.png",
    "leaflet/images/layers-2x.png": f"{LEAFLET}/dist/images/layers-2x.png",
    "leaflet/images/marker-icon.png": f"{LEAFLET}/dist/images/marker-icon.png",
    "leaflet/images/marker-icon-2x.png": f"{LEAFLET}/dist/images/marker-icon-2x.png",
    "leaflet/images/marker-shadow.png": f"{LEAFLET}/dist/images/marker-shadow.png",
    "leaflet/LICENSE": f"{LEAFLET}/LICENSE",
    "three/three.module.min.js": f"{THREE}/build/three.module.min.js",
    "three/LICENSE": f"{THREE}/LICENSE",
    **{f"psv/{part}/{name}": f"{PSV}/{part}@{PSV_VERSION}/{name}"
       for part in ("core", "markers-plugin", "virtual-tour-plugin")
       for name in ("index.module.js", "index.css", "LICENSE")},
    "exifr/full.esm.js": f"{NPM}/exifr@7.1.3/dist/full.esm.js",
    "exifr/LICENSE": f"{NPM}/exifr@7.1.3/LICENSE",
    # Face detection for the blur tool. Only the SIMD build of the WebAssembly:
    # every web engine the app runs in supports it (without, detection is just off).
    "mediapipe/vision_bundle.mjs": f"{MEDIAPIPE}/vision_bundle.mjs",
    "mediapipe/wasm/vision_wasm_internal.js": f"{MEDIAPIPE}/wasm/vision_wasm_internal.js",
    "mediapipe/wasm/vision_wasm_internal.wasm": f"{MEDIAPIPE}/wasm/vision_wasm_internal.wasm",
    "mediapipe/blaze_face_full_range.tflite": f"{MODELS}/blaze_face_full_range/float16/1/blaze_face_full_range.tflite",
    "mediapipe/blaze_face_short_range.tflite": f"{MODELS}/blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
    # Vehicles, for suggesting where licence plates are (COCO object detector).
    "mediapipe/efficientdet_lite0.tflite": "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float16/1/efficientdet_lite0.tflite",
    # QR codes for sharing a tour (MIT, the licence is in the file's header).
    "qrcode/qrcode.mjs": f"{NPM}/qrcode-generator@2.0.4/dist/qrcode.mjs",
    # The UI font; Latin only (German and English).
    "manrope/manrope-latin-wght-normal.woff2": f"{MANROPE}/files/manrope-latin-wght-normal.woff2",
    "manrope/manrope-latin-ext-wght-normal.woff2": f"{MANROPE}/files/manrope-latin-ext-wght-normal.woff2",
    "manrope/LICENSE": f"{MANROPE}/LICENSE",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "StreetviewTourEditor-vendor"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only verify the files against the lock")
    parser.add_argument("--update", action="store_true", help="download everything again and rewrite the lock")
    args = parser.parse_args()
    lock = {} if args.update or not LOCK.exists() else json.loads(LOCK.read_text(encoding="utf-8"))

    problems = []
    for path, url in FILES.items():
        target = VENDOR / path
        entry = lock.get(path)
        if entry and entry["url"] == url and target.exists() and sha256(target.read_bytes()) == entry["sha256"]:
            continue
        if args.check:
            problems.append(f"{path}: {'missing' if not target.exists() else 'differs from the lock'}")
            continue
        print(f"Downloading {path} ...")
        data = download(url)
        if entry and entry["url"] == url and sha256(data) != entry["sha256"]:
            sys.exit(f"{path}: the download does not match the lock - refusing it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        lock[path] = {"url": url, "sha256": sha256(data), "size": len(data)}

    stale = set(lock) - set(FILES)
    for path in stale:
        lock.pop(path)
        (VENDOR / path).unlink(missing_ok=True)
    if problems:
        sys.exit("editor/vendor is not what packaging/vendor.lock.json says:\n  " + "\n  ".join(problems))
    if not args.check:
        LOCK.write_text(json.dumps(dict(sorted(lock.items())), indent=1) + "\n", encoding="utf-8")
    total = sum(e["size"] for e in lock.values())
    print(f"{len(lock)} files, {total / 1024 / 1024:.1f} MB, all match the lock.")


if __name__ == "__main__":
    main()
