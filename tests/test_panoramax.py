"""Publishing on Panoramax (a fake instance here): signing in with a token claimed in the
browser, the photos going up with their position and north, and what is sent once is not
sent again."""
import email
import email.policy
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from PIL import Image

from helpers import KIEL, STEP_8M, b64, drop, jpg

SHOWN = "id => document.getElementById(id).classList.contains('show')"


class FakeInstance(BaseHTTPRequestHandler):
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

    def authorised(self):
        return self.headers.get('Authorization') == 'Bearer JWT1' and self.state.claimed

    def do_GET(self):
        st, path = self.state, urlparse(self.path).path
        if path == '/api/auth/tokens/tok1/claim':  # the user, logged in, in the browser
            st.claimed = True
            return self.reply({})
        if path == '/api/users/me':
            return self.reply({'name': 'Ada'}) if self.authorised() else self.reply({'message': 'not logged in'}, 401)
        if path == '/api/upload_sets/set1':
            return self.reply({'ready': True, 'items_status': {'prepared': len(st.files), 'preparing': 0, 'rejected': 0},
                               'associated_collections': [{'id': 'c1'}]})
        self.reply({'message': 'not found'}, 404)

    def do_POST(self):
        st, path = self.state, urlparse(self.path).path
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if path == '/api/auth/tokens/generate':
            return self.reply({'id': 'tok1', 'jwt_token': 'JWT1', 'links': [{'rel': 'claim', 'href': f'{st.base}/api/auth/tokens/tok1/claim'}]})
        if not self.authorised():
            return self.reply({'message': 'Unauthorized'}, 401)
        if path == '/api/upload_sets':
            st.sets.append(json.loads(body))
            return self.reply({'id': 'set1'})
        if path == '/api/upload_sets/set1/files':
            msg = email.message_from_bytes(b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + body,
                                           policy=email.policy.HTTP)
            parts = {p.get_param('name', header='content-disposition'): p for p in msg.iter_parts()}
            st.files.append({'name': parts['file'].get_filename(), 'data': parts['file'].get_payload(decode=True),
                             'isBlurred': parts['isBlurred'].get_content().strip()})
            return self.reply({'picture_id': f'pic{len(st.files)}'}, 202)
        if path == '/api/upload_sets/set1/complete':
            st.completed = True
            return self.reply({})
        self.reply({'message': 'not found'}, 404)


@pytest.fixture
def instance():
    srv = ThreadingHTTPServer(('127.0.0.1', 0), FakeInstance)
    FakeInstance.state = SimpleNamespace(base=f'http://127.0.0.1:{srv.server_port}', claimed=False, sets=[], files=[], completed=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield FakeInstance.state
    srv.shutdown()


def test_publish_a_tour_on_panoramax(editor, instance):
    page = editor.page
    drop(page, [b64(f'p{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=90, taken=f'2026:05:01 14:00:0{k}')) for k in range(2)]
         + [b64('notime.jpg', jpg(lat=KIEL[0] + 0.001, lng=KIEL[1]))])
    page.click('#btools')
    page.click('button[data-run=panoramax]')
    page.wait_for_function(SHOWN, arg='pmx-back')
    page.wait_for_function("document.querySelectorAll('#pmx-instance option').length > 1")
    page.select_option('#pmx-instance', '')
    page.fill('#pmx-other', instance.base)
    with page.context.expect_page() as claim_page:  # the claim link opens in the browser
        page.click('#pmx-connect')
    claim_page.value.wait_for_load_state()
    page.wait_for_function("!document.getElementById('pmx-ready').hidden", timeout=20000)
    assert page.inner_text('#pmx-account') == f"Connected to {instance.base.replace('http://', '')} as Ada"
    assert page.inner_text('#pmx-sum') == '2 photo(s) to publish · 1 without a capture time - Panoramax needs one'

    page.click('#pmx-go')
    page.wait_for_function("document.getElementById('pmx-status').textContent.includes('Panoramax:')", timeout=30000)
    assert instance.completed and instance.sets[0]['estimated_nb_files'] == 2 and instance.sets[0]['no_split'] is True
    assert sorted(f['isBlurred'] for f in instance.files) == ['false', 'false']  # Panoramax blurs too
    sent = Image.open(io.BytesIO(instance.files[0]['data']))
    gps = sent.getexif().get_ifd(0x8825)
    assert gps.get(1) == 'N' and abs(float(gps[17]) - 90) < 0.01
    assert page.inner_text('#pmx-sum').startswith('0 photo(s) to publish · 2 on Panoramax already')

    page.click('#pmx-forget')
    page.wait_for_function("!document.getElementById('pmx-signin').hidden")
