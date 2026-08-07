"""
Serve the tour editor over http://127.0.0.1 and open it in the browser.

Opening editor/editor.html directly as a file:// URL sends no Referer header,
which OpenStreetMap's tile servers now reject (403 "Access blocked"), and it
also disables the File System Access API used for saving exports straight to
a folder. Serving over localhost fixes both.

This server also exposes a small JSON API under /api/ so the editor's
"Upload to Google Street View" button can sign in with Google and publish
photos directly, without needing the separate gen_tour_csv.py / upload_tour.py
commands. It reuses the same client_secrets.json / token.json as those
scripts (see uploader/upload_tour.py), so either workflow can be used
interchangeably.

Usage:
  python run_editor.py
"""

import json
import os
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer, BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = 8420

# When bundled into a .exe (PyInstaller), __file__ resolves to a temporary
# extraction folder that is wiped after the app closes, so credentials must
# be anchored to a persistent folder instead. sys.executable is python.exe in
# normal `python run_editor.py` use, which is why this only takes the frozen
# branch when actually running as a bundled executable. build_exe.py places
# editor/ and uploader/ two levels up from the .exe (see its docstring for
# why they can't live right next to it), so mirror that layout here.
if getattr(sys, 'frozen', False):
    ROOT = Path(sys.executable).parent.parent.resolve()
else:
    ROOT = Path(__file__).parent.resolve()

EDITOR_DIR = ROOT / "editor"
UPLOADER_DIR = ROOT / "uploader"
UPLOADER_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = UPLOADER_DIR / "token.json"
CREDENTIALS_FILE = UPLOADER_DIR / "client_secrets.json"
SCOPES = ['https://www.googleapis.com/auth/streetviewpublish']
BASE_URL = 'https://streetviewpublish.googleapis.com/v1'


def _google_libs():
    """Import the Google/requests libs on demand so plain editing (no upload) never needs them installed."""
    try:
        import requests
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleRequest
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build as build_service
        return requests, Credentials, GoogleRequest, InstalledAppFlow, build_service
    except ImportError as e:
        raise RuntimeError(
            "Missing dependencies for uploading. Run: pip install -r requirements.txt"
        ) from e


def _load_credentials():
    """Return valid cached credentials, refreshing if needed. Raises RuntimeError if sign-in is required."""
    _, Credentials, GoogleRequest, _, _ = _google_libs()
    if not TOKEN_FILE.exists():
        raise RuntimeError("not_signed_in")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            TOKEN_FILE.write_text(creds.to_json())
        else:
            raise RuntimeError("not_signed_in")
    return creds


def _sign_in():
    """Run the interactive OAuth consent flow (opens the browser) and cache the resulting token."""
    _, _, _, InstalledAppFlow, _ = _google_libs()
    import socket as _socket

    if not CREDENTIALS_FILE.exists():
        raise RuntimeError(
            f"client_secrets.json not found. Save your OAuth desktop client credentials to {CREDENTIALS_FILE}"
        )

    OAUTH_PORT = 8085
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    flow.redirect_uri = f'http://127.0.0.1:{OAUTH_PORT}/'
    auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')

    received = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            received['code'] = params.get('code', [None])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write('Authentication complete. You can close this tab.'.encode())

        def log_message(self, *args):
            pass

    class _DualStackServer(HTTPServer):
        address_family = _socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 0)
            super().server_bind()

    try:
        server = _DualStackServer(('::', OAUTH_PORT), _Handler)
    except OSError:
        server = HTTPServer(('0.0.0.0', OAUTH_PORT), _Handler)
        flow.redirect_uri = f'http://127.0.0.1:{OAUTH_PORT}/'
        auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')

    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    webbrowser.open(auth_url)
    t.join(timeout=120)
    server.server_close()

    if not received.get('code'):
        raise RuntimeError("Sign-in timed out or was cancelled. Please try again.")

    flow.fetch_token(code=received['code'])
    creds = flow.credentials
    TOKEN_FILE.write_text(creds.to_json())
    return creds


def _save_credentials(client_id, client_secret):
    """Write a client_secrets.json in Google's standard 'installed app' shape
    from just the two values shown on the OAuth client's page in Google Cloud
    Console, so the user never has to download or hand-edit a JSON file."""
    client_id = (client_id or '').strip()
    client_secret = (client_secret or '').strip()
    if not client_id or not client_secret:
        raise RuntimeError("Client ID and Client Secret are both required.")
    doc = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }
    CREDENTIALS_FILE.write_text(json.dumps(doc, indent=2), encoding='utf-8')


def _clear_credentials():
    TOKEN_FILE.unlink(missing_ok=True)
    CREDENTIALS_FILE.unlink(missing_ok=True)


def _upload_photo(creds, data, filename, lat, lng, heading=None, place_id=None):
    requests, *_ = _google_libs()
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {creds.token}'})

    r = session.post(f'{BASE_URL}/photo:startUpload', json={})
    r.raise_for_status()
    upload_url = r.json()['uploadUrl']

    r = session.post(
        upload_url,
        data=data,
        headers={
            'Content-Type': 'image/jpeg',
            'X-Goog-Upload-Protocol': 'raw',
            'X-Goog-Upload-Content-Length': str(len(data)),
        },
    )
    r.raise_for_status()

    pose = {'latLngPair': {'latitude': lat, 'longitude': lng}}
    if heading is not None:
        pose['heading'] = heading
    body = {'uploadReference': {'uploadUrl': upload_url}, 'pose': pose}
    if place_id:
        body['places'] = [{'placeId': place_id}]

    r = session.post(f'{BASE_URL}/photo', json=body)
    r.raise_for_status()
    return r.json().get('photoId', {}).get('id')


def _connect_photos(creds, name_to_id, connects):
    """connects: {filename: [connected filename, ...]}"""
    _, _, _, _, build_service = _google_libs()
    sv_service = build_service('streetviewpublish', 'v1', credentials=creds)
    results = []
    for filename, targets in connects.items():
        pid = name_to_id.get(filename)
        if not pid:
            results.append({'filename': filename, 'ok': False, 'error': 'not uploaded'})
            continue
        connections = [{'target': {'id': name_to_id[t]}} for t in targets if t in name_to_id]
        if not connections:
            continue
        try:
            sv_service.photo().update(
                id=pid, updateMask='connections', body={'connections': connections}
            ).execute()
            results.append({'filename': filename, 'ok': True})
        except Exception as e:
            results.append({'filename': filename, 'ok': False, 'error': str(e)})
        time.sleep(0.3)
    return results


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(EDITOR_DIR), **kwargs)

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/'):
            super().log_message(fmt, *args)

    def _json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/api/status':
            return self._json(200, {
                'signedIn': TOKEN_FILE.exists(),
                'hasCredentials': CREDENTIALS_FILE.exists(),
            })
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        raw_body = self.rfile.read(length) if length else b''

        try:
            if parsed.path == '/api/set-credentials':
                payload = json.loads(raw_body.decode('utf-8'))
                _save_credentials(payload.get('client_id'), payload.get('client_secret'))
                return self._json(200, {'ok': True})

            if parsed.path == '/api/clear-credentials':
                _clear_credentials()
                return self._json(200, {'ok': True})

            if parsed.path == '/api/login':
                _sign_in()
                return self._json(200, {'ok': True})

            if parsed.path == '/api/upload-photo':
                qs = parse_qs(parsed.query)
                filename = qs.get('filename', [''])[0]
                lat = float(qs['lat'][0])
                lng = float(qs['lng'][0])
                heading = float(qs['heading'][0]) if qs.get('heading', [''])[0] else None
                place_id = qs.get('place_id', [''])[0] or None
                try:
                    creds = _load_credentials()
                except RuntimeError:
                    return self._json(401, {'ok': False, 'error': 'not_signed_in'})
                photo_id = _upload_photo(creds, raw_body, filename, lat, lng, heading, place_id)
                return self._json(200, {'ok': True, 'photoId': photo_id})

            if parsed.path == '/api/connect':
                payload = json.loads(raw_body.decode('utf-8'))
                try:
                    creds = _load_credentials()
                except RuntimeError:
                    return self._json(401, {'ok': False, 'error': 'not_signed_in'})
                results = _connect_photos(creds, payload.get('name_to_id', {}), payload.get('connects', {}))
                return self._json(200, {'ok': True, 'results': results})

            return self._json(404, {'ok': False, 'error': 'not_found'})

        except RuntimeError as e:
            return self._json(400, {'ok': False, 'error': str(e)})
        except Exception as e:
            return self._json(500, {'ok': False, 'error': str(e)})


def main():
    if not EDITOR_DIR.exists():
        print(f"editor/ folder not found: {EDITOR_DIR}")
        print("Rebuild with build_exe.py, or run this from the project's own folder.")
        input("Press Enter to exit...")
        sys.exit(1)

    ThreadingHTTPServer.allow_reuse_address = True
    with ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as httpd:
        url = f"http://127.0.0.1:{PORT}/editor.html"
        print(f"Serving tour editor at {url}")
        print(f"Google credentials folder: {UPLOADER_DIR}")
        if not CREDENTIALS_FILE.exists():
            print(f"  (no client_secrets.json yet - see README for the one-time Google setup)")
        print("Press Ctrl+C to stop.")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
