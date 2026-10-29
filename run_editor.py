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
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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
import bug_report  # noqa: E402
import app_window  # noqa: E402
from google_api import (  # noqa: E402
    UPLOAD_PROGRESS, GoogleError, SignIn, _delete_photos,
    _list_photos, _photo_status, _update_photos, _upload_photo,
    _create_sequence, _delete_sequence, _list_sequences, _video_chunk, _video_query, _video_start,
    _download_photo,
)
from places import PLACES_CACHE, _geocode, _places_nearby  # noqa: E402
from panoramax import Panoramax  # noqa: E402

try:
    import _app_version  # written by build_exe.py / packaging/linux/build_deb.py
    APP_VERSION = _app_version.VERSION
except ImportError:
    _app_version = None
    APP_VERSION = "dev"

# SVT_PORT and SVT_DATA_DIR give a second, separate copy of the app (the tests use them).
PORT = int(os.environ.get('SVT_PORT') or 8420)
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
    if os.environ.get('SVT_DATA_DIR'):
        data = Path(os.environ['SVT_DATA_DIR'])
        return data, data / "webview"
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
LOG_MAX = 5 * 1024 * 1024  # then the log starts over; the one before stays as editor.log.1
if sys.stdout is None or sys.stderr is None or (PACKAGED and not sys.stdout.isatty()):
    _log_file = UPLOADER_DIR / "editor.log"
    try:
        if _log_file.stat().st_size > LOG_MAX:
            os.replace(_log_file, UPLOADER_DIR / "editor.log.1")
    except OSError:
        pass
    _log = open(_log_file, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log
    sys.stderr = _log
TOKEN_FILE = UPLOADER_DIR / "token.json"
CREDENTIALS_FILE = UPLOADER_DIR / "client_secrets.json"
# The Google sign-in, kept in memory and refreshed under a lock (google_api.SignIn).
GOOGLE = SignIn(TOKEN_FILE, CREDENTIALS_FILE)


def _load_credentials():
    """Valid credentials. RuntimeError('not_signed_in') when a sign-in is needed, GoogleError when offline."""
    return GOOGLE.credentials()


def _sign_in():
    """Run the interactive OAuth consent flow (opens the browser) and keep the resulting token."""
    creds = GOOGLE.sign_in()
    if APP['window']:
        APP['window'].focus()  # back from the browser to the editor
    return creds


# ── Google Places ─────────────────────────────────
# Optional, recommended: ties photos to their place on Google Maps - a building
# or business (Places API), a house number or a street (Geocoding API). Google
# hands out Place IDs only through these APIs, which need an API key of the
# user's own (and a billing account; the monthly free usage covers far more
# than this app needs). The key stays in this file: the page never sees it.
PLACES_KEY_FILE = UPLOADER_DIR / "places_api_key.txt"
# Panoramax, the open street-level photo platform: the instance and its token (panoramax.py).
PANORAMAX = Panoramax(UPLOADER_DIR / "panoramax.json")


def _places_key():
    try:
        return PLACES_KEY_FILE.read_text(encoding='utf-8').strip() or None
    except OSError:
        return None


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
# What a second launch may hand over (double-click): projects, nothing else. The
# page gets a file token for it, so it must never name e.g. the app's own settings.
PROJECT_SUFFIXES = ('.vrtour', '.zip')
# Files the page may write to without the user having picked them in a save dialog:
# a project it opened (Ctrl+S saves back into it). Everything else is read only.
WRITABLE_SUFFIXES = PROJECT_SUFFIXES
FILES_MAX = 20000  # tokens kept; the oldest go first (an import hands out one per photo)

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
FILES = {}  # token -> {'path': Path, 'kind': 'file' | 'folder', 'write': bool}
FOLDER_GRANTS_FILE = UPLOADER_DIR / "folders.json"
BAD_NAME_CHARS = set('<>:"/\\|?*')


def _file_token(path, kind, write=None):
    """A token for a file or folder; write: may the page write to it (default: folders and projects)."""
    path = Path(path)
    if write is None:
        write = kind == 'folder' or path.suffix.lower() in WRITABLE_SUFFIXES
    token = secrets.token_urlsafe(18)
    FILES[token] = {'path': path, 'kind': kind, 'write': write}
    while len(FILES) > FILES_MAX:
        FILES.pop(next(iter(FILES)))
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


# Photos the app window keeps by reference in its autosave instead of copying
# them into the web engine's storage once more: a file the user picked, or the
# project file a photo is in. The page only ever sees an opaque id; a file that
# changed or vanished since is not handed out again.
REFS_FILE = UPLOADER_DIR / "filerefs.json"
REFS_MAX = 5000


def _file_refs():
    try:
        refs = json.loads(REFS_FILE.read_text(encoding='utf-8'))
        return refs if isinstance(refs, dict) else {}
    except (OSError, ValueError):
        return {}


def _make_file_refs(paths):
    refs = _file_refs()
    out = []
    for path in paths:
        st = path.stat()
        rid = hashlib.sha256(f'{path}|{st.st_size}|{st.st_mtime_ns}'.encode('utf-8')).hexdigest()[:24]
        refs.pop(rid, None)
        refs[rid] = {'path': str(path), 'size': st.st_size, 'mtime': st.st_mtime_ns}
        out.append(rid)
    REFS_FILE.write_text(json.dumps(dict(list(refs.items())[-REFS_MAX:])), encoding='utf-8')
    return out


PHOTO_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp'}


def _folder_photos(folder, max_depth=3, limit=5000):
    """The photos in a folder picked for import, also in subfolders (a camera's DCIM/100XXX)."""
    found = []

    def walk(path, depth):
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name.lower())
        except OSError:
            return
        for e in entries:
            if len(found) >= limit:
                return
            if e.name.startswith('.'):  # hidden files, macOS "._" resource forks
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < max_depth:
                        walk(e.path, depth + 1)
                elif e.is_file() and Path(e.name).suffix.lower() in PHOTO_SUFFIXES:
                    found.append(Path(e.path))
            except OSError:
                continue

    walk(folder, 0)
    return found


# Projects opened or saved in the app window, newest first ("Recent" in the
# Project menu). The page names them by an id; it gets a file token to open one.
RECENT_FILE = UPLOADER_DIR / "recent.json"
RECENT_MAX = 8


def _recent_paths():
    try:
        paths = json.loads(RECENT_FILE.read_text(encoding='utf-8'))
        return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []
    except (OSError, ValueError):
        return []


def _recent_id(path):
    return hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:16]


def _save_recent(paths):
    RECENT_FILE.write_text(json.dumps(paths[:RECENT_MAX * 2], indent=1), encoding='utf-8')


def _recent_items():
    items = []
    for p in _recent_paths():
        path = Path(p)
        if path.is_file():  # a missing one may come back (USB drive), so it stays in the file
            items.append({'id': _recent_id(p), 'name': path.name, 'folder': str(path.parent)})
        if len(items) >= RECENT_MAX:
            break
    return items


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


def _editor_url(key=True):
    """The editor's address. It carries the version: web engines that cached an
    older editor under its address (before the server forbade caching) would
    otherwise keep showing it - a new address can't be in any cache."""
    stamp = APP_VERSION
    if stamp == 'dev':
        try:
            stamp = str(int((EDITOR_DIR / "editor.html").stat().st_mtime))
        except OSError:
            pass
    url = f"http://127.0.0.1:{PORT}/editor.html?v={stamp}"
    return f"{url}#k={EDITOR_KEY}" if key else url


class Handler(SimpleHTTPRequestHandler):
    # Fixed types for the editor's own files: Python would otherwise ask the
    # system (on Windows the registry, which some programs bend), and module
    # scripts and WebAssembly refuse to load under a wrong type.
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
        '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.wasm': 'application/wasm',
        '.woff2': 'font/woff2', '.tflite': 'application/octet-stream',
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(EDITOR_DIR), **kwargs)

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/') and not self.path.startswith(('/api/update/status', '/api/pending-project', '/api/panoramax/check', '/api/upload-progress')):
            super().log_message(fmt, *args)

    # Nothing from here may be kept in a browser cache. Without Cache-Control a
    # web engine guesses how long a file stays valid and, after an update, kept
    # showing the old editor. Everything is local, so loading it again is cheap.
    def send_response(self, *args, **kwargs):
        self._cache_header = False
        super().send_response(*args, **kwargs)

    def send_header(self, keyword, value):
        if keyword.lower() == 'cache-control':
            self._cache_header = True
        super().send_header(keyword, value)

    def end_headers(self):
        if not getattr(self, '_cache_header', True):
            self.send_header('Cache-Control', 'no-store')
        super().end_headers()

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
            self.send_header('Location', _editor_url(key=False))
            self.end_headers()
            return
        if parsed.path == '/api/status':
            had_token = TOKEN_FILE.exists()
            signed_in = GOOGLE.signed_in()
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
        if parsed.path == '/api/recent':
            return self._json(200, {'ok': True, 'items': _recent_items() if APP['window'] else []})
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
        if not entry.get('write'):
            return self._json(403, {'ok': False, 'error': 'This file was opened for reading only.'})
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

    def _video_chunk(self, parsed):
        """One piece of a 360° video, streamed on to Google's resumable upload."""
        self.close_connection = True  # an unread body must not leak into a next request
        query = parse_qs(parsed.query)
        job = query.get('job', [''])[0] or None
        length = int(self.headers.get('Content-Length') or 0)
        try:
            creds = _load_credentials()
        except GoogleError as e:
            _drain_body(self.rfile, length)
            return self._json(200, {'ok': False, 'error': e.message, 'status': e.status, 'step': e.step})
        except RuntimeError:
            _drain_body(self.rfile, length)
            return self._json(401, {'ok': False, 'error': 'not_signed_in'})
        try:
            received = _video_chunk(creds, query.get('url', [''])[0], self.rfile, length,
                                    int(query.get('offset', ['0'])[0]), query.get('final', ['0'])[0] == '1', job)
        except GoogleError as e:
            return self._json(200, {'ok': False, 'error': e.message, 'status': e.status, 'step': e.step})
        except Exception as e:  # e.g. the connection to Google broke: the editor asks how far it got
            return self._json(200, {'ok': False, 'error': str(e), 'status': 0, 'step': 'upload'})
        finally:
            UPLOAD_PROGRESS.pop(job, None)
        return self._json(200, {'ok': True, 'received': received})

    # ── POST: what the editor asks the app to do ──
    # Each path has a method of its own (POST_ROUTES, below the class). Bodies that
    # can be hundreds of MB are streamed: handled before the body would be read.
    def do_POST(self):
        if self._forbidden_origin():
            return self._json(403, {'ok': False, 'error': 'forbidden'})
        parsed = urlparse(self.path)
        if parsed.path == '/api/files/write':  # can be hundreds of MB: streamed, not read into memory
            if not self._key_ok():
                return self._bad_key()
            return self._write_picked_file(parsed)
        if parsed.path == '/api/video/chunk':  # a piece of a 360° video: streamed on to Google
            if not self._key_ok():
                _drain_body(self.rfile, int(self.headers.get('Content-Length') or 0))
                return self._bad_key()
            return self._video_chunk(parsed)
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else b''

        if parsed.path == '/api/bye':
            # Sent with navigator.sendBeacon, which can't set headers: key in the body.
            try:
                payload = json.loads(body.decode('utf-8') or '{}')
            except ValueError:
                payload = {}
            if not self._key_ok(str(payload.get('key', ''))):
                return self._bad_key()
            PAGES.bye(str(payload.get('page', '')))
            return self._json(200, {'ok': True})
        if not self._key_ok():
            return self._bad_key()
        handler = POST_ROUTES.get(parsed.path)
        if not handler:
            return self._json(404, {'ok': False, 'error': 'not_found'})
        try:
            return getattr(self, handler)(parsed, body)
        except GoogleError as e:  # e.g. offline while the sign-in had to be renewed
            return self._json(200, {'ok': False, 'error': e.message, 'status': e.status, 'step': e.step})
        except RuntimeError as e:
            return self._json(400, {'ok': False, 'error': str(e)})
        except Exception as e:
            return self._json(500, {'ok': False, 'error': str(e)})

    @staticmethod
    def _payload(body):
        return json.loads(body.decode('utf-8') or '{}')

    @staticmethod
    def _creds():
        """The Google sign-in, or None (then the editor is told 'not_signed_in'). Offline: GoogleError."""
        try:
            return _load_credentials()
        except GoogleError:
            raise
        except RuntimeError:
            return None

    def _not_signed_in(self):
        return self._json(401, {'ok': False, 'error': 'not_signed_in'})

    def _send_bytes(self, data, content_type):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    # The editor pages and the window
    def _post_hello(self, parsed, body):
        PAGES.hello(str(self._payload(body).get('page', '')))
        return self._json(200, {'ok': True})

    def _post_busy(self, parsed, body):
        # While e.g. an upload runs, closing the window asks first (the page sends the question).
        if APP['window']:
            APP['window'].set_busy(str(self._payload(body).get('question') or '')[:500])
        return self._json(200, {'ok': True})

    def _post_window_title(self, parsed, body):
        if APP['window']:
            APP['window'].set_title(str(self._payload(body).get('title') or '')[:200])
        return self._json(200, {'ok': True})

    def _post_window_theme(self, parsed, body):
        # The editor switched light / dark: the window frame follows.
        if APP['window']:
            APP['window'].set_theme(self._payload(body).get('theme') == 'dark')
        return self._json(200, {'ok': True})

    def _post_focus(self, parsed, body):
        # A second launch of the app: show this window, and open a project it brought along.
        if APP['window']:
            APP['window'].focus()
            if PENDING['path']:
                APP['window'].run_js('window.__svtOpenPending && window.__svtOpenPending()')
        return self._json(200, {'ok': True, 'window': bool(APP['window'])})

    def _post_open_file(self, parsed, body):
        # A second launch hands over the project it was started with (see main()).
        path = Path(json.loads(body.decode('utf-8')).get('path') or '')
        if path.suffix.lower() not in PROJECT_SUFFIXES or not path.is_file():
            raise RuntimeError(f"Not a project file: {path}")
        PENDING['path'] = str(path.resolve())
        return self._json(200, {'ok': True})

    def _post_update_install(self, parsed, body):
        if not app_update.is_installed():
            raise RuntimeError("Only the installed app can update itself.")
        app_update.start_install(APP_VERSION, lambda: SHUTDOWN['fn']())
        return self._json(200, {'ok': True})

    def _post_report(self, parsed, body):
        # "Create a bug report": a zip to save, without sign-in, keys or photos.
        data = bug_report.build(
            APP_VERSION, UPLOADER_DIR, self._payload(body), packaged=PACKAGED, window=bool(APP['window']),
            signed_in=TOKEN_FILE.exists(), places_key=bool(_places_key()),
            secrets=[EDITOR_KEY, _places_key(), (PANORAMAX.config() or {}).get('token')])
        return self._send_bytes(data, 'application/zip')

    # Files in the app window: only where the user picked them in the system's dialogs
    def _post_files_pick(self, parsed, body):
        if not APP['window']:
            return self._json(404, {'ok': False, 'error': 'not_found'})
        payload = self._payload(body)
        kind = payload.get('kind')
        if kind not in ('open', 'save', 'folder'):
            raise RuntimeError(f"Unknown dialog: {kind}")
        multiple = kind == 'open' and bool(payload.get('multiple'))
        path = APP['window'].pick(
            kind, suggested_name=Path(str(payload.get('suggestedName') or '')).name,
            filters=_file_filters(payload.get('types')),
            all_files_label='Alle Dateien' if app_tray.GERMAN else 'All files', multiple=multiple)
        if not path:
            return self._json(200, {'ok': True, 'cancelled': True})
        if multiple:
            return self._json(200, {'ok': True, 'files': [
                {'token': _file_token(p, 'file'), 'name': Path(p).name} for p in path]})
        path = Path(path)
        if kind == 'folder':
            # Export folders are remembered (grant) and written to; a folder to import photos from is only read.
            export = bool(payload.get('remember', True))
            grant = _remember_folder(path) if export else None
            return self._json(200, {'ok': True, 'token': _file_token(path, 'folder', write=export),
                                    'name': path.name or str(path), 'grant': grant})
        # Picked in a save dialog: the user chose to write there.
        token = _file_token(path, 'file', write=True if kind == 'save' else None)
        return self._json(200, {'ok': True, 'token': token, 'name': path.name})

    def _post_files_photos(self, parsed, body):
        # The photos in a folder the user picked (import): a token for each.
        entry = FILES.get(str(self._payload(body).get('token') or ''))
        if not APP['window'] or not entry or entry['kind'] != 'folder' or not entry['path'].is_dir():
            return self._json(404, {'ok': False, 'error': 'folder_not_found'})
        root = entry['path']
        return self._json(200, {'ok': True, 'files': [
            {'token': _file_token(p, 'file'), 'name': p.name, 'path': p.relative_to(root).as_posix()}
            for p in _folder_photos(root)]})

    def _post_files_ref(self, parsed, body):
        payload = self._payload(body)
        if not APP['window']:
            return self._json(404, {'ok': False, 'error': 'not_found'})
        if parsed.path == '/api/files/ref':  # files the page holds tokens for -> ids for the autosave
            entries = [FILES.get(str(tok)) for tok in payload.get('tokens', [])]
            paths = [e['path'] for e in entries if e and e['kind'] == 'file' and e['path'].is_file()]
            if len(paths) != len(entries):
                return self._json(200, {'ok': False, 'error': 'unknown_file'})
            return self._json(200, {'ok': True, 'refs': _make_file_refs(paths)})
        ref = _file_refs().get(str(payload.get('ref') or ''))
        path = ref and Path(ref['path'])
        if not path or not path.is_file():
            return self._json(200, {'ok': False, 'error': 'gone'})
        st = path.stat()
        if (st.st_size, st.st_mtime_ns) != (ref['size'], ref['mtime']):
            return self._json(200, {'ok': False, 'error': 'changed'})
        return self._json(200, {'ok': True, 'token': _file_token(path, 'file'), 'name': path.name})

    def _post_files_folder(self, parsed, body):
        # The export folder picked in an earlier run (the page keeps only the grant id).
        path = _folder_grants().get(str(self._payload(body).get('grant') or ''))
        if not APP['window'] or not path or not Path(path).is_dir():
            return self._json(200, {'ok': False, 'error': 'folder_gone'})
        return self._json(200, {'ok': True, 'token': _file_token(path, 'folder'), 'name': Path(path).name or path})

    def _post_recent(self, parsed, body):
        payload = self._payload(body)
        if not APP['window']:
            return self._json(404, {'ok': False, 'error': 'not_found'})
        if parsed.path == '/api/recent/add':  # a project was just opened or saved here
            entry = FILES.get(str(payload.get('token') or ''))
            if entry and entry['kind'] == 'file':
                path = str(entry['path'])
                _save_recent([path] + [p for p in _recent_paths() if p != path])
            return self._json(200, {'ok': True})
        rid = str(payload.get('id') or '')
        path = next((p for p in _recent_paths() if _recent_id(p) == rid), None)
        if parsed.path == '/api/recent/remove':
            _save_recent([p for p in _recent_paths() if p != path])
            return self._json(200, {'ok': True})
        if not path or not Path(path).is_file():
            return self._json(200, {'ok': False, 'error': 'file_gone'})
        return self._json(200, {'ok': True, 'token': _file_token(path, 'file'), 'name': Path(path).name})

    # Google sign-in
    def _post_set_credentials(self, parsed, body):
        payload = json.loads(body.decode('utf-8'))
        GOOGLE.save_client(payload.get('client_id'), payload.get('client_secret'))
        return self._json(200, {'ok': True})

    def _post_clear_credentials(self, parsed, body):
        GOOGLE.clear()
        return self._json(200, {'ok': True})

    def _post_login(self, parsed, body):
        _sign_in()
        return self._json(200, {'ok': True})

    # Street View: photos
    def _post_upload_photo(self, parsed, body):
        qs = parse_qs(parsed.query)
        filename = qs.get('filename', [''])[0]
        lat = float(qs['lat'][0])
        lng = float(qs['lng'][0])
        heading = float(qs['heading'][0]) if qs.get('heading', [''])[0] else None
        place_id = qs.get('place_id', [''])[0] or None
        job = qs.get('job', [''])[0] or None
        level = ({'number': int(qs['level'][0]), 'name': qs.get('level_name', [''])[0][:3]}
                 if qs.get('level', [''])[0] else None)
        creds = self._creds()
        if not creds:
            return self._not_signed_in()
        try:
            photo_id = _upload_photo(creds, body, filename, lat, lng, heading, place_id, job, level)
        except GoogleError as e:
            return self._json(200, {'ok': False, 'error': e.message, 'status': e.status,
                                    'step': e.step, 'kind': e.kind})
        finally:
            UPLOAD_PROGRESS.pop(job, None)
        return self._json(200, {'ok': True, 'photoId': photo_id})

    def _post_google_photo(self, parsed, body):
        # A photo's bytes from Google ("Photos from Google"): as a file, or an error as JSON.
        creds = self._creds()
        if not creds:
            return self._not_signed_in()
        try:
            data = _download_photo(creds, str(self._payload(body).get('id') or ''))
        except GoogleError as e:
            return self._json(200, {'ok': False, 'error': e.message, 'status': e.status})
        return self._send_bytes(data, 'image/jpeg')

    def _post_photos(self, parsed, body):
        payload = self._payload(body)
        creds = self._creds()
        if not creds:
            return self._not_signed_in()
        if parsed.path == '/api/list-photos':
            return self._json(200, {'ok': True, 'photos': _list_photos(creds)})
        if parsed.path == '/api/delete-photos':
            return self._json(200, {'ok': True, 'results': _delete_photos(creds, payload.get('ids', []))})
        if parsed.path == '/api/photo-status':
            return self._json(200, {'ok': True, 'photos': _photo_status(creds, payload.get('ids', []))})
        return self._json(200, {'ok': True, 'results': _update_photos(creds, payload.get('items', []))})

    # Street View: 360° videos (the pieces themselves: _video_chunk, streamed)
    def _post_video(self, parsed, body):
        payload = self._payload(body)
        creds = self._creds()
        if not creds:
            return self._not_signed_in()
        try:
            if parsed.path == '/api/video/start':
                upload_url, session_url = _video_start(creds, int(payload.get('size') or 0), str(payload.get('type') or 'video/mp4'))
                return self._json(200, {'ok': True, 'uploadUrl': upload_url, 'session': session_url})
            if parsed.path == '/api/video/query':
                return self._json(200, {'ok': True, **_video_query(creds, str(payload.get('session') or ''))})
            if parsed.path == '/api/video/list':
                return self._json(200, {'ok': True, 'sequences': _list_sequences(creds)})
            if parsed.path == '/api/video/delete':
                _delete_sequence(creds, str(payload.get('id') or ''))
                return self._json(200, {'ok': True})
            gps = [{'lat': float(p['lat']), 'lng': float(p['lng']), 'time': str(p['time'])}
                   for p in payload.get('gps') or []]
            sid = _create_sequence(creds, str(payload.get('uploadUrl') or ''), gps,
                                   str(payload.get('captureTime') or '') or None)
            return self._json(200, {'ok': True, 'id': sid})
        except GoogleError as e:
            return self._json(200, {'ok': False, 'error': e.message, 'status': e.status, 'step': e.step})

    # Places on Google Maps (the user's own API key)
    def _post_places(self, parsed, body):
        payload = self._payload(body)
        lang = 'de' if payload.get('lang') == 'de' else 'en'
        if parsed.path == '/api/places':
            return self._json(200, {'ok': True, 'configured': bool(_places_key())})
        if parsed.path == '/api/places/key':
            return self._save_places_key(payload, lang)
        key = _places_key()
        if not key:
            return self._json(200, {'ok': False, 'error': 'no_key'})
        # 'fetched': how many lookups really went to Google (the rest came from the cache),
        # so the editor can show what was used of the free monthly usage.
        try:
            if parsed.path == '/api/places/geocode':
                points = [(float(p['lat']), float(p['lng'])) for p in payload.get('points', [])][:500]
                fetched = len({(round(a, 5), round(b, 5)) for a, b in points
                               if ('geo', key, round(a, 5), round(b, 5), lang) not in PLACES_CACHE})
                with ThreadPoolExecutor(max_workers=6) as pool:
                    found = list(pool.map(lambda ll: _geocode(key, ll[0], ll[1], lang), points))
                return self._json(200, {'ok': True, 'results': found, 'fetched': fetched})
            lat, lng = float(payload['lat']), float(payload['lng'])
            radius = min(max(float(payload.get('radius', 50)), 5.0), 500.0)
            fetched = int((key, round(lat, 5), round(lng, 5), round(radius), lang) not in PLACES_CACHE)
            places = _places_nearby(key, lat, lng, radius, lang)
        except GoogleError as e:
            return self._json(200, {'ok': False, 'error': e.message, 'status': e.status, 'step': e.step})
        return self._json(200, {'ok': True, 'places': places, 'fetched': fetched})

    # Panoramax (the instance's token stays here, as the Places key does)
    def _post_panoramax(self, parsed, body):
        payload = self._payload(body)
        action = parsed.path.rsplit('/', 1)[-1]
        try:
            if action == 'connect':  # a new token, claimed by the user in the browser
                return self._json(200, {'ok': True, 'claim': PANORAMAX.start_sign_in(payload.get('instance'))})
            if action == 'check':
                return self._json(200, {'ok': True, 'done': PANORAMAX.finish_sign_in(), **PANORAMAX.info()})
            if action == 'token':
                PANORAMAX.use_token(payload.get('instance'), payload.get('token'))
            elif action == 'forget':
                PANORAMAX.forget()
            elif action == 'start':
                return self._json(200, {'ok': True, 'set': PANORAMAX.create_set(str(payload.get('title') or ''), payload.get('count') or 1)})
            elif action == 'complete':
                PANORAMAX.complete(str(payload.get('set') or ''))
            elif action == 'status':
                return self._json(200, {'ok': True, **PANORAMAX.status(str(payload.get('set') or ''))})
            return self._json(200, {'ok': True, **PANORAMAX.info()})
        except RuntimeError as e:
            if str(e) == 'not_signed_in':
                return self._json(401, {'ok': False, 'error': 'not_signed_in'})
            raise

    def _post_panoramax_file(self, parsed, body):
        # One photo, already carrying its position and north (written by the editor).
        qs = parse_qs(parsed.query)
        try:
            out = PANORAMAX.add_file(qs.get('set', [''])[0], Path(qs.get('name', ['photo.jpg'])[0]).name, body,
                                     qs.get('blurred', ['0'])[0] == '1')
        except RuntimeError as e:
            if str(e) == 'not_signed_in':
                return self._json(401, {'ok': False, 'error': 'not_signed_in'})
            raise
        return self._json(200, {'ok': True, **out})

    def _save_places_key(self, payload, lang):
        key = str(payload.get('key') or '').strip()
        if not key:
            PLACES_KEY_FILE.unlink(missing_ok=True)
            return self._json(200, {'ok': True, 'configured': False})
        if not re.fullmatch(r'[A-Za-z0-9_\-]{20,80}', key):
            return self._json(200, {'ok': False, 'error': 'That does not look like an API key (AIza…).'})
        # One lookup with each API, to tell which of them the key works for.
        lat, lng = float(payload.get('lat', 48.8584)), float(payload.get('lng', 2.2945))
        errors = {}
        try:
            _places_nearby(key, lat, lng, 50, lang)
        except GoogleError as e:
            errors['places'] = e.message
        try:
            _geocode(key, lat, lng, lang)
        except GoogleError as e:
            errors['geocoding'] = e.message
        if len(errors) == 2:
            return self._json(200, {'ok': False, 'error': errors['places'], 'errors': errors})
        PLACES_KEY_FILE.write_text(key, encoding='utf-8')
        return self._json(200, {'ok': True, 'configured': True, 'places': 'places' not in errors,
                                'geocoding': 'geocoding' not in errors, 'errors': errors})


# Which method of Handler answers which POST path.
POST_ROUTES = {
    '/api/hello': '_post_hello',
    '/api/busy': '_post_busy',
    '/api/window-title': '_post_window_title',
    '/api/window-theme': '_post_window_theme',
    '/api/focus': '_post_focus',
    '/api/open-file': '_post_open_file',
    '/api/update/install': '_post_update_install',
    '/api/report': '_post_report',
    '/api/files/pick': '_post_files_pick',
    '/api/files/photos': '_post_files_photos',
    '/api/files/ref': '_post_files_ref',
    '/api/files/ref/open': '_post_files_ref',
    '/api/files/folder': '_post_files_folder',
    '/api/recent/add': '_post_recent',
    '/api/recent/open': '_post_recent',
    '/api/recent/remove': '_post_recent',
    '/api/set-credentials': '_post_set_credentials',
    '/api/clear-credentials': '_post_clear_credentials',
    '/api/login': '_post_login',
    '/api/upload-photo': '_post_upload_photo',
    '/api/google-photo': '_post_google_photo',
    '/api/list-photos': '_post_photos',
    '/api/delete-photos': '_post_photos',
    '/api/photo-status': '_post_photos',
    '/api/update-photos': '_post_photos',
    '/api/video/start': '_post_video',
    '/api/video/query': '_post_video',
    '/api/video/create': '_post_video',
    '/api/video/list': '_post_video',
    '/api/video/delete': '_post_video',
    '/api/places': '_post_places',
    '/api/panoramax': '_post_panoramax',
    '/api/panoramax/connect': '_post_panoramax',
    '/api/panoramax/check': '_post_panoramax',
    '/api/panoramax/token': '_post_panoramax',
    '/api/panoramax/forget': '_post_panoramax',
    '/api/panoramax/start': '_post_panoramax',
    '/api/panoramax/complete': '_post_panoramax',
    '/api/panoramax/status': '_post_panoramax',
    '/api/panoramax/file': '_post_panoramax_file',
    '/api/places/key': '_post_places',
    '/api/places/nearby': '_post_places',
    '/api/places/geocode': '_post_places',
}


def _drain_body(stream, length):
    """Read and drop a request body that is not used (else the client is still sending when answered)."""
    while length > 0:
        chunk = stream.read(min(length, 1024 * 1024))
        if not chunk:
            break
        length -= len(chunk)


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
            webbrowser.open(_editor_url())
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
    url = _editor_url()

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
