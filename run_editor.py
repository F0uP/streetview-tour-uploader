"""
Serve the tour editor over http://127.0.0.1 and show it in its own window.

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

The same file is the entry point of the desktop app on Windows, macOS and
Linux (build_exe.py, packaging/linux/build_deb.py). The editor opens in its
own window (app_window.py: WebView2 / WKWebView / WebKitGTK); closing it quits
the app. Without a usable web engine - or with --browser - it opens in the
default browser instead, and the app quits once the last editor tab is
closed. The app also opens .vrtour files passed on the command line and, as
installed Windows app, can update itself (app_update.py).

Usage:
  python run_editor.py [project.vrtour] [--browser [--tray] [--no-browser]]
"""

import argparse
import base64
import ctypes
import hmac
import io
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer, BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

FROZEN = getattr(sys, 'frozen', False)
SOURCE_DIR = Path(sys._MEIPASS) if FROZEN else Path(__file__).parent.resolve()
# The Linux package ships the Python libraries it doesn't take from the system here.
if (SOURCE_DIR / "vendor").is_dir():
    sys.path.insert(0, str(SOURCE_DIR / "vendor"))

import app_tray  # noqa: E402
import app_update  # noqa: E402
import app_window  # noqa: E402

try:
    import _app_version  # written by build_exe.py / packaging/linux/build_deb.py
    APP_VERSION = _app_version.VERSION
except ImportError:
    _app_version = None
    APP_VERSION = "dev"

PORT = 8420
APP_NAME = "StreetviewTourEditor"
APP_MUTEX = "StreetviewTourEditor.Running"  # also named in installer/StreetviewTourEditor.iss

# Installed (the .exe / .app, or the Linux package in /usr/lib): the program
# folder is replaced wholesale by updates or isn't writable at all, so each
# user's sign-in, settings and autosave live in their own profile instead.
# Running from source keeps everything in the repo.
PACKAGED = FROZEN or getattr(_app_version, 'PACKAGED', False)
EDITOR_DIR = SOURCE_DIR / "editor"
ICONS_DIR = SOURCE_DIR if FROZEN else SOURCE_DIR / "packaging" / "icons"
APP_ICON = ICONS_DIR / "app.ico"  # tray icon (Windows)


def _data_dirs():
    """(settings and sign-in, website data of the app window) for this platform."""
    if not PACKAGED:
        return SOURCE_DIR / "uploader", SOURCE_DIR / "uploader" / "webview"
    home = Path.home()
    if sys.platform == 'win32':
        roaming = Path(os.environ.get('APPDATA') or home) / APP_NAME
        # The autosaved session with its photos can get big: not into the roaming profile.
        return roaming, Path(os.environ.get('LOCALAPPDATA') or roaming) / APP_NAME / "WebView2"
    if sys.platform == 'darwin':
        support = home / "Library" / "Application Support" / APP_NAME
        return support, support / "WebView"
    config = Path(os.environ.get('XDG_CONFIG_HOME') or home / ".config") / "streetview-tour-editor"
    data = Path(os.environ.get('XDG_DATA_HOME') or home / ".local" / "share") / "streetview-tour-editor"
    return config, data / "webview"


UPLOADER_DIR, WEBVIEW_DIR = _data_dirs()
UPLOADER_DIR.mkdir(parents=True, exist_ok=True)

# An installed app has no console to read (on Windows sys.stdout is even None),
# so keep a log file next to the credentials instead - handy when something
# goes wrong. Started from a terminal, it keeps printing there.
if sys.stdout is None or sys.stderr is None or (PACKAGED and not sys.stdout.isatty()):
    _log = open(UPLOADER_DIR / "editor.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = _log
    sys.stderr = _log
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
            try:
                creds.refresh(GoogleRequest())
            except Exception as e:
                # The sign-in was revoked or ran out. For Cloud projects in "Testing"
                # mode Google lets refresh tokens expire after 7 days. Forget it so the
                # editor shows "not signed in" instead of a cryptic invalid_grant error.
                print(f"Google sign-in expired: {e}")
                TOKEN_FILE.unlink(missing_ok=True)
                raise RuntimeError("not_signed_in") from e
            TOKEN_FILE.write_text(creds.to_json())
        else:
            raise RuntimeError("not_signed_in")
    return creds


def _signed_in():
    """True if there is a token that still works (refreshing it if needed)."""
    if not TOKEN_FILE.exists():
        return False
    try:
        _load_credentials()
        return True
    except RuntimeError:
        return False
    except Exception:
        return True  # offline etc. - don't claim the sign-in is gone


def _sign_in():
    """Run the interactive OAuth consent flow (opens the browser) and cache the resulting token."""
    _, _, _, InstalledAppFlow, _ = _google_libs()

    if not CREDENTIALS_FILE.exists():
        raise RuntimeError(
            f"client_secrets.json not found. Save your OAuth desktop client credentials to {CREDENTIALS_FILE}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    received = {}
    state = None

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            if not params.get('code') and not params.get('error'):
                # e.g. the browser asking for /favicon.ico - not Google's redirect
                self.send_response(404)
                self.end_headers()
                return
            if params.get('state', [None])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'Sign-in request did not match. Please try again from the editor.')
                return
            received['code'] = params.get('code', [None])[0]
            received['done'] = True
            german = (self.headers.get('Accept-Language') or '').lower().startswith('de')
            ok = bool(received['code'])
            text = {
                (True, True): ('Anmeldung abgeschlossen', 'Du kannst diesen Tab schließen und im Streetview Tour Editor weitermachen.'),
                (True, False): ('Anmeldung abgebrochen', 'Du kannst diesen Tab schließen und es im Editor noch einmal versuchen.'),
                (False, True): ('Signed in', 'You can close this tab and continue in the Streetview Tour Editor.'),
                (False, False): ('Sign-in cancelled', 'You can close this tab and try again from the editor.'),
            }[(german, ok)]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(SIGN_IN_PAGE.format(title=text[0], text=text[1], lang='de' if german else 'en').encode('utf-8'))

        def log_message(self, *args):
            pass

    # Only reachable from this computer (the redirect goes to 127.0.0.1), and
    # the state value proves the answer belongs to this sign-in. Desktop OAuth
    # clients accept any loopback port, so fall back to a free one if 8085 is taken.
    try:
        server = HTTPServer(('127.0.0.1', 8085), _Handler)
    except OSError:
        server = HTTPServer(('127.0.0.1', 0), _Handler)
    server.timeout = 1
    flow.redirect_uri = f'http://127.0.0.1:{server.server_address[1]}/'
    auth_url, state = flow.authorization_url(prompt='consent', access_type='offline')

    def serve():
        deadline = time.time() + 180
        while not received.get('done') and time.time() < deadline:
            server.handle_request()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    webbrowser.open(auth_url)
    t.join(timeout=181)
    server.server_close()

    if not received.get('code'):
        raise RuntimeError("Sign-in timed out or was cancelled. Please try again.")

    flow.fetch_token(code=received['code'])
    creds = flow.credentials
    TOKEN_FILE.write_text(creds.to_json())
    if APP['window']:
        APP['window'].focus()  # back from the browser to the editor
    return creds


SIGN_IN_PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#060608; color:#f3f1f7;
  font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ padding:32px 36px; max-width:420px; border-radius:16px; background:#100e16; border:1px solid rgba(255,255,255,.08); text-align:center; }}
h1 {{ margin:0 0 8px; font-size:20px; }} p {{ margin:0; color:#9793a6; }}
</style></head><body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""


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


class GoogleError(RuntimeError):
    """An error answer from Google, with its readable message and a hint for the user."""

    def __init__(self, step, status, message, kind='other'):
        super().__init__(f'{step}: {message}')
        self.step, self.status, self.message, self.kind = step, status, message, kind


def _google_message(r):
    try:
        err = r.json().get('error', {})
        return err.get('message') or r.text[:300]
    except ValueError:
        return r.text[:300] or r.reason


# Upload progress per job id, polled by the editor (/api/upload-progress) while a
# photo goes from this app to Google, which is the slow part of an upload.
UPLOAD_PROGRESS = {}


class _ProgressReader(io.BytesIO):
    def __init__(self, data, job):
        super().__init__(data)
        self.total = len(data)
        self.job = job

    def __len__(self):
        return self.total

    def read(self, size=-1):
        chunk = super().read(64 * 1024 if size is None or size < 0 else min(size, 64 * 1024))
        if self.job:
            UPLOAD_PROGRESS[self.job] = {'phase': 'uploading', 'sent': self.tell(), 'total': self.total}
        return chunk


def _upload_photo(creds, data, filename, lat, lng, heading=None, place_id=None, job=None):
    requests, *_ = _google_libs()
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {creds.token}'})
    if job:
        UPLOAD_PROGRESS[job] = {'phase': 'starting', 'sent': 0, 'total': len(data)}

    r = session.post(f'{BASE_URL}/photo:startUpload', json={})
    if not r.ok:
        raise GoogleError('start', r.status_code, _google_message(r))
    upload_url = r.json()['uploadUrl']

    r = session.post(
        upload_url,
        data=_ProgressReader(data, job),
        headers={
            'Content-Type': 'image/jpeg',
            'X-Goog-Upload-Protocol': 'raw',
            'X-Goog-Upload-Content-Length': str(len(data)),
        },
    )
    if not r.ok:
        raise GoogleError('upload', r.status_code, _google_message(r))

    if job:
        UPLOAD_PROGRESS[job] = {'phase': 'creating', 'sent': len(data), 'total': len(data)}
    pose = {'latLngPair': {'latitude': lat, 'longitude': lng}}
    if heading is not None:
        pose['heading'] = heading
    body = {'uploadReference': {'uploadUrl': upload_url}, 'pose': pose}
    if place_id:
        body['places'] = [{'placeId': place_id}]

    r = session.post(f'{BASE_URL}/photo', json=body)
    if not r.ok:
        message = _google_message(r)
        # A wrong Place ID is by far the most common reason Google refuses an
        # otherwise fine photo with "400 Bad Request".
        kind = 'place' if place_id and r.status_code == 400 else 'other'
        raise GoogleError('create', r.status_code, message, kind)
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


def _id_candidates(pid):
    """Forms a photo ID can be asked for.

    photo.create answers with an ID like 'CAoSHENJQUJJaEMx...Wko.': web-safe base64
    (with '.' as padding) around the photo's real ID ('CIABIhC1...'). Depending on the
    endpoint Google only knows one of these forms, so try all of them.
    """
    out = [pid]
    stripped = pid.rstrip('.=')
    if stripped != pid:
        out.append(stripped)
    try:
        raw = base64.urlsafe_b64decode(stripped + '=' * (-len(stripped) % 4))
        i = 0
        while i < len(raw):
            tag = raw[i]; i += 1
            field, wire = tag >> 3, tag & 7
            if wire == 0:
                while raw[i] & 0x80:
                    i += 1
                i += 1
            elif wire == 2:
                length = raw[i]; i += 1
                value = raw[i:i + length]; i += length
                if field == 2 and value.isascii() and value.decode().isprintable():
                    out.append(value.decode())
            else:
                break
    except Exception:
        pass
    return list(dict.fromkeys(out))


def _photo_status(creds, ids):
    """Ask Google how far each uploaded photo got.

    Returns {id: {status, shareLink, resolvedId}} where status is 'published',
    'processing' (known to Google, not on Maps yet), 'rejected', or 'notfound'
    (not visible yet - normal for a while right after uploading - or deleted).
    resolvedId is the form of the ID Google actually knows.
    """
    requests, *_ = _google_libs()
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {creds.token}'})
    cands = {pid: _id_candidates(pid) for pid in ids}
    flat = list(dict.fromkeys(c for cs in cands.values() for c in cs))
    found = {}
    for start in range(0, len(flat), 20):  # batchGet takes at most 20 IDs
        chunk = flat[start:start + 20]
        params = [('photoIds', i) for i in chunk] + [('view', 'BASIC')]
        r = session.get(f'{BASE_URL}/photos:batchGet', params=params)
        if not r.ok:
            raise GoogleError('status', r.status_code, _google_message(r))
        for cid, res in zip(chunk, r.json().get('results', [])):
            if (res.get('status') or {}).get('code', 0) == 0 and res.get('photo'):
                found[cid] = res['photo']
    out = {}
    for pid, cs in cands.items():
        hit = next((c for c in cs if c in found), None)
        if not hit:
            out[pid] = {'status': 'notfound', 'shareLink': None, 'resolvedId': None}
            continue
        photo = found[hit]
        publish = photo.get('mapsPublishStatus')
        status = 'published' if publish == 'PUBLISHED' else 'rejected' if publish == 'REJECTED_UNKNOWN' else 'processing'
        real_id = (photo.get('photoId') or {}).get('id') or hit
        out[pid] = {'status': status, 'shareLink': photo.get('shareLink'), 'resolvedId': real_id, **_photo_facts(photo)}
    return out


def _photo_facts(photo):
    """What Google has stored for a photo: position, heading and its links."""
    pose = photo.get('pose') or {}
    ll = pose.get('latLngPair') or {}
    return {
        'lat': ll.get('latitude'),
        'lng': ll.get('longitude'),
        'heading': pose.get('heading'),
        'connections': [((c.get('target') or {}).get('id')) for c in photo.get('connections') or [] if (c.get('target') or {}).get('id')],
    }


def _list_photos(creds):
    """All of the signed-in user's Street View photos (ID, position, status)."""
    requests, *_ = _google_libs()
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {creds.token}'})
    photos, token = [], None
    for _ in range(50):  # 50 pages x 100 photos is plenty
        params = {'view': 'BASIC', 'pageSize': 100}
        if token:
            params['pageToken'] = token
        r = session.get(f'{BASE_URL}/photos', params=params)
        if not r.ok:
            raise GoogleError('list', r.status_code, _google_message(r))
        data = r.json()
        for p in data.get('photos', []):
            publish = p.get('mapsPublishStatus')
            photos.append({
                'id': (p.get('photoId') or {}).get('id'),
                'status': 'published' if publish == 'PUBLISHED' else 'rejected' if publish == 'REJECTED_UNKNOWN' else 'processing',
                'shareLink': p.get('shareLink'),
                'uploadTime': p.get('uploadTime'),
                **_photo_facts(p),
            })
        token = data.get('nextPageToken')
        if not token:
            break
    return photos


def _delete_photos(creds, ids):
    """Delete photos from Google (old versions of photos that were replaced)."""
    requests, *_ = _google_libs()
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {creds.token}'})
    results = []
    for pid in ids:
        result = {'id': pid, 'ok': True, 'error': None}  # every form 404 = already gone, fine
        for cid in _id_candidates(pid):
            r = session.delete(f'{BASE_URL}/photo/{cid}')
            if r.ok:
                result = {'id': pid, 'ok': True, 'error': None}
                break
            if r.status_code in (400, 404):
                # Wrong form of the ID (or already deleted) - try the next form.
                if r.status_code == 400:
                    result = {'id': pid, 'ok': False, 'error': _google_message(r)}
                continue
            result = {'id': pid, 'ok': False, 'error': _google_message(r)}
            break
        results.append(result)
    return results


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
            # googleapiclient's HttpError carries Google's own message in .reason
            status = getattr(getattr(e, 'resp', None), 'status', None)
            results.append({'id': it['id'], 'ok': False, 'status': status,
                            'error': getattr(e, 'reason', None) or str(e)})
        time.sleep(0.3)
    return results


# A secret only this Windows user can read (it lives in their profile). The editor
# page gets it in the URL the app opens and sends it with every API call, so other
# programs or other users on this computer can't use the API - and with it the
# Google sign-in - even though the server listens on 127.0.0.1.
KEY_FILE = UPLOADER_DIR / ".editor_key"


def _load_editor_key():
    try:
        key = KEY_FILE.read_text(encoding='utf-8').strip()
        if len(key) >= 32:
            return key, False
    except OSError:
        pass
    key = secrets.token_urlsafe(32)
    KEY_FILE.write_text(key, encoding='utf-8')
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return key, True


def _new_editor_key():
    """A fresh key for this run: browser tabs from earlier runs lose access."""
    global EDITOR_KEY
    EDITOR_KEY = secrets.token_urlsafe(32)
    KEY_FILE.write_text(EDITOR_KEY, encoding='utf-8')  # a second launch hands over with it


EDITOR_KEY, EDITOR_KEY_IS_NEW = _load_editor_key()
# Readable without the key: the update progress page survives the app restart.
KEYLESS_API = {'/api/update/status'}

ALLOWED_HOSTS = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
ALLOWED_ORIGINS = {f'http://{h}' for h in ALLOWED_HOSTS}
PROJECT_SUFFIXES = ('.vrtour', '.zip', '.json')

# A .vrtour file handed to the app (double-click in Explorer, or "Open with")
# waits here until the editor tab picks it up via /api/pending-project.
PENDING = {'path': None}
# Set by main(): stops the tray icon and HTTP server so an update can replace the app.
SHUTDOWN = {'fn': lambda: None}
# The app window (app_window.AppWindow) when the editor runs in one, else None.
APP = {'window': None}

# Files picked in the app window. WebView2's own file pickers are titled for
# websites ("Choose a file this site can read"), so the app window shows
# Windows' regular dialogs instead and reads and writes the files here. The page
# only gets a token for what the user picked - it can't name any other path.
# Folders chosen as export target are remembered under a grant id, so they
# keep working after a restart.
FILES = {}  # token -> {'path': Path, 'kind': 'file' | 'folder'}
FOLDER_GRANTS_FILE = UPLOADER_DIR / "folders.json"
BAD_NAME_CHARS = set('<>:"/\\|?*')


def _file_token(path, kind):
    token = secrets.token_urlsafe(18)
    FILES[token] = {'path': Path(path), 'kind': kind}
    return token


def _folder_grants():
    try:
        grants = json.loads(FOLDER_GRANTS_FILE.read_text(encoding='utf-8'))
        return grants if isinstance(grants, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_folder(path):
    grants = _folder_grants()
    grant = next((g for g, p in grants.items() if p == str(path)), None) or secrets.token_urlsafe(12)
    grants.pop(grant, None)
    grants[grant] = str(path)
    grants = dict(list(grants.items())[-20:])
    FOLDER_GRANTS_FILE.write_text(json.dumps(grants, indent=1), encoding='utf-8')
    return grant


def _file_filters(types):
    """The page's picker types ({description, extensions}) as dialog filters."""
    filters = []
    for t in types or []:
        exts = [e for e in (t.get('extensions') or []) if isinstance(e, str) and re.fullmatch(r'\.[A-Za-z0-9]{1,10}', e)]
        if exts:
            desc = re.sub(r'[|()]', ' ', str(t.get('description') or '')).strip()[:80] or 'Files'
            filters.append((desc, ['*' + e for e in dict.fromkeys(exts)]))
    return filters


class PageTracker:
    """Quit the app once the last editor tab is closed.

    Each open editor page says hello every 30 s and sends a goodbye when it is
    closed (also on reload, which says hello again right away). A goodbye with
    no other page alive starts a short grace period; if no page comes back the
    app shuts down. A goodbye can get lost (browser crash, killed process), so
    the app also quits once no page has been heard from for QUIET_FOR seconds.
    Hidden tabs keep saying hello at least once a minute; time spent asleep
    (laptop lid closed) doesn't count.
    """

    ALIVE_FOR = 150   # a page counts as open if heard from within this many seconds
    GRACE = 12        # seconds to wait for a reload before quitting
    QUIET_FOR = 300   # quit when no page has been heard from for this long

    def __init__(self):
        self.pages = {}
        self.quit_at = None
        self.last_seen = time.time()
        self.lock = threading.Lock()

    def hello(self, page):
        with self.lock:
            self.pages[page] = self.last_seen = time.time()
            self.quit_at = None

    def touch(self):
        """Any request from an editor page (e.g. a long upload) counts as a sign of life."""
        with self.lock:
            self.last_seen = time.time()

    def bye(self, page):
        with self.lock:
            self.pages.pop(page, None)
            now = time.time()
            if not any(now - seen < self.ALIVE_FOR for seen in self.pages.values()):
                self.quit_at = now + self.GRACE

    def watch(self, on_quit):
        def loop():
            last_tick = time.time()
            while True:
                time.sleep(2)
                now = time.time()
                with self.lock:
                    if now - last_tick > 30:
                        # The computer slept: give the pages time to say hello again.
                        self.last_seen = now
                        self.pages = {page: now for page in self.pages}
                        if self.quit_at is not None:
                            self.quit_at = now + self.GRACE
                    last_tick = now
                    closed = self.quit_at is not None and now >= self.quit_at
                    quiet = now - self.last_seen > self.QUIET_FOR
                if closed or quiet:
                    print("Last editor tab closed - quitting." if closed
                          else "No editor tab open for a while - quitting.")
                    on_quit()
                    return

        threading.Thread(target=loop, daemon=True).start()


PAGES = PageTracker()


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

    def _key_ok(self, key=None):
        key = key if key is not None else self.headers.get('X-Editor-Key', '')
        ok = hmac.compare_digest(key.encode('utf-8'), EDITOR_KEY.encode('utf-8'))
        if ok:
            PAGES.touch()
        return ok

    def _bad_key(self):
        body = json.dumps({'ok': False, 'error': 'bad_key', 'window': bool(APP['window'])}).encode('utf-8')
        self.send_response(401)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Editor-Auth', 'bad-key')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self._forbidden_origin():
            return self._json(403, {'ok': False, 'error': 'forbidden'})
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/') and parsed.path not in KEYLESS_API and not self._key_ok():
            return self._bad_key()
        if parsed.path in ('/', '/index.html'):
            self.send_response(302)
            self.send_header('Location', '/editor.html')
            self.end_headers()
            return
        if parsed.path == '/api/status':
            had_token = TOKEN_FILE.exists()
            signed_in = _signed_in()
            return self._json(200, {
                'signedIn': signed_in,
                'expired': had_token and not signed_in,
                'hasCredentials': CREDENTIALS_FILE.exists(),
            })
        if parsed.path == '/api/app-info':
            return self._json(200, {
                'version': APP_VERSION,
                'installed': app_update.is_installed(),
                'dataDir': str(UPLOADER_DIR),
                'window': bool(APP['window']),
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
        if parsed.path == '/api/upload-progress':
            job = parse_qs(parsed.query).get('job', [''])[0]
            return self._json(200, UPLOAD_PROGRESS.get(job, {}))
        if parsed.path == '/api/pending-project':
            path = PENDING['path']
            if not path or not Path(path).is_file():
                return self._json(200, {})
            return self._json(200, {
                'name': Path(path).name, 'size': Path(path).stat().st_size,
                'token': _file_token(path, 'file') if APP['window'] else None,
            })
        if parsed.path == '/api/pending-project/data':
            return self._send_pending_project()
        if parsed.path == '/api/files/read':
            entry = FILES.get((parse_qs(parsed.query).get('token') or [''])[0])
            if not APP['window'] or not entry or entry['kind'] != 'file' or not entry['path'].is_file():
                return self._json(404, {'ok': False, 'error': 'file_not_found'})
            return self._send_file(entry['path'])
        return super().do_GET()

    def _send_pending_project(self):
        path = PENDING['path']
        PENDING['path'] = None
        if not path or not Path(path).is_file():
            return self._json(404, {'ok': False, 'error': 'no_pending_project'})
        return self._send_file(path)

    def _send_file(self, path):
        size = Path(path).stat().st_size
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        with open(path, 'rb') as f:
            shutil.copyfileobj(f, self.wfile, 1024 * 1024)

    def _write_picked_file(self, parsed):
        """Stream the request body into a file the user picked: into a temporary file
        next to it first, then swapped in, so a failed save never leaves half a file."""
        self.close_connection = True  # an unread body must not leak into a next request
        query = parse_qs(parsed.query)
        entry = FILES.get((query.get('token') or [''])[0])
        if not APP['window'] or not entry:
            return self._json(404, {'ok': False, 'error': 'unknown_file'})
        target = entry['path']
        if entry['kind'] == 'folder':
            name = (query.get('name') or [''])[0]
            if not name or name in ('.', '..') or name != Path(name).name or BAD_NAME_CHARS & set(name):
                return self._json(400, {'ok': False, 'error': f'Not a valid file name: {name}'})
            target = target / name
        length = int(self.headers.get('Content-Length') or 0)
        tmp = target.with_name(f'.{target.name}.{secrets.token_hex(4)}.part')
        try:
            with open(tmp, 'wb') as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ConnectionError("The data stopped arriving - nothing was saved.")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, target)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return self._json(500, {'ok': False, 'error': str(e)})
        return self._json(200, {'ok': True, 'size': length})

    def do_POST(self):
        if self._forbidden_origin():
            return self._json(403, {'ok': False, 'error': 'forbidden'})
        parsed = urlparse(self.path)
        if parsed.path == '/api/files/write':  # can be hundreds of MB: streamed, not read into memory
            if not self._key_ok():
                return self._bad_key()
            return self._write_picked_file(parsed)
        length = int(self.headers.get('Content-Length') or 0)
        raw_body = self.rfile.read(length) if length else b''

        if parsed.path == '/api/bye':
            # Sent with navigator.sendBeacon, which can't set headers: key in the body.
            try:
                payload = json.loads(raw_body.decode('utf-8') or '{}')
            except ValueError:
                payload = {}
            if not self._key_ok(str(payload.get('key', ''))):
                return self._bad_key()
            PAGES.bye(str(payload.get('page', '')))
            return self._json(200, {'ok': True})
        if not self._key_ok():
            return self._bad_key()

        try:
            if parsed.path == '/api/hello':
                payload = json.loads(raw_body.decode('utf-8') or '{}')
                PAGES.hello(str(payload.get('page', '')))
                return self._json(200, {'ok': True})

            if parsed.path == '/api/busy':
                # While e.g. an upload runs, closing the window asks first (the page sends the question).
                payload = json.loads(raw_body.decode('utf-8') or '{}')
                if APP['window']:
                    APP['window'].set_busy(str(payload.get('question') or '')[:500])
                return self._json(200, {'ok': True})

            if parsed.path == '/api/files/pick':
                if not APP['window']:
                    return self._json(404, {'ok': False, 'error': 'not_found'})
                payload = json.loads(raw_body.decode('utf-8') or '{}')
                kind = payload.get('kind')
                if kind not in ('open', 'save', 'folder'):
                    raise RuntimeError(f"Unknown dialog: {kind}")
                path = APP['window'].pick(
                    kind, suggested_name=Path(str(payload.get('suggestedName') or '')).name,
                    filters=_file_filters(payload.get('types')),
                    all_files_label='Alle Dateien' if app_tray.GERMAN else 'All files')
                if not path:
                    return self._json(200, {'ok': True, 'cancelled': True})
                path = Path(path)
                if kind == 'folder':
                    return self._json(200, {'ok': True, 'token': _file_token(path, 'folder'),
                                            'name': path.name or str(path), 'grant': _remember_folder(path)})
                return self._json(200, {'ok': True, 'token': _file_token(path, 'file'), 'name': path.name})

            if parsed.path == '/api/files/folder':
                # The export folder picked in an earlier run (the page keeps only the grant id).
                payload = json.loads(raw_body.decode('utf-8') or '{}')
                path = _folder_grants().get(str(payload.get('grant') or ''))
                if not APP['window'] or not path or not Path(path).is_dir():
                    return self._json(200, {'ok': False, 'error': 'folder_gone'})
                return self._json(200, {'ok': True, 'token': _file_token(path, 'folder'), 'name': Path(path).name or path})

            if parsed.path == '/api/window-title':
                payload = json.loads(raw_body.decode('utf-8') or '{}')
                if APP['window']:
                    APP['window'].set_title(str(payload.get('title') or '')[:200])
                return self._json(200, {'ok': True})

            if parsed.path == '/api/focus':
                # A second launch of the app: show this window, and open a project it brought along.
                if APP['window']:
                    APP['window'].focus()
                    if PENDING['path']:
                        APP['window'].run_js('window.__svtOpenPending && window.__svtOpenPending()')
                return self._json(200, {'ok': True, 'window': bool(APP['window'])})

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
                job = qs.get('job', [''])[0] or None
                try:
                    creds = _load_credentials()
                except RuntimeError:
                    return self._json(401, {'ok': False, 'error': 'not_signed_in'})
                try:
                    photo_id = _upload_photo(creds, raw_body, filename, lat, lng, heading, place_id, job)
                except GoogleError as e:
                    return self._json(200, {'ok': False, 'error': e.message, 'status': e.status,
                                            'step': e.step, 'kind': e.kind})
                finally:
                    UPLOAD_PROGRESS.pop(job, None)
                return self._json(200, {'ok': True, 'photoId': photo_id})

            if parsed.path == '/api/connect':
                payload = json.loads(raw_body.decode('utf-8'))
                try:
                    creds = _load_credentials()
                except RuntimeError:
                    return self._json(401, {'ok': False, 'error': 'not_signed_in'})
                results = _connect_photos(creds, payload.get('name_to_id', {}), payload.get('connects', {}))
                return self._json(200, {'ok': True, 'results': results})

            if parsed.path in ('/api/photo-status', '/api/update-photos', '/api/list-photos', '/api/delete-photos'):
                payload = json.loads(raw_body.decode('utf-8') or '{}')
                try:
                    creds = _load_credentials()
                except RuntimeError:
                    return self._json(401, {'ok': False, 'error': 'not_signed_in'})
                if parsed.path == '/api/list-photos':
                    return self._json(200, {'ok': True, 'photos': _list_photos(creds)})
                if parsed.path == '/api/delete-photos':
                    return self._json(200, {'ok': True, 'results': _delete_photos(creds, payload.get('ids', []))})
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
    if not FROZEN or sys.platform != 'win32':
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
    req = Request(f"http://127.0.0.1:{PORT}{path}", data=data,
                  headers={'Content-Type': 'application/json', 'X-Editor-Key': EDITOR_KEY})
    try:
        with urlopen(req, timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _fatal(msg):
    """Show an error the user can actually see - there's no console in the installed app."""
    print(msg)
    if sys.stdin and sys.stdin.isatty():
        input("Press Enter to exit...")
    elif sys.platform == 'win32':
        ctypes.windll.user32.MessageBoxW(None, msg, "Streetview Tour Editor", 0x10)
    elif sys.platform == 'darwin':
        import subprocess
        subprocess.run(['osascript', '-e', 'on run argv', '-e',
                        'display alert "Streetview Tour Editor" message (item 1 of argv) as critical', '-e', 'end run', msg])
    elif shutil.which('zenity'):
        import subprocess
        subprocess.run(['zenity', '--error', '--title=Streetview Tour Editor', '--no-markup', f'--text={msg}'])
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Serve the Streetview tour editor on localhost.")
    parser.add_argument('project', nargs='?', help="a .vrtour project to open")
    parser.add_argument('--browser', action='store_true', help="open the editor in the browser instead of its own window")
    parser.add_argument('--no-browser', action='store_true', help="browser mode: don't open a tab (used after updates)")
    parser.add_argument('--console', action='store_true', help="browser mode: run in the console instead of the system tray")
    parser.add_argument('--tray', action='store_true', help="browser mode: run from a system tray icon (needs pystray + Pillow)")
    parser.add_argument('--keep-running', action='store_true', help="browser mode: don't quit when the last editor tab is closed")
    parser.add_argument('--debug', action='store_true', help="window mode: allow the developer tools (F12)")
    parser.add_argument('--smoke-test', action='store_true',
                        help="open the editor window, check that it works, quit (exit code 0 = fine; used by CI)")
    parser.add_argument('--cdp-port', type=int, help=argparse.SUPPRESS)  # automated tests drive the window over this
    args = parser.parse_args()

    project = str(Path(args.project).resolve()) if args.project and Path(args.project).is_file() else None

    # A second launch (shortcut, or double-clicking a .vrtour) hands over to the
    # running copy instead of failing with "address already in use".
    running = _local_api('/api/app-info')
    if running is not None:
        if project:
            _local_api('/api/open-file', {'path': project})
        if running.get('window'):
            if sys.platform == 'win32':
                ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: let it come to the front
            _local_api('/api/focus', {})
        elif not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{PORT}/editor.html#k={EDITOR_KEY}")
        return

    _migrate_legacy_credentials()

    if not EDITOR_DIR.exists():
        _fatal(f"editor/ folder not found: {EDITOR_DIR}\nRebuild with build_exe.py, or run this from the project's own folder.")

    window_class = None if args.browser else app_window.window_class()
    use_window = window_class is not None
    if args.smoke_test:
        if not use_window:
            print("Smoke test: no app window available on this computer")
            sys.exit(2)
        _start_smoke_watchdog()

    # SO_REUSEADDR on Windows lets a second server bind the same port and
    # silently steal requests, so only enable it elsewhere.
    ThreadingHTTPServer.allow_reuse_address = os.name != 'nt'
    # Don't wait for open requests (e.g. a pending Google sign-in) when quitting.
    ThreadingHTTPServer.daemon_threads = True
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        _fatal(f"Could not start on port {PORT}: {e}\nAnother program is using this port. Close it and try again.")
    if sys.platform == 'win32':
        # Held until the app exits: the installer waits on it before replacing files.
        APP['mutex'] = ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX)
    if use_window:
        _new_editor_key()  # only now: a launch that can't start must not lock out the running copy

    PENDING['path'] = project
    print(f"Streetview Tour Editor {APP_VERSION}")
    print(f"Serving tour editor at http://127.0.0.1:{PORT}/editor.html")
    print(f"Google credentials folder: {UPLOADER_DIR}")
    if not CREDENTIALS_FILE.exists():
        print("  (no client_secrets.json yet - see README for the one-time Google setup)")
    # The key travels in the URL fragment: it never reaches the server log or
    # any other site, and the page moves it into its own storage right away.
    url = f"http://127.0.0.1:{PORT}/editor.html#k={EDITOR_KEY}"

    if use_window:
        server = threading.Thread(target=httpd.serve_forever, daemon=True)
        server.start()
        window = window_class(
            url, "Streetview Tour Editor",
            state_file=UPLOADER_DIR / "window.json", storage_dir=WEBVIEW_DIR,
            icon=_window_icon(), debug=args.debug, cdp_port=args.cdp_port, smoke_test=args.smoke_test,
        )
        APP['window'] = window
        SHUTDOWN['fn'] = window.quit
        print("Running in its own window - closing it stops the editor.")
        try:
            window.run()
        except Exception as e:  # e.g. the WebView2 runtime is broken
            print(f"The app window failed to start ({e}) - opening the browser instead.")
            APP['window'] = None
            SHUTDOWN['fn'] = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
            webbrowser.open(url)
            PAGES.watch(SHUTDOWN['fn'])
            server.join()
        finally:
            httpd.shutdown()
            httpd.server_close()
        # Everything is saved and the server is down: skip Python's slow and
        # sometimes hanging shutdown of the .NET runtime. The web engine's own
        # process finishes writing the page's data independently.
        code = 0
        if args.smoke_test:
            code = 0 if (window.smoke_result or '').startswith('ok') else 1
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)

    if not args.browser:
        print("No app window available (needs pywebview and the system's web engine: WebView2 on Windows, "
              "WebKitGTK on Linux) - opening the browser instead.")
    _run_in_browser(args, httpd, url)


def _window_icon():
    """The window / dock icon; the installed .exe and .app bring their own."""
    if sys.platform == 'win32':
        icon = None if FROZEN else ICONS_DIR / "app.ico"
    elif sys.platform == 'darwin':
        icon = None if FROZEN else ICONS_DIR / "png" / "512.png"
    else:
        icon = ICONS_DIR / "png" / "256.png"
    return icon if icon and icon.exists() else None


def _start_smoke_watchdog(seconds=150):
    def watchdog():
        time.sleep(seconds)
        print(f"Smoke test: the editor didn't come up within {seconds} s")
        sys.stdout.flush()
        os._exit(3)
    threading.Thread(target=watchdog, daemon=True).start()


def _run_in_browser(args, httpd, url):
    """The editor in a browser tab; the app quits once the last editor tab is closed."""
    # After an update the old tab reconnects by itself - unless this version is
    # the first with an editor key, which the old tab doesn't have yet.
    if not args.no_browser or EDITOR_KEY_IS_NEW:
        webbrowser.open(url)

    # The desktop app lives in the tray; from source, stay in the console (Ctrl+C
    # to stop) unless pystray is installed and --tray is asked for.
    frozen = getattr(sys, 'frozen', False)
    use_tray = (frozen or args.tray) and not args.console and app_tray.available() and APP_ICON.exists()
    if not use_tray:
        SHUTDOWN['fn'] = lambda: threading.Thread(target=httpd.shutdown, daemon=True).start()
        if not args.keep_running:
            PAGES.watch(lambda: SHUTDOWN['fn']())
        print("Press Ctrl+C to stop." + ("" if args.keep_running else " Closing the editor tab stops it too."))
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
    if not args.keep_running:
        PAGES.watch(shutdown)
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
