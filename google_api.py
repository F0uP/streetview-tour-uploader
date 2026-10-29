"""Street View Publish API: sign in, upload photos, link them, read them back, update and delete them.

Every call takes the signed-in user's credentials (SignIn, below). The Google
libraries are imported on demand, so plain editing never needs them installed.
The app (run_editor.py) and the command-line tools (uploader/) share all of it.
"""

import base64
import io
import json
import os
import secrets
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# SVT_STREETVIEW_API: the tests point the app at a fake Google with it.
BASE_URL = os.environ.get('SVT_STREETVIEW_API') or 'https://streetviewpublish.googleapis.com/v1'
SCOPES = ['https://www.googleapis.com/auth/streetviewpublish']

# Every request gives up at some point: a connection that hangs must not stop an upload for good.
TIMEOUT = (15, 120)          # seconds to connect, seconds without an answer
UPLOAD_TIMEOUT = (15, 300)   # a 75 MB photo can take Google a while to take in
# Google is busy or the connection broke: worth another try (a few seconds later).
RETRY_STATUS = {429, 500, 502, 503, 504}
OFFLINE = 'Google could not be reached - check the internet connection.'


def _google_libs():
    """Import the Google/requests libs on demand so plain editing (no upload) never needs them installed."""
    try:
        import requests
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleRequest
        from google_auth_oauthlib.flow import InstalledAppFlow
        return requests, Credentials, GoogleRequest, InstalledAppFlow
    except ImportError as e:
        raise RuntimeError(
            "Missing dependencies for uploading. Run: pip install -r requirements.txt"
        ) from e


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


def _session(creds=None, timeout=TIMEOUT):
    """A requests session for Google: every request with a timeout; reading and deleting
    (safe to repeat) is tried again a few times when Google is busy or the connection broke."""
    requests, *_ = _google_libs()
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=sorted(RETRY_STATUS),
                  allowed_methods={'GET', 'HEAD', 'DELETE'}, raise_on_status=False)
    for prefix in ('https://', 'http://'):
        session.mount(prefix, HTTPAdapter(max_retries=retry))
    send = session.request
    session.request = lambda method, url, **kwargs: send(method, url, **{'timeout': timeout, **kwargs})
    if creds is not None:
        session.headers['Authorization'] = f'Bearer {creds.token}'
    return session


def _post(session, url, step, attempts=3, retry_status=RETRY_STATUS, retry_errors=None, **kwargs):
    """POST with a few more tries where it is safe; a broken connection becomes a GoogleError.
    retry_errors: the exceptions after which the request certainly did not reach Google."""
    requests, *_ = _google_libs()
    retry_errors = retry_errors or (requests.ConnectionError, requests.Timeout)
    data = kwargs.pop('data', None)
    for attempt in range(attempts):
        body = data() if callable(data) else data  # a stream is read once: a fresh one for every try
        try:
            r = session.post(url, data=body, **kwargs)
        except retry_errors as e:
            if attempt == attempts - 1:
                raise GoogleError(step, 0, OFFLINE) from e
        except requests.RequestException as e:
            raise GoogleError(step, 0, OFFLINE) from e
        else:
            if r.status_code not in retry_status or attempt == attempts - 1:
                return r
        time.sleep(2 * 2 ** attempt)


# ── Sign-in ───────────────────────────────────────
# Kept in memory and refreshed under a lock: the app answers several requests at
# once, and they must not refresh the token at the same time or read a half-written
# file. Only Google saying the sign-in is no longer valid ends it - not being offline.
SIGN_IN_PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#060608; color:#f3f1f7;
  font:15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ padding:32px 36px; max-width:420px; border-radius:16px; background:#100e16; border:1px solid rgba(255,255,255,.08); text-align:center; }}
h1 {{ margin:0 0 8px; font-size:20px; }} p {{ margin:0; color:#9793a6; }}
</style></head><body><main><h1>{title}</h1><p>{text}</p></main></body></html>"""

SIGN_IN_TEXT = {
    (True, True): ('Anmeldung abgeschlossen', 'Du kannst diesen Tab schließen und im Streetview Tour Editor weitermachen.'),
    (True, False): ('Anmeldung abgebrochen', 'Du kannst diesen Tab schließen und es im Editor noch einmal versuchen.'),
    (False, True): ('Signed in', 'You can close this tab and continue in the Streetview Tour Editor.'),
    (False, False): ('Sign-in cancelled', 'You can close this tab and try again from the editor.'),
}


class SignIn:
    """The Google sign-in: the OAuth client (client_secrets.json) and the token (token.json)."""

    def __init__(self, token_file, credentials_file):
        self.token_file = Path(token_file)
        self.credentials_file = Path(credentials_file)
        self._lock = threading.Lock()
        self._creds = None
        self._stamp = None

    def _file_stamp(self):
        try:
            st = self.token_file.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def credentials(self):
        """Valid credentials, refreshed if needed. RuntimeError('not_signed_in') when the user has
        to sign in (again); GoogleError when Google can't be reached to refresh them."""
        _, Credentials, GoogleRequest, _ = _google_libs()
        from google.auth.exceptions import RefreshError
        with self._lock:
            stamp = self._file_stamp()
            if stamp is None:
                self._creds = None
                raise RuntimeError("not_signed_in")
            if self._creds is None or stamp != self._stamp:  # also a sign-in by a command-line tool
                try:
                    self._creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
                except (OSError, ValueError, KeyError) as e:
                    self._creds = None
                    raise RuntimeError("not_signed_in") from e
                self._stamp = stamp
            creds = self._creds
            if creds.valid:
                return creds
            if not creds.refresh_token:
                raise RuntimeError("not_signed_in")
            try:
                creds.refresh(GoogleRequest())
            except RefreshError as e:
                if getattr(e, 'retryable', False):  # Google's sign-in service is busy, the sign-in is fine
                    raise GoogleError('sign-in', 503, 'Google could not renew the sign-in just now - try again in a moment.') from e
                # Revoked or run out: for Cloud projects in "Testing" mode Google lets
                # refresh tokens expire after 7 days. Forget it, so the editor says
                # "not signed in" instead of showing a cryptic invalid_grant error.
                print(f"Google sign-in expired: {e}")
                self._forget()
                raise RuntimeError("not_signed_in") from e
            except Exception as e:  # offline, a proxy, a DNS failure: try again later, keep the sign-in
                raise GoogleError('sign-in', 0, OFFLINE) from e
            self._save(creds)
            return creds

    def signed_in(self):
        """True if there is a token that works - or can't be checked right now (offline)."""
        try:
            self.credentials()
            return True
        except GoogleError:
            return True
        except RuntimeError:
            return False

    def _save(self, creds):
        """Write the token in one go: other requests may read it at the same moment."""
        tmp = self.token_file.with_name(f'.{self.token_file.name}.{secrets.token_hex(4)}.tmp')
        tmp.write_text(creds.to_json(), encoding='utf-8')
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.token_file)
        self._creds, self._stamp = creds, self._file_stamp()

    def _forget(self):
        self.token_file.unlink(missing_ok=True)
        self._creds = self._stamp = None

    def clear(self):
        with self._lock:
            self._forget()
            self.credentials_file.unlink(missing_ok=True)

    def save_client(self, client_id, client_secret):
        """Write a client_secrets.json in Google's standard 'installed app' shape from just the
        two values shown on the OAuth client's page, so nobody has to edit a JSON file."""
        client_id = (client_id or '').strip()
        client_secret = (client_secret or '').strip()
        if not client_id or not client_secret:
            raise RuntimeError("Client ID and Client Secret are both required.")
        doc = {"installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }}
        self.credentials_file.write_text(json.dumps(doc, indent=2), encoding='utf-8')

    def sign_in(self, open_url=webbrowser.open, wait=180):
        """The OAuth consent in the browser, answered on a port of this computer only.
        Google doesn't allow sign-ins in embedded windows, so it is the default browser."""
        _, _, _, InstalledAppFlow = _google_libs()
        if not self.credentials_file.exists():
            raise RuntimeError(
                f"client_secrets.json not found. Save your OAuth desktop client credentials to {self.credentials_file}")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), SCOPES)
        received = {}
        state = None

        class Answer(BaseHTTPRequestHandler):
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
                title, text = SIGN_IN_TEXT[(german, bool(received['code']))]
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(SIGN_IN_PAGE.format(title=title, text=text, lang='de' if german else 'en').encode('utf-8'))

            def log_message(self, *args):
                pass

        # Only reachable from this computer (the redirect goes to 127.0.0.1), and the
        # state value proves the answer belongs to this sign-in. Desktop OAuth clients
        # accept any loopback port, so fall back to a free one if 8085 is taken.
        try:
            server = HTTPServer(('127.0.0.1', 8085), Answer)
        except OSError:
            server = HTTPServer(('127.0.0.1', 0), Answer)
        server.timeout = 1
        flow.redirect_uri = f'http://127.0.0.1:{server.server_address[1]}/'
        auth_url, state = flow.authorization_url(prompt='consent', access_type='offline')

        def serve():
            deadline = time.time() + wait
            while not received.get('done') and time.time() < deadline:
                server.handle_request()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        open_url(auth_url)
        t.join(timeout=wait + 1)
        server.server_close()
        if not received.get('code'):
            raise RuntimeError("Sign-in timed out or was cancelled. Please try again.")
        flow.fetch_token(code=received['code'])
        with self._lock:
            self._save(flow.credentials)
        return flow.credentials


# ── Photos ────────────────────────────────────────
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


def _upload_photo(creds, data, filename, lat, lng, heading=None, place_id=None, job=None, level=None, altitude=None):
    requests, *_ = _google_libs()
    session = _session(creds, UPLOAD_TIMEOUT)
    if job:
        UPLOAD_PROGRESS[job] = {'phase': 'starting', 'sent': 0, 'total': len(data)}

    r = _post(session, f'{BASE_URL}/photo:startUpload', 'start', json={})
    if not r.ok:
        raise GoogleError('start', r.status_code, _google_message(r))
    upload_url = r.json()['uploadUrl']

    # Sending the bytes again to the same address is harmless: try again after a broken connection.
    r = _post(session, upload_url, 'upload', data=lambda: _ProgressReader(data, job), headers={
        'Content-Type': 'image/jpeg',
        'X-Goog-Upload-Protocol': 'raw',
        'X-Goog-Upload-Content-Length': str(len(data)),
    })
    if not r.ok:
        raise GoogleError('upload', r.status_code, _google_message(r))

    if job:
        UPLOAD_PROGRESS[job] = {'phase': 'creating', 'sent': len(data), 'total': len(data)}
    pose = {'latLngPair': {'latitude': lat, 'longitude': lng}}
    if heading is not None:
        pose['heading'] = heading
    if altitude is not None:
        pose['altitude'] = altitude
    if level:  # the floor, inside buildings: {'number': 1, 'name': '1st floor'}
        pose['level'] = level
    body = {'uploadReference': {'uploadUrl': upload_url}, 'pose': pose}
    if place_id:
        body['places'] = [{'placeId': place_id}]

    # Asked again only when the photo was certainly not made yet (else it would be there twice).
    r = _post(session, f'{BASE_URL}/photo', 'create', retry_status={429, 503},
              retry_errors=(requests.ConnectTimeout,), json=body)
    if not r.ok:
        message = _google_message(r)
        # A wrong Place ID is by far the most common reason Google refuses an
        # otherwise fine photo with "400 Bad Request".
        kind = 'place' if place_id and r.status_code == 400 else 'other'
        raise GoogleError('create', r.status_code, message, kind)
    return r.json().get('photoId', {}).get('id')


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


def _get(session, url, step, **kwargs):
    requests, *_ = _google_libs()
    try:
        return session.get(url, **kwargs)
    except requests.RequestException as e:
        raise GoogleError(step, 0, OFFLINE) from e


def _photo_status(creds, ids):
    """Ask Google how far each uploaded photo got.

    Returns {id: {status, shareLink, resolvedId}} where status is 'published',
    'processing' (known to Google, not on Maps yet), 'rejected', or 'notfound'
    (not visible yet - normal for a while right after uploading - or deleted).
    resolvedId is the form of the ID Google actually knows.
    """
    session = _session(creds)
    cands = {pid: _id_candidates(pid) for pid in ids}
    flat = list(dict.fromkeys(c for cs in cands.values() for c in cs))
    found = {}
    for start in range(0, len(flat), 20):  # batchGet takes at most 20 IDs
        chunk = flat[start:start + 20]
        params = [('photoIds', i) for i in chunk] + [('view', 'BASIC')]
        r = _get(session, f'{BASE_URL}/photos:batchGet', 'status', params=params)
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
    """What Google has stored for a photo: position, heading, floor, its links - and how often it was viewed."""
    pose = photo.get('pose') or {}
    ll = pose.get('latLngPair') or {}
    return {
        'lat': ll.get('latitude'),
        'lng': ll.get('longitude'),
        'heading': pose.get('heading'),
        'level': pose.get('level'),
        'connections': [((c.get('target') or {}).get('id')) for c in photo.get('connections') or [] if (c.get('target') or {}).get('id')],
        'viewCount': int(photo.get('viewCount') or 0),
        'placeId': next((p.get('placeId') for p in photo.get('places') or [] if p.get('placeId')), None),
        'placeName': next((p.get('name') for p in photo.get('places') or [] if p.get('placeId')), None),
        'thumbnailUrl': photo.get('thumbnailUrl'),
        'captureTime': photo.get('captureTime'),
    }


def _download_photo(creds, pid):
    """A photo's own bytes as Google keeps them - to load photos from Google into the editor."""
    session = _session(creds)
    for cid in _id_candidates(pid):
        r = _get(session, f'{BASE_URL}/photo/{cid}', 'download', params={'view': 'INCLUDE_DOWNLOAD_URL'})
        if r.status_code in (400, 404):
            continue  # another form of the ID, maybe
        if not r.ok:
            raise GoogleError('download', r.status_code, _google_message(r))
        url = r.json().get('downloadUrl')
        if not url:
            raise GoogleError('download', 404, 'Google offers no download of this photo.')
        # The download address is signed for itself; only if Google wants the sign-in, it goes along.
        d = _get(_session(timeout=(15, 180)), url, 'download')
        if d.status_code in (401, 403):
            d = _get(_session(creds, (15, 180)), url, 'download')
        if not d.ok:
            raise GoogleError('download', d.status_code, _google_message(d))
        return d.content
    raise GoogleError('download', 404, 'This photo is not on Google (any more).')


def _list_photos(creds):
    """All of the signed-in user's Street View photos (ID, position, status)."""
    session = _session(creds)
    photos, token = [], None
    for _ in range(50):  # 50 pages x 100 photos is plenty
        params = {'view': 'BASIC', 'pageSize': 100}
        if token:
            params['pageToken'] = token
        r = _get(session, f'{BASE_URL}/photos', 'list', params=params)
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
    session = _session(creds)
    results = []
    for pid in ids:
        refused, gone, result = None, False, None
        for cid in _id_candidates(pid):
            try:
                r = session.delete(f'{BASE_URL}/photo/{cid}')
            except requests.RequestException:
                result = {'id': pid, 'ok': False, 'error': OFFLINE}
                break
            if r.ok:
                result = {'id': pid, 'ok': True, 'error': None}
                break
            if r.status_code == 404:
                gone = True  # not (or no longer) there in this form
            elif r.status_code == 400:
                refused = _google_message(r)  # a form of the ID Google doesn't take - another may be right
            else:
                result = {'id': pid, 'ok': False, 'error': _google_message(r)}
                break
        if result is None:  # already gone is fine; only refused forms is an error
            result = {'id': pid, 'ok': gone or refused is None, 'error': None if gone else refused}
        results.append(result)
    return results


def _update_photos(creds, items):
    """Update position, heading, connections and place of already uploaded photos.

    items: [{id, lat, lng, heading|None, connections: [photo id, ...]?, placeId?, level?}]
    connections, placeId and level are only there when they should change ('' or None
    removes a place or floor). Without 'connections' the links on Google stay as they are.
    """
    requests, *_ = _google_libs()
    session = _session(creds)
    results = []
    for it in items:
        mask = ['pose.latLngPair']
        pose = {'latLngPair': {'latitude': it['lat'], 'longitude': it['lng']}}
        body = {'photoId': {'id': it['id']}, 'pose': pose}
        if 'connections' in it:
            mask.append('connections')
            body['connections'] = [{'target': {'id': t}} for t in it['connections']]
        if it.get('heading') is not None:
            pose['heading'] = it['heading']
            mask.append('pose.heading')
        if 'level' in it:  # only when the floor is to change (None removes it)
            mask.append('pose.level')
            if it['level']:
                pose['level'] = {'number': int(it['level']['number']), 'name': str(it['level'].get('name') or '')[:3]}
        if 'placeId' in it:
            mask.append('places')
            body['places'] = [{'placeId': it['placeId']}] if it['placeId'] else []
        result = {'id': it['id'], 'ok': True}
        for attempt in range(2):
            try:
                r = session.put(f'{BASE_URL}/photo/{it["id"]}', params={'updateMask': ','.join(mask)}, json=body)
            except requests.RequestException:
                result = {'id': it['id'], 'ok': False, 'status': 0, 'error': OFFLINE}
                break
            if r.ok:
                break
            result = {'id': it['id'], 'ok': False, 'status': r.status_code, 'error': _google_message(r)}
            if attempt or r.status_code != 400 or 'places' not in mask:
                break
            # A place Google won't take must not hold up the rest: again without it.
            mask.remove('places')
            body.pop('places')
            result = {'id': it['id'], 'ok': True, 'placeRejected': True}
        results.append(result)
        time.sleep(0.3)
    return results


# ── 360° videos (photo sequences) ─────────────────
# Google cuts a 360° video into photos itself, places them along the GPS track and
# connects them. Videos are often several GB, so they go up with Google's resumable
# protocol: the editor sends the video piece by piece through this app (never whole
# in memory), and after a broken connection - or a restart - asks Google how much
# arrived and goes on from there.

class _StreamReader:
    """`total` bytes from a stream (the editor's request), handed on piece by piece, with progress."""

    def __init__(self, stream, total, job):
        self.stream, self.total, self.job, self.sent = stream, total, job, 0

    def drain(self):
        """Read what is left, so the editor can be answered (it is still sending otherwise)."""
        while self.sent < self.total:
            chunk = self.stream.read(min(1024 * 1024, self.total - self.sent))
            if not chunk:
                break
            self.sent += len(chunk)

    def __len__(self):
        return self.total

    def read(self, size=-1):
        left = self.total - self.sent
        if left <= 0:
            return b''
        want = 1024 * 1024 if size is None or size < 0 else min(size, 1024 * 1024)
        chunk = self.stream.read(min(want, left))
        if not chunk:
            raise ConnectionError('The video stopped arriving from the editor.')
        self.sent += len(chunk)
        if self.job:
            UPLOAD_PROGRESS[self.job] = {'phase': 'uploading', 'sent': self.sent, 'total': self.total}
        return chunk


def _check_upload_url(url):
    """The sign-in goes along only to Google's own upload addresses (the editor hands them in)."""
    ours, theirs = urlparse(BASE_URL), urlparse(url or '')
    if (theirs.scheme, theirs.netloc) != (ours.scheme, ours.netloc):
        raise GoogleError('upload', 400, 'Not an upload address of the Street View Publish API.')


def _video_start(creds, size, content_type='video/mp4'):
    """Open a resumable upload: (upload URL - names the video when the sequence is made -,
    session URL - where its bytes go)."""
    session = _session(creds)
    r = _post(session, f'{BASE_URL}/photoSequence:startUpload', 'start', json={})
    if not r.ok:
        raise GoogleError('start', r.status_code, _google_message(r))
    upload_url = r.json()['uploadUrl']
    r = _post(session, upload_url, 'upload', data=b'', headers={
        'X-Goog-Upload-Protocol': 'resumable',
        'X-Goog-Upload-Command': 'start',
        'X-Goog-Upload-Header-Content-Length': str(size),
        'X-Goog-Upload-Header-Content-Type': content_type or 'video/mp4',
    })
    if not r.ok:
        raise GoogleError('upload', r.status_code, _google_message(r))
    session_url = r.headers.get('X-Goog-Upload-URL')
    if not session_url:
        raise GoogleError('upload', r.status_code, 'Google did not open a resumable upload.')
    return upload_url, session_url


def _video_chunk(creds, session_url, stream, length, offset, final, job=None):
    """Send `length` bytes of the video, which start at `offset`; `final`: the last piece.
    Not tried again here: the editor asks Google how much arrived and goes on from there."""
    _check_upload_url(session_url)
    reader = _StreamReader(stream, length, job)
    try:
        r = _session(creds, UPLOAD_TIMEOUT).post(session_url, data=reader, headers={
            'X-Goog-Upload-Command': 'upload, finalize' if final else 'upload',
            'X-Goog-Upload-Offset': str(offset),
        })
    finally:
        reader.drain()
    if not r.ok:
        raise GoogleError('upload', r.status_code, _google_message(r))
    return offset + length


def _video_query(creds, session_url):
    """How much of the video Google has: {'status': 'active' | 'final' | ..., 'received': bytes}."""
    _check_upload_url(session_url)
    r = _post(_session(creds), session_url, 'query', data=b'', headers={'X-Goog-Upload-Command': 'query'})
    if not r.ok:
        raise GoogleError('query', r.status_code, _google_message(r))
    return {'status': r.headers.get('X-Goog-Upload-Status') or 'active',
            'received': int(r.headers.get('X-Goog-Upload-Size-Received') or 0)}


def _create_sequence(creds, upload_url, gps=None, capture_time=None):
    """Have Google make photos of the uploaded video. gps: [{lat, lng, time (RFC 3339)}] from a GPX
    track - without it, Google takes the GPS from the video's camera motion track (CAMM)."""
    requests, *_ = _google_libs()
    body = {'uploadReference': {'uploadUrl': upload_url}}
    if gps:
        body['rawGpsTimeline'] = [{
            'latLngPair': {'latitude': p['lat'], 'longitude': p['lng']},
            'gpsRecordTimestampUnixEpoch': p['time'],
        } for p in gps]
        body['gpsSource'] = 'PHOTO_SEQUENCE'
    else:
        body['gpsSource'] = 'CAMERA_MOTION_METADATA_TRACK'
    if capture_time:
        body['captureTimeOverride'] = capture_time
    r = _post(_session(creds), f'{BASE_URL}/photoSequence', 'create', retry_status={429, 503},
              retry_errors=(requests.ConnectTimeout,), params={'inputType': 'VIDEO'}, json=body)
    if not r.ok:
        raise GoogleError('create', r.status_code, _google_message(r))
    op = r.json()
    return _sequence_id(op)


def _sequence_id(op):
    """The sequence's ID: Google puts it in the operation's name (sometimes as 'photoSequence/ID')."""
    seq = op.get('response') or op.get('metadata') or {}
    return seq.get('id') or str(op.get('name') or '').rsplit('/', 1)[-1] or None


def _sequence_facts(op):
    """One photo sequence as the editor shows it."""
    seq = {**(op.get('metadata') or {}), **(op.get('response') or {})}
    state = seq.get('processingState')
    if not state or state == 'PROCESSING_STATE_UNSPECIFIED':
        state = ('FAILED' if op.get('error') else 'PROCESSED') if op.get('done') else 'PROCESSING'
    return {
        'id': _sequence_id(op),
        'state': state,
        'failure': seq.get('failureReason') if state == 'FAILED' else None,
        'error': (op.get('error') or {}).get('message'),
        'filename': seq.get('filename'),
        'uploadTime': seq.get('uploadTime'),
        'captureTime': seq.get('captureTimeOverride'),
        'distance': seq.get('distanceMeters'),
        'photos': len(seq.get('photos') or []),
        'views': int(seq.get('viewCount') or 0),
    }


def _list_sequences(creds):
    """The signed-in user's videos on Google, newest first, each with its processing state."""
    session = _session(creds)
    ops, token = [], None
    for _ in range(20):
        params = {'pageSize': 100}
        if token:
            params['pageToken'] = token
        r = _get(session, f'{BASE_URL}/photoSequences', 'list', params=params)
        if not r.ok:
            raise GoogleError('list', r.status_code, _google_message(r))
        data = r.json()
        ops += data.get('photoSequences') or []
        token = data.get('nextPageToken')
        if not token:
            break
    # The list holds little more than the ID once a sequence is done: ask for each,
    # a few at a time.
    def details(op):
        try:
            r = session.get(f'{BASE_URL}/photoSequence/{_sequence_id(op)}', params={'view': 'BASIC'})
        except Exception:
            return _sequence_facts(op)
        return _sequence_facts(r.json() if r.ok else op)
    with ThreadPoolExecutor(max_workers=6) as pool:
        out = list(pool.map(details, [op for op in ops[:50] if _sequence_id(op)]))
    out.sort(key=lambda s: s.get('uploadTime') or '', reverse=True)
    return out


def _delete_sequence(creds, sid):
    requests, *_ = _google_libs()
    try:
        r = _session(creds).delete(f'{BASE_URL}/photoSequence/{sid}')
    except requests.RequestException as e:
        raise GoogleError('delete', 0, OFFLINE) from e
    if not r.ok and r.status_code != 404:
        raise GoogleError('delete', r.status_code, _google_message(r))
