#!/usr/bin/env python3
"""
Street View 360 Photo Tour Uploader

Uploads equirectangular 360 JPEGs (RICOH THETA, Insta360, GoPro MAX, etc.) to the
Street View Publish API, links them to a place, and auto-builds navigation
connections between photos. It shares the sign-in and the API code with the
editor (google_api.py).

Usage:
  # EXIF GPS + auto-connect in file order (simple linear route)
  python upload_tour.py --folder ./photos

  # With a CSV manifest (gardens / branching routes -- recommended)
  python upload_tour.py --folder ./photos --manifest ./photos/tour.csv

  # Link to a Google place
  python upload_tour.py --folder ./photos --manifest ./photos/tour.csv --place-id ChIJxxxxxxxx

  # Dry run (no upload)
  python upload_tour.py --folder ./photos --manifest ./photos/tour.csv --dry-run

  # Links only (Google was still processing the photos last time)
  python upload_tour.py --folder ./photos --manifest ./photos/tour.csv --connect-only
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # google_api.py

try:
    import piexif
    from google_api import GoogleError, SignIn, _photo_status, _update_photos, _upload_photo
except ImportError:
    print("Missing dependencies. Install them first:")
    print("   pip install -r requirements.txt")
    sys.exit(1)

HERE = Path(__file__).resolve().parent
SIGN_IN = SignIn(HERE / 'token.json', HERE / 'client_secrets.json')  # the same files as the editor from source
PHOTO_SUFFIXES = {'.jpg', '.jpeg'}


# ──────────────────────────────────────────────
# OAuth 2.0
# ──────────────────────────────────────────────
def get_credentials():
    """The saved sign-in; the first time (or once it expired) the browser asks for it."""
    try:
        return SIGN_IN.credentials()
    except GoogleError as e:
        print(f"Google sign-in: {e.message}")
        sys.exit(1)
    except RuntimeError:
        if not SIGN_IN.credentials_file.exists():
            print("client_secrets.json not found")
            print(f"   Save it as {SIGN_IN.credentials_file} (or set it up once in the editor)")
            sys.exit(1)
        print('\nOpening your browser - sign in with your Google account (within 3 minutes)...')
        try:
            return SIGN_IN.sign_in()
        except RuntimeError as e:
            print(f'\n{e}')
            sys.exit(1)


# ──────────────────────────────────────────────
# EXIF GPS
# ──────────────────────────────────────────────
def get_exif_gps(filepath):
    """Return GPS lat/lng/altitude/heading from a JPEG's EXIF."""
    try:
        exif = piexif.load(str(filepath))
        gps = exif.get('GPS', {})
        if not gps:
            return None, None, None, None

        def dms_to_decimal(dms, ref):
            d = dms[0][0] / dms[0][1]
            m = dms[1][0] / dms[1][1]
            s = dms[2][0] / dms[2][1]
            val = d + m / 60 + s / 3600
            if ref in [b'S', b'W', 'S', 'W']:
                val = -val
            return val

        if piexif.GPSIFD.GPSLatitude not in gps or piexif.GPSIFD.GPSLongitude not in gps:
            return None, None, None, None
        lat = dms_to_decimal(gps[piexif.GPSIFD.GPSLatitude], gps.get(piexif.GPSIFD.GPSLatitudeRef, b'N'))
        lng = dms_to_decimal(gps[piexif.GPSIFD.GPSLongitude], gps.get(piexif.GPSIFD.GPSLongitudeRef, b'E'))

        alt = None
        if piexif.GPSIFD.GPSAltitude in gps:
            a = gps[piexif.GPSIFD.GPSAltitude]
            alt = a[0] / a[1] if a[1] != 0 else 0.0

        heading = None
        if piexif.GPSIFD.GPSImgDirection in gps:
            h = gps[piexif.GPSIFD.GPSImgDirection]
            heading = h[0] / h[1] if h[1] != 0 else None

        return lat, lng, alt, heading
    except Exception:
        return None, None, None, None


# ──────────────────────────────────────────────
# Upload and links
# ──────────────────────────────────────────────
def upload_one(creds, pd, place_id):
    """Upload one photo; its ID, or None (the reason is printed)."""
    try:
        return _upload_photo(creds, pd['path'].read_bytes(), pd['filename'], pd['lat'], pd['lng'],
                             heading=pd['heading'], place_id=place_id, altitude=pd.get('alt'))
    except GoogleError as e:
        print(f"  error ({e.step}, {e.status}): {e.message}")
    except OSError as e:
        print(f"  error: {e}")
    return None


def wait_until_known(creds, ids, minutes):
    """Wait (up to `minutes`) until Google knows the photos - it accepts links only then.
    Returns {id: what Google knows of it} for the photos it knows."""
    deadline = time.time() + minutes * 60
    while True:
        try:
            status = _photo_status(creds, ids)
        except GoogleError as e:
            print(f"  could not ask Google: {e.message}")
            status = {}
        known = {pid: s for pid, s in status.items() if s.get('status') != 'notfound'}
        print(f"  Google knows {len(known)} of {len(ids)} photos", end='\r')
        if len(known) == len(ids) or time.time() >= deadline:
            print()
            return known
        time.sleep(15)


def connect(creds, name_to_id, links, wait_minutes):
    """Set the links: links = {filename: [linked filename, ...]}. Photos Google doesn't know
    yet are left out; run again with --connect-only later for them."""
    if not links:
        return
    print(f"\nWaiting for Google to take the photos in (up to {wait_minutes} min)...")
    known = wait_until_known(creds, list(name_to_id.values()), wait_minutes)
    # Google answers to the real form of each ID: use it for the links too.
    real = {name: known[pid].get('resolvedId') or pid for name, pid in name_to_id.items() if pid in known}
    items = []
    for name, targets in links.items():
        if name not in real:
            print(f"  {name}: not processed by Google yet - skip")
            continue
        facts = known[name_to_id[name]]
        ids = [real[t] for t in targets if t in real]
        missing = [t for t in targets if t not in real]
        if missing:
            print(f"  {name}: {', '.join(missing)} not on Google yet - left out")
        # Position and heading stay as Google has them; only the links change.
        items.append({'id': real[name], 'lat': facts['lat'], 'lng': facts['lng'], 'heading': facts.get('heading'), 'connections': ids})
    if not items:
        print("Nothing to link yet. Try again later with --connect-only.")
        return
    print(f"\nBuilding connections... ({len(items)} photos)")
    for res in _update_photos(creds, items):
        name = next(n for n, i in real.items() if i == res['id'])
        print(f"  [{'OK' if res['ok'] else 'NG'}] {name}" + ('' if res['ok'] else f"  {res.get('error')}"))


def linear_links(names):
    return {n: [x for x in (names[i - 1] if i else None, names[i + 1] if i + 1 < len(names) else None) if x]
            for i, n in enumerate(names)}


def manifest_links(rows):
    return {row['filename'].strip(): [t.strip() for t in (row.get('connects_to') or '').split(';') if t.strip()]
            for row in rows}


def load_manifest(manifest_path):
    """Load tour.csv."""
    with open(manifest_path, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def save_result(folder, name_to_id, place_id, manifest):
    result_file = folder / 'upload_result.json'
    result_file.write_text(json.dumps({
        'uploaded': len(name_to_id),
        'place_id': place_id,
        'manifest': str(manifest) if manifest else None,
        'name_to_id': name_to_id,
        'photo_ids': list(name_to_id.values()),
    }, indent=2, ensure_ascii=False), encoding='utf-8')
    return result_file


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Street View 360 photo tour uploader')
    parser.add_argument('--folder',       required=True, help='Folder containing the JPEGs')
    parser.add_argument('--manifest',     default=None,  help='tour.csv path (manual lat/lng/heading/connections)')
    parser.add_argument('--place-id',     default=None,  help='Google Place ID to link the tour to')
    parser.add_argument('--dry-run',      action='store_true', help='Validate only, do not upload')
    parser.add_argument('--connect-only', action='store_true', help='Re-run only the connection step from upload_result.json')
    parser.add_argument('--wait',         type=float, default=10, help='Minutes to wait for Google before linking (default 10)')
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Folder not found: {folder}")
        sys.exit(1)

    # Auto-read place_id from tour-meta.json (--place-id takes precedence)
    if not args.place_id:
        meta_path = folder / 'tour-meta.json'
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            if meta.get('place_id'):
                args.place_id = meta['place_id']
                print(f"place_id: {args.place_id}  ({meta.get('note', '')})")

    manifest_rows = None
    if args.manifest:
        if not Path(args.manifest).exists():
            print(f"Manifest not found: {args.manifest}")
            sys.exit(1)
        manifest_rows = load_manifest(args.manifest)

    # ── Re-run connections only ───────────────────
    if args.connect_only:
        result_file = folder / 'upload_result.json'
        if not result_file.exists():
            print(f"{result_file} not found. Run the upload first.")
            sys.exit(1)
        name_to_id = json.loads(result_file.read_text(encoding='utf-8')).get('name_to_id', {})
        if not name_to_id:
            print("No uploaded photoIds found.")
            sys.exit(1)
        creds = get_credentials()
        links = manifest_links(manifest_rows) if manifest_rows else linear_links(list(name_to_id))
        connect(creds, name_to_id, links, args.wait)
        print("Connections done")
        return

    # ──────────────────────────────────────────
    # Manifest or auto-scan
    # ──────────────────────────────────────────
    valid = []
    if manifest_rows:
        print(f"Manifest: {args.manifest}  ({len(manifest_rows)} rows)")
        for row in manifest_rows:
            filename = row['filename'].strip()
            path = folder / filename
            if not path.exists():
                print(f"  {filename}: file not found -> skip")
                continue
            # lat/lng: CSV first, then EXIF
            lat_raw, lng_raw = (row.get('lat') or '').strip(), (row.get('lng') or '').strip()
            heading_raw = (row.get('heading') or '').strip()
            if lat_raw and lng_raw:
                lat, lng, alt = float(lat_raw), float(lng_raw), None
            else:
                lat, lng, alt, _ = get_exif_gps(path)
                if lat is None:
                    print(f"  {filename}: no lat/lng in CSV or EXIF -> skip")
                    continue
            heading = float(heading_raw) if heading_raw else None
            valid.append({'path': path, 'filename': filename, 'lat': lat, 'lng': lng, 'alt': alt, 'heading': heading})
            print(f"  {filename:40s} ({lat:.6f}, {lng:.6f})" + (f"  heading={heading:.0f}" if heading is not None else ""))
    else:
        # Auto-scan (EXIF GPS + file order)
        photos = sorted(p for p in folder.iterdir() if p.suffix.lower() in PHOTO_SUFFIXES)
        if not photos:
            print(f"No JPG files found: {folder}")
            sys.exit(1)
        print(f"Folder: {folder}\nFound {len(photos)} photos\n")
        for p in photos:
            lat, lng, alt, heading = get_exif_gps(p)
            if lat is not None:
                valid.append({'path': p, 'filename': p.name, 'lat': lat, 'lng': lng, 'alt': alt, 'heading': heading})
                print(f"  {p.name:40s} ({lat:.6f}, {lng:.6f})" + (f"  heading={heading:.1f}" if heading is not None else ""))
            else:
                print(f"  {p.name:40s} no GPS -> skip")

    print(f"\nTo upload: {len(valid)} photos")
    if not valid:
        print("No valid photos.")
        sys.exit(1)
    if args.dry_run:
        print("\n[dry-run] Stopping here. Remove --dry-run to actually upload.")
        return

    print("\nGoogle auth...")
    creds = get_credentials()

    print("\nUploading...")
    name_to_id = {}   # filename -> photoId (used for connections)
    for i, pd in enumerate(valid, 1):
        print(f"\n[{i}/{len(valid)}] {pd['filename']}")
        photo_id = upload_one(get_credentials(), pd, args.place_id)  # a long upload can outlast the sign-in's hour
        if photo_id:
            name_to_id[pd['filename']] = photo_id
            print(f"  photoId: {photo_id}")
        save_result(folder, name_to_id, args.place_id, args.manifest)

    if len(name_to_id) > 1:
        links = manifest_links(manifest_rows) if manifest_rows else linear_links([pd['filename'] for pd in valid if pd['filename'] in name_to_id])
        connect(get_credentials(), name_to_id, links, args.wait)

    result_file = save_result(folder, name_to_id, args.place_id, args.manifest)
    print(f"\nDone! Uploaded {len(name_to_id)} photos")
    if len(name_to_id) > 1:
        print("If some photos were not linked yet, run later:")
        print(f"   python upload_tour.py --folder {args.folder}" + (f" --manifest {args.manifest}" if args.manifest else "") + " --connect-only")
    print(f"Result: {result_file}")
    print("Check: https://www.google.com/maps/contrib/")
    if args.place_id:
        print(f"Linked to Place ID: {args.place_id}")


if __name__ == '__main__':
    main()
