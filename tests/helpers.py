"""Test photos, projects and a fake Google for the editor tests."""
import base64
import io
import json
import struct
import zipfile
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw, ImageFilter

XMP = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       '<rdf:Description xmlns:GPano="http://ns.google.com/photos/1.0/panorama/" GPano:ProjectionType="equirectangular"{extra}/>'
       '</rdf:RDF></x:xmpmeta>')
KIEL = (54.3233, 10.1228)
STEP_8M = 8 / (111320 * 0.5835)  # 8 m east at Kiel's latitude


def dms(v):
    d = int(v)
    m = int((v - d) * 60)
    return (d, m, round(((v - d) * 60 - m) * 60, 4))


def jpg(width=2048, height=1024, color=(60, 90, 120), lat=None, lng=None, heading=None, taken=None,
        offset='+02:00', image=None, xmp_heading=None, fmt='JPEG'):
    """A 360° photo (2:1, GPano XMP) with optional GPS, compass heading and capture time ('2026:05:01 14:00:00')."""
    im = image or Image.new('RGB', (width, height), color)
    if fmt != 'JPEG':
        buf = io.BytesIO()
        im.save(buf, fmt)
        return buf.getvalue()
    exif = Image.Exif()
    if lat is not None:
        gps = {1: 'N', 2: dms(lat), 3: 'E', 4: dms(lng)}
        if heading is not None:
            gps.update({16: 'T', 17: float(heading)})
        exif[0x8825] = gps
    if taken:
        exif[0x8769] = {0x9003: taken, 0x9011: offset}
    extra = f' GPano:PoseHeadingDegrees="{xmp_heading}"' if xmp_heading is not None else ''
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=80, exif=exif.tobytes(), xmp=XMP.format(extra=extra).encode())
    return buf.getvalue()


def face_pano(size=220, width=2048, shift=0):
    """A panorama with a drawn face at the horizon (MediaPipe finds it like a real one); size is for 4096 px width."""
    height = width // 2
    k = width / 4096
    im = Image.new('RGB', (width, height), (120, 150, 190))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 1100 * k, width, height], fill=(90, 90, 90))
    cx, cy, r = width // 2 + shift, 1000 * k, size * k / 2
    d.rectangle([cx - r * 1.4, cy + r * 0.9, cx + r * 1.4, cy + r * 3.2], fill=(40, 60, 120))       # shoulders
    d.ellipse([cx - r * 1.05, cy - r * 1.35, cx + r * 1.05, cy + r * 0.2], fill=(60, 40, 30))     # hair
    d.ellipse([cx - r * 0.85, cy - r * 1.1, cx + r * 0.85, cy + r * 1.1], fill=(224, 182, 150))   # face
    for sx in (-1, 1):
        ex = cx + sx * r * 0.35
        d.ellipse([ex - r * 0.17, cy - r * 0.32, ex + r * 0.17, cy - r * 0.12], fill=(250, 250, 250))
        d.ellipse([ex - r * 0.08, cy - r * 0.3, ex + r * 0.08, cy - r * 0.14], fill=(50, 40, 30))
        d.line([ex - r * 0.22, cy - r * 0.45, ex + r * 0.22, cy - r * 0.5], fill=(60, 40, 30), width=max(3, int(r // 14)))
    d.polygon([(cx, cy - r * 0.1), (cx - r * 0.12, cy + r * 0.3), (cx + r * 0.12, cy + r * 0.3)], fill=(200, 150, 120))
    d.chord([cx - r * 0.35, cy + r * 0.35, cx + r * 0.35, cy + r * 0.7], 0, 180, fill=(170, 60, 60))
    return im.filter(ImageFilter.GaussianBlur(1.2))


MAC_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)  # MP4 times count from 1904


def box(kind, payload=b''):
    return struct.pack('>I', 8 + len(payload)) + kind + payload


def mp4(created=None, seconds=60, width=5760, height=2880, spherical=True, camm=False, data=3 * 1024 * 1024):
    """A small MP4 as cameras write it, with the header (moov) after the video data."""
    ct = int(((created or datetime(2026, 6, 14, 9, 30, tzinfo=timezone.utc)) - MAC_EPOCH).total_seconds())
    mvhd = box(b'mvhd', b'\0' * 4 + struct.pack('>IIII', ct, ct, 1000, seconds * 1000) + b'\0' * 80)
    tkhd = box(b'tkhd', b'\0' * 76 + struct.pack('>II', width << 16, height << 16))
    hdlr = box(b'hdlr', b'\0' * 8 + b'vide' + b'\0' * 12 + b'VideoHandler\0')
    trak = box(b'trak', tkhd + box(b'mdia', hdlr))
    if spherical:
        trak += box(b'uuid', b'\xff\xcc\x82\x63\xf8\x55\x4a\x93\x88\x14\x58\x7a\x02\x52\x1f\xdd'
                    b'<rdf:SphericalVideo><GSpherical:Spherical>true</GSpherical:Spherical></rdf:SphericalVideo>')
    if camm:
        trak += box(b'trak', box(b'mdia', box(b'minf', box(b'stbl', box(b'stsd', b'\0' * 8 + box(b'camm', b'\0' * 8))))))
    body = bytes((i * 7) % 251 for i in range(data))
    return box(b'ftyp', b'isom\0\0\0\0isommp41') + box(b'mdat', body) + box(b'moov', mvhd + trak)


def b64(name, data, mime='image/jpeg'):
    return [name, base64.b64encode(data).decode(), mime]


DROP_JS = """async ([files, x, y]) => {
  const dt = new DataTransfer();
  files.forEach(([name, b, type]) => dt.items.add(new File([Uint8Array.from(atob(b), c => c.charCodeAt(0))], name, { type })));
  const mw = document.getElementById('mw'); const r = mw.getBoundingClientRect();
  mw.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true, clientX: r.x + x, clientY: r.y + y }));
  await new Promise(r => setTimeout(r, 300));
}"""

CONFIRM_IMPORT_JS = """async (opts) => {
  if (!document.getElementById('imp-back').classList.contains('show')) return false;
  for (let i = 0; i < 300 && document.getElementById('imp-go').disabled; i++) await new Promise(r => setTimeout(r, 100));
  for (const [id, on] of Object.entries(opts)) document.getElementById(id).checked = on;
  document.getElementById('imp-go').click();
  await new Promise(r => setTimeout(r, 800));
  return true;
}"""

# The import assistant without its automatics: photos land as they are.
PLAIN = {'imp-connect': False, 'imp-north': False, 'imp-check': False}


def drop(page, files, x=300, y=300, confirm=PLAIN):
    """Drop files on the map. Several photos open the import assistant: confirmed with `confirm` (None: left open)."""
    page.evaluate(DROP_JS, [files, x, y])
    if confirm is not None:
        page.evaluate(CONFIRM_IMPORT_JS, confirm)
    page.wait_for_timeout(500)


def vrtour(path, name, lat, lng, step=0.0001, n=3, start='n1', gsv=None, extra=None):
    """An older-format .vrtour (no tours inside): a chain of n photos going east."""
    nodes, conns = [], []
    for i in range(n):
        node = {'id': f'n{i + 1}', 'name': f'{name}-{i + 1}', 'lat': lat, 'lng': lng + i * step, 'heading': 90.0,
                'origHeading': None, 'viewYaw': 0, 'origViewYaw': 0, 'headingSource': 'manual', 'hasGps': True,
                'photoName': f'{name}{i + 1}.jpg', 'photoPath': f'photos/n{i + 1}/{name}{i + 1}.jpg', 'photoType': 'image/jpeg',
                'hotspots': [{'id': f'h{10 + i}', 'yaw': 0, 'pitch': 0, 'title': 'i'}] if i == 0 else []}
        if gsv:
            node.update({'gsvPhotoId': f'{gsv}{i}', 'gsvConfirmed': True})
        nodes.append(node)
        if i:
            conns.append({'id': f'c{20 + i}', 'from': f'n{i}', 'to': f'n{i + 1}'})
    data = {'format': 'vrtour', 'version': 1, 'nodes': nodes, 'conns': conns, 'startId': start, 'nxt': 40, **(extra or {})}
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('project.json', json.dumps(data))
        for i, nd in enumerate(nodes):
            z.writestr(nd['photoPath'], jpg(color=(40 + 30 * i, 90, 120)))
    return path


def file_item(path, mime='application/zip'):
    with open(path, 'rb') as f:
        return b64(path.name, f.read(), mime)


class FakeGoogle:
    """Street View Publish as the editor sees it through the app: uploads, status, updates, deletes."""

    def __init__(self, page, reject_place=lambda pid: False):
        self.uploads, self.updates, self.deleted, self.queries = [], [], [], []
        self.bodies = {}  # file name -> the bytes uploaded
        self.views = {}  # photo id -> view count Google reports
        self.ids = iter(f'g{i}' for i in range(1000))
        self.reject_place = reject_place
        page.route('**/api/status', lambda r: r.fulfill(json={'signedIn': True, 'hasCredentials': True}))
        page.route('**/api/upload-progress*', lambda r: r.fulfill(json={}))
        page.route('**/api/list-photos', lambda r: r.fulfill(json={'ok': True, 'photos': self.listed()}))
        page.route('**/api/upload-photo*', self._upload)
        page.route('**/api/photo-status', lambda r: r.fulfill(json={'ok': True, 'photos': {
            i: {'status': 'processing', 'resolvedId': i, **({'viewCount': self.views[i]} if i in self.views else {})}
            for i in json.loads(r.request.post_data)['ids']}}))
        page.route('**/api/update-photos', self._update)
        page.route('**/api/delete-photos', self._delete)

    def listed(self):
        return []

    def _upload(self, route):
        q = parse_qs(urlparse(route.request.url).query, keep_blank_values=True)
        self.queries.append({k: v[0] for k, v in q.items()})
        place = q.get('place_id', [None])[0]
        self.uploads.append((q['filename'][0], place))
        self.bodies[q['filename'][0]] = route.request.post_data_buffer
        if place and self.reject_place(place):
            return route.fulfill(json={'ok': False, 'error': f'Invalid place id: {place}', 'status': 400, 'step': 'create', 'kind': 'place'})
        route.fulfill(json={'ok': True, 'photoId': next(self.ids)})

    def _update(self, route):
        items = json.loads(route.request.post_data)['items']
        self.updates.append(items)
        route.fulfill(json={'ok': True, 'results': [{'id': i['id'], 'ok': True} for i in items]})

    def _delete(self, route):
        ids = json.loads(route.request.post_data)['ids']
        self.deleted.extend(ids)
        route.fulfill(json={'ok': True, 'results': [{'id': i, 'ok': True} for i in ids]})


def run_upload(page, timeout=120000):
    """Start the upload in the open upload dialog, publish after the review and wait until it is done."""
    page.click('#upl-start')
    page.wait_for_timeout(1200)
    if page.evaluate("document.getElementById('check-back').classList.contains('show')"):
        page.click('#check-continue')
        page.wait_for_timeout(800)
    # the review before publishing
    page.wait_for_function("document.getElementById('review-back').classList.contains('show')")
    page.click('#review-go')
    page.wait_for_function("!document.getElementById('up-finish').hidden", timeout=timeout)
