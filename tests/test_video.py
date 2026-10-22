"""360° video to Street View: the editor checks the video and the GPX track, the app streams the
video to Google (a fake one here, as the Street View Publish API answers) and lists the sequences."""
import hashlib
import json
import struct
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import launch_app, open_editor, start

MAC_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
REAL_START = datetime(2026, 6, 14, 9, 30, 0, tzinfo=timezone.utc)


def box(kind, payload=b''):
    return struct.pack('>I', 8 + len(payload)) + kind + payload


def mp4(created, seconds=60, width=5760, height=2880, spherical=True, camm=False, data=3 * 1024 * 1024):
    """A small MP4 as cameras write it, with the header (moov) after the video data."""
    ct = int((created - MAC_EPOCH).total_seconds())
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


def gpx(start, seconds, skip=()):
    """A track with a point every second from 5 s before `start`, leaving out the seconds in `skip`."""
    pts = ''.join(
        f'<trkpt lat="{54.32 + i * 1e-5:.6f}" lon="{10.12 + i * 1e-5:.6f}"><time>{(start + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>'
        for i in range(-5, seconds + 6) if i not in skip)
    return f'<?xml version="1.0"?><gpx version="1.1" creator="test"><trk><trkseg>{pts}</trkseg></trk></gpx>'.encode()


class FakeStreetView(BaseHTTPRequestHandler):
    """photoSequence:startUpload, the upload URL, photoSequence (create, get, delete), photoSequences."""
    state = None

    def log_message(self, *args):
        pass

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        st, url = self.state, urlparse(self.path)
        st.auth.add(self.headers.get('Authorization'))
        length = int(self.headers.get('Content-Length') or 0)
        if url.path == '/v1/photoSequence:startUpload':
            self.rfile.read(length)
            return self.reply({'uploadUrl': f'{st.base}/media/upload/seq-new'})
        if url.path == '/media/upload/seq-new':
            digest, left = hashlib.sha256(), length
            while left:
                chunk = self.rfile.read(min(left, 256 * 1024))
                digest.update(chunk)
                left -= len(chunk)
            st.uploads.append({'size': length, 'sha': digest.hexdigest(), 'type': self.headers.get('Content-Type'),
                               'protocol': self.headers.get('X-Goog-Upload-Protocol')})
            return self.reply({})
        if url.path == '/v1/photoSequence':
            st.created.append({'query': parse_qs(url.query), 'body': json.loads(self.rfile.read(length))})
            st.sequences['seq-new'] = {'processingState': 'PENDING', 'filename': 'walk.mp4'}
            return self.reply({'name': 'seq-new'})
        self.reply({'error': {'message': 'not found'}}, 404)

    def do_GET(self):
        st, url = self.state, urlparse(self.path)
        if url.path == '/v1/photoSequences':
            return self.reply({'photoSequences': [{'name': sid} for sid in st.sequences]})
        sid = url.path.rsplit('/', 1)[-1]
        if url.path.startswith('/v1/photoSequence/') and sid in st.sequences:
            return self.reply({'name': sid, 'done': True, 'response': {'id': sid, **st.sequences[sid]}})
        self.reply({'error': {'message': 'not found'}}, 404)

    def do_DELETE(self):
        sid = urlparse(self.path).path.rsplit('/', 1)[-1]
        self.state.deleted.append(sid)
        self.state.sequences.pop(sid, None)
        self.reply({})


@pytest.fixture(scope='module')
def google():
    srv = ThreadingHTTPServer(('127.0.0.1', 0), FakeStreetView)
    st = SimpleNamespace(base=f'http://127.0.0.1:{srv.server_port}', uploads=[], created=[], deleted=[], auth=set(), sequences={
        'seq-a': {'processingState': 'PROCESSED', 'filename': 'harbour.mp4', 'photos': [{}, {}, {}], 'distanceMeters': 412.6,
                  'viewCount': '17', 'uploadTime': '2026-05-01T10:00:00Z'},
        'seq-b': {'processingState': 'FAILED', 'failureReason': 'NO_OVERLAP_GPS', 'filename': 'old.mp4', 'uploadTime': '2026-04-01T10:00:00Z'},
    })
    FakeStreetView.state = st
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield st
    srv.shutdown()


@pytest.fixture(scope='module')
def app(tmp_path_factory, google):
    """The app, signed in (a token that is valid for long) and talking to the fake Google."""
    data = tmp_path_factory.mktemp('video-profile')
    (data / 'token.json').write_text(json.dumps({
        'token': 'test-token', 'refresh_token': 'r', 'client_id': 'c', 'client_secret': 's',
        'token_uri': 'https://oauth2.googleapis.com/token', 'expiry': '2099-01-01T00:00:00Z',
    }), encoding='utf-8')
    yield from launch_app(data, {'SVT_STREETVIEW_API': f'{google.base}/v1'})


@pytest.fixture
def page(browser, app):
    ed = start(open_editor(browser, app), app)
    yield ed.page
    errors = list(ed.errors)
    ed.context.close()
    assert not errors, errors


def facts(page, list_id):
    return page.evaluate(f"[...document.querySelectorAll('#{list_id} .check-row')].map(r => r.className.split(' ')[1] + ': ' + r.querySelector('.msg').textContent)")


def test_video_upload_with_gpx(page, google):
    page.click('#btools')
    page.click('button[data-run=video]')
    page.wait_for_function("document.querySelectorAll('#vd-list .check-row').length === 2")
    rows = page.evaluate("[...document.querySelectorAll('#vd-list .check-row')].map(r => r.textContent)")
    assert rows[0].startswith('harbour.mp4Done: 3 photo(s) over 413 m · 17 views'), rows
    assert rows[1].startswith('old.mp4Failed: the GPS track and the video do not overlap in time'), rows

    # The camera wrote its local time (UTC+2) as UTC: the video seems to start 2 h late.
    video = mp4(REAL_START + timedelta(hours=2))
    page.set_input_files('#vd-file', files=[{'name': 'walk.mp4', 'mimeType': 'video/mp4', 'buffer': video}])
    page.wait_for_function("document.querySelectorAll('#vd-facts .check-row').length >= 5")
    got = facts(page, 'vd-facts')
    assert got[:4] == ['ok: Length: 1 min 0 s', 'ok: 5760 × 2880 pixels (2:1, as a 360° video should be)', 'ok: 360° metadata found',
                       'warn: No GPS track in the video - choose a GPX file recorded at the same time.'], got
    assert page.is_checked('#vd-gps-gpx') and page.is_disabled('#vd-go')

    page.set_input_files('#vd-gpx-file', files=[{'name': 'walk.gpx', 'mimeType': 'application/gpx+xml', 'buffer': gpx(REAL_START, 60, skip=range(20, 31))}])
    got = facts(page, 'vd-gpx-facts')
    assert 'error: The track and the video do not overlap in time - check the start time.' in got, got
    assert 'warn: Shifted by -2 h they match: the camera probably wrote its local time.' in got, got
    page.click('#vd-gpx-facts button')
    got = facts(page, 'vd-gpx-facts')
    assert 'ok: The track covers the whole video (60 points).' in got, got
    assert 'ok: 5 point(s) filled in along the track where it had gaps of 5 to 30 s (Google refuses gaps over 5 s).' in got, got

    page.click('#vd-go')
    page.wait_for_function("document.getElementById('vd-status').textContent.startsWith('Uploaded.')", timeout=60000)
    assert google.uploads == [{'size': len(video), 'sha': hashlib.sha256(video).hexdigest(), 'type': 'video/mp4', 'protocol': 'raw'}]
    assert google.auth == {'Bearer test-token'}
    (created,) = google.created
    assert created['query'] == {'inputType': ['VIDEO']}
    body = created['body']
    assert body['uploadReference'] == {'uploadUrl': f'{google.base}/media/upload/seq-new'}
    assert body['gpsSource'] == 'PHOTO_SEQUENCE'
    assert body['captureTimeOverride'] == '2026-06-14T09:30:00.000Z'
    timeline = body['rawGpsTimeline']
    assert len(timeline) == 60 + 5
    assert timeline[0] == {'latLngPair': {'latitude': 54.31995, 'longitude': 10.11995}, 'gpsRecordTimestampUnixEpoch': '2026-06-14T09:29:55.000Z'}
    stamps = [datetime.fromisoformat(p['gpsRecordTimestampUnixEpoch'].replace('Z', '+00:00')) for p in timeline]
    assert max((b - a).total_seconds() for a, b in zip(stamps, stamps[1:])) <= 2

    # the new one shows up; deleting the failed one asks first, then it is gone
    page.wait_for_function("[...document.querySelectorAll('#vd-list .check-row')].some(r => r.textContent.includes('Waiting to be processed'))")
    page.click('#vd-list .check-row:has-text("old.mp4") button')
    page.wait_for_function("![...document.querySelectorAll('#vd-list .check-row')].some(r => r.textContent.includes('old.mp4'))")
    assert google.deleted == ['seq-b']


def test_video_facts(page):
    info = page.evaluate("""async () => {
      const v = await import('/js/video.js');
      const pts = [0, 1, 2, 40, 41].map(s => ({ lat: 1, lng: 2, t: s * 1000 }));
      return v.gpsTimeline(pts, 0, 41);
    }""")
    assert info['gapS'] == 38 and info['filled'] == 0 and info['recorded'] == 5
    page.click('#btools')
    page.click('button[data-run=video]')
    page.set_input_files('#vd-file', files=[{'name': 'flat.mp4', 'mimeType': 'video/mp4', 'buffer': mp4(REAL_START, width=3840, height=1920, camm=True, spherical=False, data=1000)}])
    page.wait_for_function("document.querySelectorAll('#vd-facts .check-row').length >= 5")
    got = facts(page, 'vd-facts')
    assert 'warn: No 360° metadata found' in ' | '.join(got)
    assert 'ok: Camera motion track (CAMM) found - Google reads the GPS from it, if the camera recorded GPS.' in got
    assert page.is_checked('#vd-gps-camm') and not page.is_disabled('#vd-go')
    page.set_input_files('#vd-file', files=[{'name': 'notes.mp4', 'mimeType': 'video/mp4', 'buffer': b'not a video at all'}])
    page.wait_for_function("document.querySelector('#vd-facts .check-row.error')")
    assert facts(page, 'vd-facts') == ['error: This is not an MP4 or MOV video that can be read.']
