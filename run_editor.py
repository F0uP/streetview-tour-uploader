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

The same file is the entry point of the Windows desktop app (build_exe.py):
there it runs from a system tray icon instead of a console, opens .vrtour
files passed on the command line, and can update itself (app_update.py).

Usage:
  python run_editor.py [project.vrtour] [--tray] [--no-browser]
"""

import argparse
import json
import os
import shutil
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer, BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

import app_tray
import app_update

try:
    from _app_version import VERSION as APP_VERSION  # written by build_exe.py
except ImportError:
    APP_VERSION = "dev"

PORT = 8420
APP_NAME = "StreetviewTourEditor"

# When bundled into a .exe (PyInstaller), the editor files ship inside the
# bundle itself (build_exe.py adds them with --add-data), and credentials go
# to the per-user %APPDATA%\StreetviewTourEditor folder. That way the app can
# live in an install folder that updates replace wholesale, while each
# Windows user keeps their own sign-in across updates and reinstalls. Running
# from source (sys.executable is python.exe) keeps everything in the repo.
if getattr(sys, 'frozen', False):
    EDITOR_DIR = Path(sys._MEIPASS) / "editor"
    APP_ICON = Path(sys._MEIPASS) / "app.ico"
    UPLOADER_DIR = Path(os.environ.get('APPDATA') or Path.home()) / APP_NAME
else:
    EDITOR_DIR = Path(__file__).parent.resolve() / "editor"
    APP_ICON = Path(__file__).parent.resolve() / "installer" / "app.ico"
    UPLOADER_DIR = Path(__file__).parent.resolve() / "uploader"

UPLOADER_DIR.mkdir(parents=True, exist_ok=True)

# The windowed .exe has no console (sys.stdout is None), so keep a log file
# next to the credentials instead - handy when something goes wrong.
if sys.stdout is None or sys.stderr is None:
    _log = open(UPLOADER_DIR / "editor.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log
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


def _photo_status(creds, ids):
    """Ask Google how far each uploaded photo got: {id: {status, shareLink}}.

    status is 'published', 'processing' (accepted, not on Maps yet),
    'rejected', 'missing' (deleted on Google's side) or 'error'.
    """
    requests, *_ = _google_libs()
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {creds.token}'})
    out = {}
    for start in range(0, len(ids), 20):  # batchGet takes at most 20 IDs
        chunk = ids[start:start + 20]
        params = [('photoIds', i) for i in chunk] + [('view', 'BASIC')]
        r = session.get(f'{BASE_URL}/photos:batchGet', params=params)
        r.raise_for_status()
        for pid, res in zip(chunk, r.json().get('results', [])):
            code = (res.get('status') or {}).get('code', 0)
            photo = res.get('photo') or {}
            if code == 5:
                status = 'missing'
            elif code:
                status = 'error'
            elif photo.get('mapsPublishStatus') == 'PUBLISHED':
                status = 'published'
            elif photo.get('mapsPublishStatus') == 'REJECTED_UNKNOWN':
                status = 'rejected'
            else:
                status = 'processing'
            out[pid] = {'status': status, 'shareLink': photo.get('shareLink'),
                        'message': (res.get('status') or {}).get('message')}
    return out


def _update_photos(creds, items):
    """Update position, heading and connections of already uploaded photos.

    items: [{id, lat, lng, heading|None, connections: [photo id, ...]}]
    """
    _, _, _, _, build_service = _google_libs()
    sv_service = build_service('streetviewpublish', 'v1', credentials=creds)
    results = []
    for it in items:
        mask = ['pose.latLngPair', 'connections']
        pose = {'latLngPair': {'latitude': it['lat'], 'longitude': it['lng']}}
        if it.get('heading') is not None:
            pose['heading'] = it['heading']
            mask.append('pose.heading')
        body = {
            'photoId': {'id': it['id']},
            'pose': pose,
            'connections': [{'target': {'id': t}} for t in it.get('connections', [])],
        }
        try:
            sv_service.photo().update(id=it['id'], updateMask=','.join(mask), body=body).execute()
            results.append({'id': it['id'], 'ok': True})
        except Exception as e:
            results.append({'id': it['id'], 'ok': False, 'error': str(e)})
        time.sleep(0.3)
    return results


ALLOWED_HOSTS = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
ALLOWED_ORIGINS = {f'http://{h}' for h in ALLOWED_HOSTS}
PROJECT_SUFFIXES = ('.vrtour', '.zip', '.json')

# A .vrtour file handed to the app (double-click in Explorer, or "Open with")
# waits here until the editor tab picks it up via /api/pending-project.
PENDING = {'path': None}
# Set by main(): stops the tray icon and HTTP server so an update can replace the app.
SHUTDOWN = {'fn': lambda: None}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(EDITOR_DIR), **kwargs)

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/') and not self.path.startswith(('/api/update/status', '/api/pending-project')):
            super().log_message(fmt, *args)

    def _json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _forbidden_origin(self):
        """Only the editor itself may use this server: reject other Host names
        (DNS rebinding) and cross-site requests from other web pages."""
        if self.headers.get('Host', '') not in ALLOWED_HOSTS:
            return True
        origin = self.headers.get('Origin')
        return bool(origin) and origin not in ALLOWED_ORIGINS

    def do_GET(self):
        if self._forbidden_origin():
            return self._json(403, {'ok': False, 'error': 'forbidden'})
        parsed = urlparse(self.path)
        if parsed.path == '/api/status':
            return self._json(200, {
                'signedIn': TOKEN_FILE.exists(),
                'hasCredentials': CREDENTIALS_FILE.exists(),
            })
        if parsed.path == '/api/app-info':
            return self._json(200, {
                'version': APP_VERSION,
                'installed': app_update.is_installed(),
                'dataDir': str(UPLOADER_DIR),
            })
        if parsed.path == '/api/update/check':
            if APP_VERSION == 'dev':
                return self._json(200, {'ok': True, 'current': APP_VERSION, 'newer': False, 'dev': True})
            force = parse_qs(parsed.query).get('force', ['0'])[0] == '1'
            try:
                return self._json(200, {'ok': True, **app_update.check(APP_VERSION, force=force)})
            except Exception as e:
                return self._json(200, {'ok': False, 'error': str(e)})
        if parsed.path == '/api/update/status':
            return self._json(200, {'version': APP_VERSION, **app_update.status})
        if parsed.path == '/api/pending-project':
            path = PENDING['path']
            if not path:
                return self._json(200, {})
            return self._json(200, {'name': Path(path).name, 'size': Path(path).stat().st_size})
        if parsed.path == '/api/pending-project/data':
            return self._send_pending_project()
        return super().do_GET()

    def _send_pending_project(self):
        path = PENDING['path']
        PENDING['path'] = None
        if not path or not Path(path).is_file():
            return self._json(404, {'ok': False, 'error': 'no_pending_project'})
        size = Path(path).stat().st_size
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        with open(path, 'rb') as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def do_POST(self):
        if self._forbidden_origin():
            return self._json(403, {'ok': False, 'error': 'forbidden'})
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

            if parsed.path in ('/api/photo-status', '/api/update-photos'):
                payload = json.loads(raw_body.decode('utf-8'))
                try:
                    creds = _load_credentials()
                except RuntimeError:
                    return self._json(401, {'ok': False, 'error': 'not_signed_in'})
                if parsed.path == '/api/photo-status':
                    return self._json(200, {'ok': True, 'photos': _photo_status(creds, payload.get('ids', []))})
                return self._json(200, {'ok': True, 'results': _update_photos(creds, payload.get('items', []))})

            if parsed.path == '/api/open-file':
                payload = json.loads(raw_body.decode('utf-8'))
                path = Path(payload.get('path') or '')
                if path.suffix.lower() not in PROJECT_SUFFIXES or not path.is_file():
                    raise RuntimeError(f"Not a project file: {path}")
                PENDING['path'] = str(path.resolve())
                return self._json(200, {'ok': True})

            if parsed.path == '/api/update/install':
                if not app_update.is_installed():
                    raise RuntimeError("Only the installed app can update itself.")
                app_update.start_install(APP_VERSION, lambda: SHUTDOWN['fn']())
                return self._json(200, {'ok': True})

            return self._json(404, {'ok': False, 'error': 'not_found'})

        except RuntimeError as e:
            return self._json(400, {'ok': False, 'error': str(e)})
        except Exception as e:
            return self._json(500, {'ok': False, 'error': str(e)})


def _migrate_legacy_credentials():
    """Older .exe builds kept credentials in dist/uploader/, next to the app folder."""
    if not getattr(sys, 'frozen', False):
        return
    legacy = Path(sys.executable).parent.parent / "uploader"
    for name in ("client_secrets.json", "token.json"):
        src, dst = legacy / name, UPLOADER_DIR / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            print(f"Copied {name} to {UPLOADER_DIR}")


def _local_api(path, payload=None):
    """Call the API of an editor that is already running; None if there is none."""
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = Request(f"http://127.0.0.1:{PORT}{path}", data=data, headers={'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _fatal(msg):
    """Show an error the user can actually see - there's no console in the windowed app."""
    print(msg)
    if sys.platform == 'win32' and not (sys.stdin and sys.stdin.isatty()):
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "Streetview Tour Editor", 0x10)
    else:
        input("Press Enter to exit...")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Serve the Streetview tour editor on localhost.")
    parser.add_argument('project', nargs='?', help="a .vrtour project to open")
    parser.add_argument('--no-browser', action='store_true', help="don't open a browser tab (used after updates)")
    parser.add_argument('--console', action='store_true', help="run in the console instead of the system tray")
    parser.add_argument('--tray', action='store_true', help="run from a system tray icon (needs pystray + Pillow)")
    args = parser.parse_args()

    url = f"http://127.0.0.1:{PORT}/editor.html"
    project = str(Path(args.project).resolve()) if args.project and Path(args.project).is_file() else None

    # A second launch (shortcut, or double-clicking a .vrtour) hands over to the
    # running copy instead of failing with "address already in use".
    if _local_api('/api/status') is not None:
        if project:
            _local_api('/api/open-file', {'path': project})
        if not args.no_browser:
            webbrowser.open(url)
        return

    _migrate_legacy_credentials()

    if not EDITOR_DIR.exists():
        _fatal(f"editor/ folder not found: {EDITOR_DIR}\nRebuild with build_exe.py, or run this from the project's own folder.")

    # SO_REUSEADDR on Windows lets a second server bind the same port and
    # silently steal requests, so only enable it elsewhere.
    ThreadingHTTPServer.allow_reuse_address = os.name != 'nt'
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        _fatal(f"Could not start on port {PORT}: {e}\nAnother program is using this port. Close it and try again.")

    PENDING['path'] = project
    print(f"Streetview Tour Editor {APP_VERSION}")
    print(f"Serving tour editor at {url}")
    print(f"Google credentials folder: {UPLOADER_DIR}")
    if not CREDENTIALS_FILE.exists():
        print("  (no client_secrets.json yet - see README for the one-time Google setup)")
    if not args.no_browser:
        webbrowser.open(url)

    # The desktop app lives in the tray; from source, stay in the console (Ctrl+C
    # to stop) unless pystray is installed and --tray is asked for.
    frozen = getattr(sys, 'frozen', False)
    use_tray = (frozen or args.tray) and not args.console and app_tray.available() and APP_ICON.exists()
    if not use_tray:
        SHUTDOWN['fn'] = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
        print("Press Ctrl+C to stop.")
        with httpd:
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nStopped.")
        return

    server = threading.Thread(target=httpd.serve_forever, daemon=True)
    server.start()

    def shutdown():
        app_tray.stop()
        httpd.shutdown()

    SHUTDOWN['fn'] = shutdown
    app_tray.run(
        APP_ICON, APP_VERSION,
        open_editor=lambda: webbrowser.open(url),
        data_dir=UPLOADER_DIR,
        on_quit=httpd.shutdown,
        message=(app_tray.tr("Updated to version {v}.", v=APP_VERSION) if args.no_browser
                 else app_tray.tr("Running in the notification area - right-click the icon to quit.")),
    )
    httpd.server_close()


if __name__ == "__main__":
    main()
