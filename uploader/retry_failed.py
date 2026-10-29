"""
Re-upload only the files that are missing from upload_result.json (i.e. failed),
merge the results, then re-run the connection step.

Usage:
  python retry_failed.py --folder <dir> --manifest <tour.csv>
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from upload_tour import (  # noqa: E402
    connect, get_credentials, get_exif_gps, load_manifest, manifest_links, save_result, upload_one,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder",   required=True, help="Folder containing the photos")
    ap.add_argument("--manifest", required=True, help="tour.csv path")
    ap.add_argument("--wait",     type=float, default=10, help="Minutes to wait for Google before linking (default 10)")
    args = ap.parse_args()

    folder = Path(args.folder)
    result = json.loads((folder / "upload_result.json").read_text(encoding="utf-8"))
    name_to_id = result.get("name_to_id", {})
    manifest_rows = load_manifest(Path(args.manifest))

    # Identify the failed ones
    failed = [r for r in manifest_rows if r["filename"].strip() not in name_to_id]
    print(f"To re-upload: {len(failed)} / already done: {len(name_to_id)}")

    if not failed:
        print("Nothing failed. Running the connection step only.")
    for i, row in enumerate(failed, 1):
        fname = row["filename"].strip()
        path = folder / fname
        lat = float(row["lat"]) if row.get("lat") else None
        lng = float(row["lng"]) if row.get("lng") else None
        heading = float(row["heading"]) if row.get("heading") else None
        if lat is None:
            lat, lng, _, exif_heading = get_exif_gps(path)
            if heading is None:
                heading = exif_heading
        if lat is None:
            print(f"[{i}/{len(failed)}] {fname}: no position in the CSV or EXIF -> skip")
            continue
        print(f"[{i}/{len(failed)}] {fname}")
        photo_id = upload_one(get_credentials(), {'path': path, 'filename': fname, 'lat': lat, 'lng': lng, 'heading': heading},
                              result.get("place_id"))
        if photo_id:
            name_to_id[fname] = photo_id
            save_result(folder, name_to_id, result.get("place_id"), args.manifest)
            print(f"  OK: {photo_id}")

    print("\nRe-running connections...")
    connect(get_credentials(), name_to_id, manifest_links(manifest_rows), args.wait)
    print("Done")


if __name__ == "__main__":
    main()
