"""The sign-in survives being offline, files the page may only read, the command-line tools on
the app's Google code, and a point's name that can't run anything in the tour viewer."""
import json
import sys

import pytest

from conftest import REPO
from helpers import KIEL, STEP_8M, b64, drop, jpg

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'uploader'))
import google_api  # noqa: E402

SHOWN = "id => document.getElementById(id).classList.contains('show')"
TOKEN = {'token': 'old', 'refresh_token': 'r', 'client_id': 'c', 'client_secret': 's',
         'token_uri': 'https://oauth2.googleapis.com/token', 'expiry': '2000-01-01T00:00:00Z'}


@pytest.fixture
def sign_in(tmp_path):
    (tmp_path / 'token.json').write_text(json.dumps(TOKEN), encoding='utf-8')
    return google_api.SignIn(tmp_path / 'token.json', tmp_path / 'client_secrets.json')


def test_offline_keeps_the_sign_in(sign_in, monkeypatch):
    from google.auth.exceptions import TransportError
    from google.oauth2.credentials import Credentials

    def offline(self, request):
        raise TransportError('Failed to establish a new connection')
    monkeypatch.setattr(Credentials, 'refresh', offline)
    with pytest.raises(google_api.GoogleError) as err:
        sign_in.credentials()
    assert err.value.status == 0 and 'internet' in err.value.message
    assert sign_in.token_file.exists()  # still signed in, just not reachable
    assert sign_in.signed_in()


def test_a_revoked_sign_in_ends(sign_in, monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    def revoked(self, request):
        raise RefreshError('invalid_grant: Token has been expired or revoked.')
    monkeypatch.setattr(Credentials, 'refresh', revoked)
    with pytest.raises(RuntimeError, match='not_signed_in'):
        sign_in.credentials()
    assert not sign_in.token_file.exists()
    assert not sign_in.signed_in()


def test_a_renewed_sign_in_is_written_in_one_go(sign_in, monkeypatch):
    from google.oauth2.credentials import Credentials

    def renew(self, request):
        self.token = 'new'
        self.expiry = __import__('datetime').datetime(2099, 1, 1)
    monkeypatch.setattr(Credentials, 'refresh', renew)
    assert sign_in.credentials().token == 'new'
    assert json.loads(sign_in.token_file.read_text())['token'] == 'new'
    assert not list(sign_in.token_file.parent.glob('.*.tmp'))
    # kept in memory: no second refresh while it is valid
    monkeypatch.setattr(Credentials, 'refresh', lambda self, r: pytest.fail('refreshed again'))
    assert sign_in.credentials().token == 'new'


def test_an_unreadable_token_means_signing_in_again(sign_in):
    sign_in.token_file.write_text('{"token": ', encoding='utf-8')  # half written by something else
    with pytest.raises(RuntimeError, match='not_signed_in'):
        sign_in.credentials()


def test_photo_upload_tries_again_where_it_is_safe(monkeypatch):
    """startUpload and the bytes again after a broken connection; the photo itself only when Google said it is busy."""
    requests, *_ = google_api._google_libs()
    calls = []

    class Session:
        headers = {}

        def post(self, url, data=None, **kw):
            step = url.rsplit('/', 1)[-1]
            calls.append(step)
            if step == 'photo:startUpload' and calls.count(step) == 1:
                raise requests.ConnectionError('reset')
            if step == 'up' and calls.count(step) == 1:
                return Resp(503, {})
            if step == 'photo' and calls.count(step) == 1:
                raise requests.ReadTimeout('no answer')  # may have been made: not again
            return Resp(200, {'uploadUrl': f'{google_api.BASE_URL}/up', 'photoId': {'id': 'P1'}})

    class Resp:
        def __init__(self, status, body):
            self.status_code, self.ok, self.body, self.text, self.reason = status, status < 400, body, '', ''

        def json(self):
            return self.body
    monkeypatch.setattr(google_api, '_session', lambda *a, **k: Session())
    monkeypatch.setattr(google_api.time, 'sleep', lambda s: None)
    creds = type('C', (), {'token': 't'})()
    with pytest.raises(google_api.GoogleError) as err:
        google_api._upload_photo(creds, b'jpeg', 'a.jpg', 1, 2)
    assert calls == ['photo:startUpload', 'photo:startUpload', 'up', 'up', 'photo'] and err.value.status == 0


def test_cli_links_and_waits_for_google(monkeypatch):
    import upload_tour
    assert upload_tour.linear_links(['a', 'b', 'c']) == {'a': ['b'], 'b': ['a', 'c'], 'c': ['b']}
    rows = [{'filename': 'a.jpg', 'connects_to': 'b.jpg; c.jpg'}, {'filename': 'b.jpg', 'connects_to': ''}]
    assert upload_tour.manifest_links(rows) == {'a.jpg': ['b.jpg', 'c.jpg'], 'b.jpg': []}
    status = {'idA': {'status': 'processing', 'resolvedId': 'A', 'lat': 1, 'lng': 2, 'heading': 90},
              'idB': {'status': 'processing', 'resolvedId': 'B', 'lat': 3, 'lng': 4, 'heading': None},
              'idC': {'status': 'notfound'}}
    sent = []
    monkeypatch.setattr(upload_tour, '_photo_status', lambda creds, ids: status)
    monkeypatch.setattr(upload_tour, '_update_photos', lambda creds, items: sent.extend(items) or [{'id': i['id'], 'ok': True} for i in items])
    upload_tour.connect(None, {'a.jpg': 'idA', 'b.jpg': 'idB', 'c.jpg': 'idC'}, {'a.jpg': ['b.jpg', 'c.jpg'], 'b.jpg': ['a.jpg']}, wait_minutes=0)
    # real IDs, Google's own position kept; c is not processed yet and left out
    assert sent == [{'id': 'A', 'lat': 1, 'lng': 2, 'heading': 90, 'connections': ['B']},
                    {'id': 'B', 'lat': 3, 'lng': 4, 'heading': None, 'connections': ['A']}]


def test_the_page_can_hand_over_projects_only(tmp_path):
    """/api/open-file takes a project, nothing else; files the page only opened stay read only."""
    import importlib
    import os
    os.environ['SVT_DATA_DIR'] = str(tmp_path / 'profile')
    run_editor = importlib.import_module('run_editor')
    assert '.json' not in run_editor.PROJECT_SUFFIXES
    project = tmp_path / 'tour.vrtour'
    project.write_bytes(b'PK')
    photo = tmp_path / 'photo.jpg'
    photo.write_bytes(b'\xff\xd8')
    assert run_editor.FILES[run_editor._file_token(project, 'file')]['write']
    assert not run_editor.FILES[run_editor._file_token(photo, 'file')]['write']
    assert run_editor.FILES[run_editor._file_token(photo, 'file', write=True)]['write']  # picked in a save dialog
    assert not run_editor.FILES[run_editor._file_token(tmp_path, 'folder', write=False)]['write']  # a folder to import from


def test_a_name_with_markup_runs_nothing_in_the_viewer(editor):
    page = editor.page
    evil = '<img src=x onerror="window.top.pwned=1">'
    drop(page, [b64(f'p{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=90)) for k in range(2)])
    page.evaluate("""async name => {
      const v = await import('/js/viewer.js');
      window.viewerHtml = v.genViewer('a', [{ id: 'a', name, panorama: 'a.jpg', lat: 1, lng: 2, links: [{ nodeId: 'b' }] },
                                             { id: 'b', name: 'b', panorama: 'b.jpg', lat: 1, lng: 2.0001, links: [] }]);
    }""", evil)
    html = page.evaluate('window.viewerHtml')
    assert "Content-Security-Policy" in html and "script-src 'nonce-" in html
    assert evil not in html and '&lt;img' in html
    # renamed in the editor and shown in the preview: nothing runs
    page.click('#node-list .nrow >> nth=0')
    page.fill('#ni-n', evil)
    page.click('#node-list .nrow >> nth=0', modifiers=['Control'])
    page.click('#node-list .nrow >> nth=1', modifiers=['Control'])
    page.click('#multibar-connect')
    page.click('#bpreview')
    page.wait_for_function(SHOWN, arg='preview-back')
    page.wait_for_function("document.getElementById('preview-frame').contentWindow.psv", timeout=30000)
    page.evaluate("""() => { const w = document.getElementById('preview-frame').contentWindow;
      w.document.querySelectorAll('path.leaflet-interactive').forEach(p => p.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }))); }""")
    page.wait_for_timeout(500)
    assert not page.evaluate('window.pwned')
    assert page.inner_text('#pv-align-name')  # the preview works
