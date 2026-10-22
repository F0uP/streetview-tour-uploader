"""Build editor/sample/sample.vrtour: the sample tour on the editor's start screen.

The panoramas are drawn (a street with houses, trees and clouds), so the sample
holds no real place or person. Its photos are named svt-sample-NN.jpg - the editor
never uploads photos with that name to Google.

    python packaging/make_sample.py
"""
import io
import json
import math
import random
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'editor' / 'sample' / 'sample.vrtour'
W, H = 4096, 2048  # drawn at this size, saved at half
XMP = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       '<rdf:Description xmlns:GPano="http://ns.google.com/photos/1.0/panorama/" GPano:ProjectionType="equirectangular"/>'
       '</rdf:RDF></x:xmpmeta>')
PALETTE = [(196, 164, 132), (214, 196, 170), (164, 92, 72), (226, 222, 210), (148, 156, 166), (190, 120, 90), (236, 210, 160)]
START = (54.32020, 10.13320)


def pano(seed, open_sides=False):
    """A street seen from its middle: open to the front and back (and to the sides at a crossing)."""
    rnd = random.Random(seed)
    im = Image.new('RGB', (W, H))
    d = ImageDraw.Draw(im)
    horizon = int(H * 0.52)
    for y in range(horizon):  # sky
        t = y / horizon
        d.line([(0, y), (W, y)], fill=(int(92 + 120 * t), int(150 + 80 * t), int(222 + 25 * t)))
    for _ in range(14):  # clouds
        cx, cy, r = rnd.randrange(W), rnd.randrange(int(H * 0.08), int(H * 0.32)), rnd.randrange(60, 160)
        for k in range(6):
            d.ellipse([cx + k * r * 0.5 - r, cy - r * 0.45, cx + k * r * 0.5 + r, cy + r * 0.45], fill=(246, 248, 252))
    for y in range(horizon, H):  # street
        t = (y - horizon) / (H - horizon)
        d.line([(0, y), (W, y)], fill=(int(120 - 50 * t), int(118 - 48 * t), int(116 - 44 * t)))
    x = 0
    while x < W:  # houses on both sides
        bw = rnd.randrange(160, 420)
        mid = (x + bw / 2) / W
        gap = abs(mid - 0.5) < 0.06 or min(mid, 1 - mid) < 0.06 or (open_sides and min(abs(mid - 0.25), abs(mid - 0.75)) < 0.06)
        if not gap:
            top = horizon - rnd.randrange(260, 640)
            col = rnd.choice(PALETTE)
            d.rectangle([x, top, x + bw - 6, horizon + 40], fill=col)
            d.rectangle([x, top - 18, x + bw - 6, top], fill=tuple(int(c * 0.7) for c in col))
            for wy in range(top + 40, horizon - 40, 70):
                for wx in range(x + 24, x + bw - 50, 64):
                    d.rectangle([wx, wy, wx + 30, wy + 42], fill=(60, 80, 104) if rnd.random() > 0.2 else (230, 214, 150))
            if rnd.random() > 0.5:
                d.rectangle([x + 10, horizon - 20, x + bw - 16, horizon + 30], fill=(rnd.randrange(40, 200), 70, 80))
        x += bw
    if not open_sides:
        for cx in (int(W * 0.25), int(W * 0.75)):  # trees along the street
            for k in range(-2, 3):
                tx = cx + k * 150
                d.rectangle([tx - 8, horizon - 40, tx + 8, horizon + 60], fill=(90, 64, 44))
                d.ellipse([tx - 70, horizon - 200, tx + 70, horizon - 20], fill=(70 + rnd.randrange(30), 120 + rnd.randrange(40), 70))
    d.line([(W * 0.5, horizon + 20), (W * 0.5, H)], fill=(238, 238, 230), width=10)  # lane marking
    return im.filter(ImageFilter.GaussianBlur(1.2)).resize((W // 2, H // 2), Image.LANCZOS)


def jpeg(im):
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=80, optimize=True, xmp=XMP.encode())
    return buf.getvalue()


def offset(lat, lng, metres, bearing):
    b = math.radians(bearing)
    return (lat + metres * math.cos(b) / 111320, lng + metres * math.sin(b) / (111320 * math.cos(math.radians(lat))))


def main():
    # A street going north-east, 18 m between photos, and a courtyard off the crossing.
    names = ['Start', 'Bakery', 'Crossroads', 'Bus stop', 'Fountain', 'End']
    nodes = []
    for k, name in enumerate(names):
        lat, lng = offset(*START, k * 18, 38)
        nodes.append({'name': name, 'lat': lat, 'lng': lng, 'heading': 38.0, 'seed': k, 'open': name == 'Crossroads'})
    lat, lng = offset(nodes[2]['lat'], nodes[2]['lng'], 16, 128)
    nodes.append({'name': 'Courtyard', 'lat': lat, 'lng': lng, 'heading': 128.0, 'seed': 9, 'open': False})
    links = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (2, 6)]

    files, out = {}, []
    for i, nd in enumerate(nodes):
        nid = f'n{i + 1}'
        photo = f'svt-sample-{i + 1:02d}.jpg'
        path = f'photos/{nid}/{photo}'
        files[path] = jpeg(pano(nd['seed'], nd['open']))
        out.append({
            'id': nid, 'name': nd['name'], 'lat': round(nd['lat'], 7), 'lng': round(nd['lng'], 7),
            'heading': nd['heading'], 'origHeading': nd['heading'], 'viewYaw': 0, 'origViewYaw': 0,
            'headingSource': 'manual', 'hasGps': True, 'photoName': photo, 'photoPath': path, 'photoType': 'image/jpeg',
            'hotspots': [], 'tourId': 'tsample',
        })
    out[0]['hotspots'] = [{'id': 'h30', 'yaw': 0.35, 'pitch': 0.12, 'title': 'Welcome',
                           'text': 'This is a sample tour with drawn photos. Click the arrows to walk along the street, '
                                   'drag to look around. In the editor, every point can be moved, linked and turned.',
                           'url': ''}]
    data = {
        'format': 'vrtour', 'version': 2, 'nodes': out,
        'conns': [{'id': f'c{20 + k}', 'from': f'n{a + 1}', 'to': f'n{b + 1}'} for k, (a, b) in enumerate(links)],
        'startId': 'n1', 'startIds': ['n1'], 'tours': {'tsample': 'Sample tour'}, 'nxt': 40,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('project.json', json.dumps(data, indent=2))
        for path, blob in files.items():
            z.writestr(path, blob)
    print(f'{OUT.relative_to(ROOT)}: {len(nodes)} photos, {OUT.stat().st_size / 1e6:.1f} MB')


if __name__ == '__main__':
    main()
