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

  # A project saved by the editor: its photos not on Google yet, with position, north,
  # place, floor and horizon as set there; then the links. The new photo IDs are written
  # back into the project file, so the editor knows them when it opens it.
  python upload_tour.py --project ./tour.vrtour
"""

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # google_api.py

try:
    import piexif
    from google_api import GoogleError, SignIn, _delete_photos, _photo_status, _update_photos, _upload_photo
except ImportError:
    print("Missing dependencies. Install them first:")
    print("   pip install -r requirements.txt")
    sys.exit(1)

HERE = Path(__file__).resolve().parent
SIGN_IN = SignIn(HERE / 'token.json', HERE / 'client_secrets.json')  # the same files as the editor from source
PHOTO_SUFFIXES = {'.jpg', '.jpeg'}
GOOGLE_MAX = 75 * 1024 * 1024 - 256 * 1024  # larger photos: made smaller by the editor's upload
SAMPLE_PHOTO = re.compile(r'svt-sample-\d+\.jpg')  # the sample tour's photos are drawn: never published


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


def connect(creds, name_to_id, links, wait_minutes, label=str, own_ids=None):
    """Set the links: links = {filename: [linked filename, ...]}. Photos Google doesn't know
    yet are left out; run again with --connect-only later for them. own_ids: the photos of a
    project; links Google has to any other photo stay, as they do in the editor.
    Returns (what Google knows of each name's photo, the names whose links were set)."""
    if not links:
        return {}, set()
    print(f"\nWaiting for Google to take the photos in (up to {wait_minutes} min)...")
    known = wait_until_known(creds, list(name_to_id.values()), wait_minutes)
    # Google answers to the real form of each ID: use it for the links too.
    real = {name: known[pid].get('resolvedId') or pid for name, pid in name_to_id.items() if pid in known}
    items = []
    for name, targets in links.items():
        if name not in real:
            print(f"  {label(name)}: not processed by Google yet - skip")
            continue
        facts = known[name_to_id[name]]
        ids = [real[t] for t in targets if t in real]
        if own_ids is not None:
            ours = own_ids | set(name_to_id.values()) | set(real.values())
            ids += [c for c in facts.get('connections') or [] if c not in ours]
        missing = [t for t in targets if t not in real]
        if missing:
            print(f"  {label(name)}: {', '.join(map(label, missing))} not on Google yet - left out")
        # Position and heading stay as Google has them; only the links change.
        items.append({'id': real[name], 'lat': facts['lat'], 'lng': facts['lng'], 'heading': facts.get('heading'), 'connections': ids})
    facts = {name: {**known[pid], 'resolvedId': real[name]} for name, pid in name_to_id.items() if name in real}
    if not items:
        print("Nothing to link yet. Try again later with --connect-only.")
        return facts, set()
    print(f"\nBuilding connections... ({len(items)} photos)")
    linked = set()
    for res in _update_photos(creds, items):
        name = next(n for n, i in real.items() if i == res['id'])
        print(f"  [{'OK' if res['ok'] else 'NG'}] {label(name)}" + ('' if res['ok'] else f"  {res.get('error')}"))
        if res['ok']:
            linked.add(name)
    return facts, linked


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
# A project file of the editor (.vrtour)
# ──────────────────────────────────────────────
def read_project(path):
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read('project.json'))


def write_project(path, project):
    """project.json back into the .vrtour (by way of a new file); the photos are copied as they are.
    It is a new version of the project, as the editor counts them: an editor that has the
    project open merges it in from the one before."""
    if project.get('rev'):
        project['revs'] = ((project.get('revs') or []) + [project['rev']])[-50:]
    project['rev'] = uuid.uuid4().hex[:14]
    project['savedBy'] = {'name': 'upload_tour.py', 'at': int(time.time() * 1000)}
    tmp = path.with_name(path.name + '.tmp')
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, 'w', allowZip64=True) as dst:
        for info in src.infolist():
            out = zipfile.ZipInfo(info.filename, info.date_time)
            out.compress_type, out.external_attr = info.compress_type, info.external_attr
            if info.filename == 'project.json':
                dst.writestr(out, json.dumps(project, indent=2, ensure_ascii=False).encode('utf-8'))
                continue
            out.file_size = info.file_size
            with src.open(info) as f_in, dst.open(out, 'w', force_zip64=info.file_size > 0x7FFFFFFF) as f_out:
                shutil.copyfileobj(f_in, f_out, 1 << 20)
    os.replace(tmp, path)


def journal_of(path):
    """Photo IDs as they come, next to the project: an interrupted run loses none of them
    (and the next run does not upload them twice). Gone once they are in the project."""
    return path.with_name(path.name + '.upload.json')


def project_links(project):
    """{node id: {linked node ids}} from the project's links."""
    ids = {n['id'] for n in project.get('nodes', [])}
    links = {}
    for c in project.get('conns', []):
        if c.get('from') in ids and c.get('to') in ids and c['from'] != c['to']:
            links.setdefault(c['from'], set()).add(c['to'])
            links.setdefault(c['to'], set()).add(c['from'])
    return links


def own_pose(n):
    return n.get('pitch') is not None or n.get('roll') is not None


def needs_upload(n):
    """As in the editor: a photo not on Google, or gone from it - but never one of the sample tour."""
    return (bool(n.get('photoPath')) and (not n.get('gsvPhotoId') or n.get('gsvStatus') == 'missing')
            and not SAMPLE_PHOTO.fullmatch(n.get('photoName') or ''))


def js_fixed(x, digits):
    """JavaScript's x.toFixed(digits): rounded on the exact value of the float, halves away
    from zero (Python's own formatting rounds halves to even: -1.25 gives -1.2, not -1.3)."""
    if x == 0:
        x = 0.0  # -0 too: "0.00"
    return format(Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), 'f')


def signature(n, linked_ids):
    """What Google was last told about a point, as the editor counts it (gsvSignature in
    editor.js): when it no longer matches the point, the editor sends Google its facts again."""
    sig = [js_fixed(n['lat'], 7), js_fixed(n['lng'], 7), None if n.get('heading') is None else js_fixed(n['heading'], 2), sorted(linked_ids)]
    if n.get('placeId'):
        sig.append(n['placeId'])
    if n.get('level'):
        sig.append(f"level {n['level']['number']} {n['level'].get('name') or ''}")
    if own_pose(n):
        sig.append(f"pose {js_fixed(n.get('pitch') or 0, 1)} {js_fixed(n.get('roll') or 0, 1)}")
    return json.dumps(sig, ensure_ascii=False, separators=(',', ':'))


def same_but_links(old, new):
    """Do two signatures say the same, apart from the links (their fourth part)?"""
    try:
        a, b = json.loads(old), json.loads(new)
    except (TypeError, ValueError):
        return False
    return a[:3] + a[4:] == b[:3] + b[4:]


def upload_project_photo(creds, n, data, place_id):
    """One point of the project to Google, as the editor uploads it. A place Google refuses:
    the photo goes up without it (and the point has no place any more). Its ID, or None."""
    level = {'number': int(n['level']['number']), 'name': str(n['level'].get('name') or '')[:3]} if n.get('level') else None
    pose = {'pitch': n.get('pitch') or 0, 'roll': n.get('roll') or 0} if own_pose(n) else {}
    filename = n.get('photoName') or 'photo.jpg'
    try:
        try:
            return _upload_photo(creds, data, filename, n['lat'], n['lng'], heading=n.get('heading'),
                                 place_id=place_id, level=level, **pose)
        except GoogleError as e:
            if not place_id or e.kind != 'place':
                raise
            print(f"  Google did not take the place {place_id} ({e.message}) - uploading with the position only")
            photo_id = _upload_photo(creds, data, filename, n['lat'], n['lng'], heading=n.get('heading'), level=level, **pose)
            n.update(placeId=None, placeName=None, placeKind=None, placeDist=None, placeAuto=False)
            return photo_id
    except GoogleError as e:
        print(f"  error ({e.step}, {e.status}): {e.message}")
    return None


def run_project(args):
    path = Path(args.project)
    if not path.is_file():
        print(f"Project not found: {path}")
        sys.exit(1)
    try:
        project = read_project(path)
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        print(f"Not a project file of the editor ({e}): {path}")
        sys.exit(1)
    nodes = project.get('nodes', [])
    by_id = {n['id']: n for n in nodes}

    def name(nid):
        return by_id[nid].get('name') or nid

    journal = journal_of(path)
    uploaded = {}  # node id -> what its upload set, also in the journal
    if journal.exists():  # the last run was interrupted: what it uploaded counts
        uploaded = json.loads(journal.read_text(encoding='utf-8'))
        for nid, entry in uploaded.items():
            if nid in by_id and needs_upload(by_id[nid]):
                by_id[nid].update(entry)
        print(f"{len(uploaded)} photo(s) uploaded by the last, interrupted run")

    todo = [n for n in nodes if needs_upload(n)]
    print(f"Project: {path}  ({len(nodes)} points, {len(nodes) - len(todo)} of them on Google or without a photo)")
    with zipfile.ZipFile(path) as z:
        sizes = {i.filename: i.file_size for i in z.infolist()}
    ready = []
    for n in todo:
        size = sizes.get(n['photoPath'])
        if size is None:
            print(f"  {name(n['id'])}: its photo is not in the project -> skip")
        elif size > GOOGLE_MAX:
            print(f"  {name(n['id'])}: {size / 1e6:.0f} MB, too large for Google -> upload it from the editor (it makes it smaller)")
        else:
            ready.append(n)
            extras = [f"heading={n['heading']:.0f}" if n.get('heading') is not None else '',
                      f"floor {n['level']['number']}" if n.get('level') else '',
                      'levelled' if own_pose(n) else '', 'place' if n.get('placeId') else '']
            print(f"  {name(n['id']):40s} ({n['lat']:.6f}, {n['lng']:.6f})  {' '.join(x for x in extras if x)}")
    print(f"\nTo upload: {len(ready)} photos")
    if args.dry_run:
        print("\n[dry-run] Stopping here. Remove --dry-run to actually upload.")
        return

    print("\nClose the project in the editor first: the new photo IDs are written into it at the end.")
    print("\nGoogle auth...")
    get_credentials()
    try:
        if ready and not args.connect_only:
            print("\nUploading...")
            with zipfile.ZipFile(path) as z:
                for i, n in enumerate(ready, 1):
                    print(f"\n[{i}/{len(ready)}] {name(n['id'])}")
                    place_id = n['placeId'] if 'placeId' in n else args.place_id  # null in the project: no place, on purpose
                    photo_id = upload_project_photo(get_credentials(), n, z.read(n['photoPath']), place_id)
                    if not photo_id:
                        continue
                    print(f"  photoId: {photo_id}")
                    n.update(gsvPhotoId=photo_id, gsvUploadedAt=int(time.time() * 1000), gsvConfirmed=False, gsvStatus='pending',
                             gsvSig=None, gsvShareLink=None)
                    uploaded[n['id']] = {k: n[k] for k in ('gsvPhotoId', 'gsvUploadedAt', 'gsvConfirmed', 'gsvStatus', 'gsvSig', 'gsvShareLink')}
                    journal.write_text(json.dumps(uploaded), encoding='utf-8')
            old = [pid for n in ready if n.get('gsvPhotoId') for pid in n.get('gsvObsolete') or []]
            if old and args.delete_old:
                print(f"\nDeleting {len(old)} old version(s) on Google...")
                gone = {r['id'] for r in _delete_photos(get_credentials(), old) if r['ok']}
                for n in ready:
                    n['gsvObsolete'] = [pid for pid in n.get('gsvObsolete') or [] if pid not in gone]
            elif old:
                print(f"\n{len(old)} old version(s) of these photos are still on Google: --delete-old deletes them"
                      " (or the editor, with its next upload)")

        # The links: every point on Google that is linked to another one on Google.
        graph = project_links(project)
        on_google = {n['id']: n['gsvPhotoId'] for n in nodes if n.get('gsvPhotoId') and n.get('gsvStatus') != 'missing'}
        links = {nid: sorted(t for t in graph.get(nid, ()) if t in on_google) for nid in on_google}
        own_ids = {*on_google.values(), *project.get('gsvRemoved', []), *(pid for n in nodes for pid in n.get('gsvObsolete') or [])}
        facts, linked = connect(get_credentials(), on_google, {k: v for k, v in links.items() if v}, args.wait, label=name, own_ids=own_ids)
        for nid, f in facts.items():
            by_id[nid].update(gsvPhotoId=f['resolvedId'], gsvConfirmed=True, gsvStatus=f['status'],
                              gsvShareLink=f.get('shareLink') or by_id[nid].get('gsvShareLink'))
        # What Google now knows of a point (linked now, or just uploaded without links): the
        # editor sends it again only once it changes. Only links were sent here, so a point
        # changed since it last told Google keeps its signature: the editor sends the rest.
        for nid in linked | {nid for nid in uploaded if nid in by_id and not links.get(nid)}:
            n = by_id[nid]
            sig = signature(n, [by_id[t]['gsvPhotoId'] for t in graph.get(nid, ())
                                if by_id[t].get('gsvConfirmed') and by_id[t].get('gsvPhotoId')])
            if nid in uploaded or same_but_links(n.get('gsvSig'), sig):
                n['gsvSig'] = sig
    finally:
        write_project(path, project)
        journal.unlink(missing_ok=True)
        print(f"\nWritten into {path}")
    print(f"\nDone! Uploaded {len(uploaded)} photo(s)")
    if any(n.get('gsvPhotoId') and not n.get('gsvConfirmed') for n in nodes):
        print("Google is still processing some photos - link them later:")
        print(f"   python upload_tour.py --project {args.project} --connect-only")
    print("Check: https://www.google.com/maps/contrib/")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Street View 360 photo tour uploader')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--folder',       help='Folder containing the JPEGs')
    source.add_argument('--project',      help="A project file of the editor (.vrtour): uploads its photos not on Google yet")
    parser.add_argument('--manifest',     default=None,  help='tour.csv path (manual lat/lng/heading/connections)')
    parser.add_argument('--place-id',     default=None,  help='Google Place ID to link the tour to')
    parser.add_argument('--dry-run',      action='store_true', help='Validate only, do not upload')
    parser.add_argument('--connect-only', action='store_true', help='Re-run only the connection step from upload_result.json')
    parser.add_argument('--wait',         type=float, default=10, help='Minutes to wait for Google before linking (default 10)')
    parser.add_argument('--delete-old',   action='store_true', help='--project: delete the old versions of photos replaced in the editor')
    args = parser.parse_args()

    if args.project:
        run_project(args)
        return

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
