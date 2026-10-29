"""The app's local server: what it sends, whom it answers, and the Places lookups (against fakes)."""
import functools
import http.server
import importlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import REPO, free_port


def get(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def test_editor_files_are_never_cached(server):
    for path, mime in [('/editor.html', 'text/html'), ('/vendor/three/three.module.min.js', 'text/javascript'),
                       ('/vendor/mediapipe/wasm/vision_wasm_internal.wasm', 'application/wasm'),
                       ('/vendor/manrope/manrope-latin-wght-normal.woff2', 'font/woff2')]:
        status, headers, _ = get(server.base + path)
        assert status == 200, path
        assert headers['Cache-Control'] == 'no-store', path
        assert headers['Content-type'].startswith(mime), (path, headers['Content-type'])


def test_root_opens_the_versioned_editor(server):
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(server.base + '/')
    assert e.value.code == 302
    assert '/editor.html?v=' in e.value.headers['Location']


def test_api_needs_the_editor_key(server):
    status, headers, body = get(server.base + '/api/app-info')
    assert status == 401 and headers.get('X-Editor-Auth') == 'bad-key'
    status, _, body = get(server.base + '/api/app-info', {'X-Editor-Key': server.key})
    assert status == 200 and json.loads(body)['window'] is False


def test_other_sites_are_turned_away(server):
    assert get(server.base + '/editor.html', {'Host': 'evil.example'})[0] == 403
    assert get(server.base + '/api/app-info', {'X-Editor-Key': server.key, 'Origin': 'https://evil.example'})[0] == 403


def test_vendored_libraries_match_the_lock():
    r = subprocess.run([sys.executable, str(REPO / 'packaging' / 'vendor.py'), '--check'], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ── in-process: the server code against fake Google services ─────────────

GOOD = 'AIzaGOODKEY_0123456789abcdefghijklm'
PLACES_ONLY = 'AIzaPLACESONLY_0123456789abcdefghij'


class FakeMaps(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def answer(self, out, code=200):
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # Places API (New), searchNearby
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        FakeMaps.calls.append(('places', self.headers.get('X-Goog-Api-Key'), self.headers.get('X-Goog-FieldMask'), body))
        if self.headers.get('X-Goog-Api-Key') not in (GOOD, PLACES_ONLY):
            return self.answer({'error': {'code': 400, 'message': 'API key not valid. Please pass a valid API key.'}}, 400)
        c = body['locationRestriction']['circle']['center']
        self.answer({'places': [{'id': 'ChIJrathaus0000000000', 'displayName': {'text': 'Rathaus'}, 'formattedAddress': 'Fleethörn 9, Kiel',
                                 'location': {'latitude': c['latitude'] + 0.0001, 'longitude': c['longitude']},
                                 'primaryTypeDisplayName': {'text': 'Rathaus'}}]})

    def do_GET(self):  # Geocoding API, reverse
        q = parse_qs(urlparse(self.path).query)
        FakeMaps.calls.append(('geocode', q['key'][0], q.get('result_type', [''])[0], q['latlng'][0], q.get('language', [''])[0]))
        if q['key'][0] != GOOD:
            return self.answer({'status': 'REQUEST_DENIED', 'error_message': 'This API project is not authorized to use this API.', 'results': []})
        lat, lng = map(float, q['latlng'][0].split(','))
        self.answer({'status': 'OK', 'results': [
            {'place_id': f'ChIJaddr{lat:.4f}', 'types': ['street_address'], 'formatted_address': 'Holstenstraße 12, 24103 Kiel, Germany',
             'geometry': {'location': {'lat': lat + 0.00005, 'lng': lng}}},
            {'place_id': 'ChIJholstenstrasse00', 'types': ['route'], 'formatted_address': 'Holstenstraße, 24103 Kiel, Germany',
             'address_components': [{'long_name': 'Holstenstraße', 'types': ['route']}], 'geometry': {'location': {'lat': lat, 'lng': lng}}},
        ]})


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    """run_editor imported here, with its own profile, serving on a free port; Maps APIs faked."""
    os.environ['SVT_DATA_DIR'] = str(tmp_path_factory.mktemp('inproc'))
    sys.path.insert(0, str(REPO))
    r = importlib.import_module('run_editor')
    fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeMaps)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    places = importlib.import_module('places')
    places.PLACES_URL = f'http://127.0.0.1:{fake.server_port}/v1/places:searchNearby'
    places.GEOCODE_URL = f'http://127.0.0.1:{fake.server_port}/maps/api/geocode/json'
    port = free_port()
    r.PORT = port
    r.ALLOWED_HOSTS.clear()
    r.ALLOWED_HOSTS.add(f'127.0.0.1:{port}')
    r.EDITOR_KEY = 'k' * 40
    r.Handler.log_message = lambda *a: None
    srv = ThreadingHTTPServer(('127.0.0.1', port), r.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def api(path, payload=None):
        status, _, body = get(f'http://127.0.0.1:{port}{path}', {'Content-Type': 'application/json', 'X-Editor-Key': r.EDITOR_KEY},
                              json.dumps(payload or {}).encode(), 'POST')
        return json.loads(body)
    r.api = api
    yield r
    srv.shutdown()
    fake.shutdown()


def test_places_key_is_checked_and_kept_by_the_app(app):
    assert app.api('/api/places') == {'ok': True, 'configured': False}
    assert app.api('/api/places/nearby', {'lat': 1, 'lng': 2})['error'] == 'no_key'
    assert not app.api('/api/places/key', {'key': 'hello'})['ok']
    wrong = app.api('/api/places/key', {'key': 'AIzaWRONGKEY_0123456789abcdefghijk'})
    assert not wrong['ok'] and 'not valid' in wrong['error'] and not app.PLACES_KEY_FILE.exists()
    partly = app.api('/api/places/key', {'key': PLACES_ONLY})
    assert partly['ok'] and partly['places'] and not partly['geocoding'] and 'not authorized' in partly['errors']['geocoding']
    good = app.api('/api/places/key', {'key': GOOD, 'lat': 54.32, 'lng': 10.12, 'lang': 'de'})
    assert good == {'ok': True, 'configured': True, 'places': True, 'geocoding': True, 'errors': {}}
    assert app.PLACES_KEY_FILE.read_text() == GOOD
    assert GOOD not in json.dumps(app.api('/api/places'))


def test_places_lookups_and_cache(app):
    app.api('/api/places/key', {'key': GOOD})
    res = app.api('/api/places/geocode', {'points': [{'lat': 54.3233, 'lng': 10.1228}, {'lat': 54.3240, 'lng': 10.1230}], 'lang': 'de'})
    assert res['ok'] and res['fetched'] == 2
    first = res['results'][0]
    assert (first['address']['name'], first['street']['name'], first['street']['id']) == ('Holstenstraße 12', 'Holstenstraße', 'ChIJholstenstrasse00')
    geo = [c for c in FakeMaps.calls if c[0] == 'geocode'][-1]
    assert geo[2] == 'street_address|premise|subpremise|route' and geo[4] == 'de'
    again = app.api('/api/places/geocode', {'points': [{'lat': 54.3233, 'lng': 10.1228}], 'lang': 'de'})
    assert again['fetched'] == 0
    near = app.api('/api/places/nearby', {'lat': 54.3233, 'lng': 10.1228, 'radius': 99999})
    assert near['places'][0]['name'] == 'Rathaus' and near['fetched'] == 1
    body = [c for c in FakeMaps.calls if c[0] == 'places'][-1][3]
    assert body['locationRestriction']['circle']['radius'] == 500.0 and body['rankPreference'] == 'DISTANCE'
    assert app.api('/api/places/key', {'key': ''}) == {'ok': True, 'configured': False}
    assert not app.PLACES_KEY_FILE.exists()


class FakeSession:
    """Street View Publish's photo.update as _update_photos sends it: PUT /photo/{id}?updateMask=…"""

    def __init__(self, refuse=lambda body: False):
        self.calls, self.refuse = [], refuse

    def put(self, url, params, json):
        self.calls.append((url.rsplit('/', 1)[-1], params['updateMask'], json.get('places'), json.get('connections')))
        refused = self.refuse(json)
        return SimpleNamespace(ok=not refused, status_code=400 if refused else 200,
                               json=lambda: {'error': {'message': 'Invalid place id'}}, text='', reason='')


def test_a_refused_place_does_not_hold_up_an_update(app, monkeypatch):
    google_api = importlib.import_module('google_api')
    fake = FakeSession(lambda body: bool(body.get('places')) and body['places'][0]['placeId'].startswith('BAD'))
    monkeypatch.setattr(google_api, '_session', lambda *a, **k: fake)
    monkeypatch.setattr(google_api.time, 'sleep', lambda s: None)
    out = app._update_photos(None, [
        {'id': 'p1', 'lat': 1, 'lng': 2, 'heading': 90, 'connections': ['p2'], 'placeId': 'ChIJgood'},
        {'id': 'p2', 'lat': 1, 'lng': 2, 'heading': None, 'connections': [], 'placeId': 'BADplace'},
        {'id': 'p3', 'lat': 1, 'lng': 2, 'connections': [], 'placeId': None},
        {'id': 'p4', 'lat': 1, 'lng': 2, 'connections': []},
    ])
    assert out == [{'id': 'p1', 'ok': True}, {'id': 'p2', 'ok': True, 'placeRejected': True}, {'id': 'p3', 'ok': True}, {'id': 'p4', 'ok': True}]
    assert [c[:3] for c in fake.calls] == [('p1', 'pose.latLngPair,connections,pose.heading,places', [{'placeId': 'ChIJgood'}]),
                                           ('p2', 'pose.latLngPair,connections,places', [{'placeId': 'BADplace'}]),
                                           ('p2', 'pose.latLngPair,connections', None),
                                           ('p3', 'pose.latLngPair,connections,places', []),
                                           ('p4', 'pose.latLngPair,connections', None)]


def test_an_update_without_links_leaves_them_on_google(app, monkeypatch):
    """Only a changed heading: the links on Google (also to photos outside the project) stay."""
    google_api = importlib.import_module('google_api')
    fake = FakeSession()
    monkeypatch.setattr(google_api, '_session', lambda *a, **k: fake)
    monkeypatch.setattr(google_api.time, 'sleep', lambda s: None)
    assert app._update_photos(None, [{'id': 'p1', 'lat': 1, 'lng': 2, 'heading': 45}]) == [{'id': 'p1', 'ok': True}]
    assert fake.calls == [('p1', 'pose.latLngPair,pose.heading', None, None)]


def test_an_update_shows_the_new_editor_even_if_the_old_one_was_cached(app, pw, tmp_path):
    """A web engine that cached the old editor (sent without Cache-Control) must still get the new one."""
    from conftest import CHANNEL
    site = tmp_path / 'site'
    site.mkdir()
    page_file = site / 'editor.html'

    def write(text, age_days=0):
        page_file.write_text(f'<!doctype html><title>{text}</title><body>{text}</body>', encoding='utf-8')
        t = time.time() - age_days * 86400
        os.utime(page_file, (t, t))

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    def serve(handler):
        srv = ThreadingHTTPServer(('127.0.0.1', port), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def look(url):  # one browser start with the same profile: its cache survives
        ctx = pw.chromium.launch_persistent_context(str(tmp_path / 'profile'), channel=CHANNEL, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url)
        text = page.inner_text('body')
        ctx.close()
        return text

    port = free_port()
    write('OLD', age_days=30)
    old = serve(functools.partial(Quiet, directory=str(site)))
    assert look(f'http://127.0.0.1:{port}/editor.html') == 'OLD'
    old.shutdown(); old.server_close()
    write('NEW')
    old = serve(functools.partial(Quiet, directory=str(site)))
    assert look(f'http://127.0.0.1:{port}/editor.html') == 'OLD'  # the problem: still the cached one
    old.shutdown(); old.server_close()

    saved = (app.PORT, app.EDITOR_DIR, app.APP_VERSION, set(app.ALLOWED_HOSTS))
    app.PORT, app.EDITOR_DIR, app.APP_VERSION = port, site, '9.9.9'
    app.ALLOWED_HOSTS.clear()
    app.ALLOWED_HOSTS.add(f'127.0.0.1:{port}')
    try:
        new = serve(app.Handler)
        url = app._editor_url(key=False)
        assert url.endswith('/editor.html?v=9.9.9')
        assert look(url) == 'NEW'
        write('NEWER')
        assert look(url) == 'NEWER'
        new.shutdown(); new.server_close()
    finally:
        app.PORT, app.EDITOR_DIR, app.APP_VERSION = saved[:3]
        app.ALLOWED_HOSTS.clear()
        app.ALLOWED_HOSTS.update(saved[3])
