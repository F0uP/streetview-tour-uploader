"""The bug report (nothing secret in it) and how copies that can't update themselves are told to update."""
import io
import json
import sys
import zipfile

from conftest import REPO
from helpers import KIEL, b64, drop, jpg

sys.path.insert(0, str(REPO))
import app_update  # noqa: E402
import bug_report  # noqa: E402

LOG = '''GET /api/status?k=Zx81_secretEditorKey
token refreshed: ya29.a0AfB_byC-secret-access-token
refresh 1//0gAbCdEfGhIjKlMnOpQrStUvWx-secret
client GOCSPX-AbCdEf123secret and key=AIzaSyA1234567890abcdefghijklmnopqrstu
{"refresh_token": "1/whatever", "client_id": "123.apps.googleusercontent.com"}
Authorization: Bearer abc.def-ghi
'''
SECRETS = ['Zx81_secretEditorKey', 'secret-access-token', 'secret-access', 'GOCSPX-AbCdEf123secret',
           'AIzaSyA1234567890abcdefghijklmnopqrstu', '1/whatever', 'abc.def-ghi']


def test_bug_report_keeps_secrets_out(tmp_path):
    (tmp_path / 'editor.log').write_text(LOG, encoding='utf-8')
    (tmp_path / 'token.json').write_text('{"token": "TOKEN-CONTENT-123"}', encoding='utf-8')
    (tmp_path / 'client_secrets.json').write_text('{"client_secret": "CLIENT-SECRET-456"}', encoding='utf-8')
    data = bug_report.build('1.2.3', tmp_path, {'messages': [{'text': 'Saved x.vrtour'}]}, packaged=True, window=True,
                            signed_in=True, places_key=True, secrets=['Zx81_secretEditorKey'])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert sorted(z.namelist()) == ['editor-page.json', 'editor.log', 'report.txt']
        text = {name: z.read(name).decode() for name in z.namelist()}
    everything = '\n'.join(text.values())
    for secret in SECRETS + ['TOKEN-CONTENT-123', 'CLIENT-SECRET-456']:
        assert secret not in everything, secret
    assert '123.apps.googleusercontent.com' in text['editor.log']  # not a secret, helps
    assert 'App version: 1.2.3 (installed package)' in text['report.txt']
    assert 'token.json' in text['report.txt'] and 'Signed in to Google: yes' in text['report.txt']
    assert json.loads(text['editor-page.json'])['messages'][0]['text'] == 'Saved x.vrtour'


ASSETS = {name: f'https://github.com/F0uP/streetview-tour-uploader/releases/download/v2.0.0/{name}' for name in [
    'StreetviewTourEditor-2.0.0-Setup.exe', 'StreetviewTourEditor-2.0.0-macos-arm64.dmg', 'StreetviewTourEditor-2.0.0-macos-x64.dmg',
    'StreetviewTourEditor-2.0.0-windows-x64-portable.zip', 'streetview-tour-editor_2.0.0_all.deb']}


def test_how_to_update(monkeypatch, tmp_path):
    monkeypatch.setattr(app_update.sys, 'platform', 'darwin')
    monkeypatch.setattr(app_update.platform, 'machine', lambda: 'arm64')
    assert app_update.how_to_update(ASSETS, '2.0.0')['name'] == 'StreetviewTourEditor-2.0.0-macos-arm64.dmg'
    monkeypatch.setattr(app_update.sys, 'platform', 'linux')
    monkeypatch.setattr(app_update, 'APT_SOURCE', tmp_path / 'streetview-tour-editor.list')
    assert app_update.how_to_update(ASSETS, '2.0.0') == {'kind': 'deb', 'name': 'streetview-tour-editor_2.0.0_all.deb',
                                                        'url': ASSETS['streetview-tour-editor_2.0.0_all.deb']}
    (tmp_path / 'streetview-tour-editor.list').write_text('deb ...')
    assert app_update.how_to_update(ASSETS, '2.0.0')['kind'] == 'apt'
    monkeypatch.setattr(app_update.sys, 'platform', 'win32')
    assert app_update.how_to_update(ASSETS, '2.0.0')['kind'] in ('portable', 'page')  # x64 or 32-bit Python
    assert app_update.how_to_update({}, '2.0.0')['kind'] == 'page'


def test_update_hint_in_the_editor(editor):
    page = editor.page
    page.route('**/api/update/check*', lambda r: r.fulfill(json={
        'ok': True, 'current': '1.0.0', 'latest': '2.0.0', 'newer': True, 'canInstall': False, 'pageUrl': 'https://example.org/release',
        'howTo': {'kind': 'deb', 'name': 'streetview-tour-editor_2.0.0_all.deb', 'url': ASSETS['streetview-tour-editor_2.0.0_all.deb']}}))
    page.click('#btools')
    page.click('button[data-run=updates]')
    page.click('#update-go')
    assert page.inner_text('#update-title') == 'Version 2.0.0 is available'
    assert page.inner_text('.update-cmd') == 'sudo apt install ./streetview-tour-editor_2.0.0_all.deb'
    assert page.get_attribute('.update-howto a.btn', 'href') == ASSETS['streetview-tour-editor_2.0.0_all.deb']


def test_bug_report_from_the_editor(editor, server):
    page = editor.page
    drop(page, [b64('secret-street.jpg', jpg(lat=KIEL[0], lng=KIEL[1]))])
    page.click('#btools')
    page.click('button[data-run=report]')
    with page.expect_download() as dl:
        page.click('#report-save')
    with zipfile.ZipFile(dl.value.path()) as z:
        assert sorted(z.namelist()) == ['editor-page.json', 'editor.log', 'report.txt']
        info = json.loads(z.read('editor-page.json'))
        assert info['tour']['points'] == 1 and info['tour']['withPhoto'] == 1
        assert 'secret-street' not in json.dumps(info['tour'])
        assert server.key not in z.read('editor-page.json').decode() + z.read('report.txt').decode()
    assert page.inner_text('#report-status').startswith('Saved.')
