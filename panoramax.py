"""Panoramax: the open, shared street-level photo platform (panoramax.fr) - a second
place to publish a tour besides Google Street View.

Photos go up in an "upload set": created, filled file by file, then completed; the
instance groups them into sequences and blurs faces and plates itself. Signing in
works without a password in the app: the app asks the instance for a new token,
the user claims it once in the browser (logged in there), and the app keeps it in
this file - the page never sees it, as with the Places API key.
"""

import json
from pathlib import Path
from urllib.parse import urlparse

from google_api import GoogleError, _google_libs

# Instances anyone can upload to (an account there is enough); any other one can be typed in.
INSTANCES = ['https://panoramax.ign.fr', 'https://panoramax.openstreetmap.fr']


def _url(instance):
    """The instance's address, checked: https - or this computer, for tests."""
    u = urlparse((instance or '').strip().rstrip('/'))
    local = u.hostname in ('127.0.0.1', 'localhost')
    if u.scheme not in ('https', 'http') or (u.scheme == 'http' and not local) or not u.netloc or u.path not in ('', '/'):
        raise GoogleError('panoramax', 400, 'Not the address of a Panoramax instance (https://…).')
    return f'{u.scheme}://{u.netloc}'


def _message(r):
    try:
        data = r.json()
        return data.get('message') or data.get('error') or json.dumps(data)[:300]
    except ValueError:
        return r.text[:300] or r.reason


class Panoramax:
    """The connection to one instance; config: {instance, token, user} in a file of the app's profile."""

    def __init__(self, config_file):
        self.config_file = Path(config_file)
        self.pending = None  # a token waiting to be claimed in the browser: {instance, token}

    def config(self):
        try:
            data = json.loads(self.config_file.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) and data.get('token') else None
        except (OSError, ValueError):
            return None

    def info(self):
        c = self.config()
        return {'configured': bool(c), 'instance': c and c['instance'], 'user': c and c.get('user'), 'instances': INSTANCES}

    def forget(self):
        self.config_file.unlink(missing_ok=True)
        self.pending = None

    def _request(self, method, url, token=None, step='panoramax', timeout=(15, 120), **kwargs):
        requests, *_ = _google_libs()
        headers = kwargs.pop('headers', {})
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            return requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException:
            raise GoogleError(step, 0, 'Panoramax could not be reached - check the address and the internet connection.') from None

    def start_sign_in(self, instance):
        """A new token from the instance: returns the address where the user claims it."""
        base = _url(instance)
        r = self._request('POST', f'{base}/api/auth/tokens/generate', step='sign-in')
        if not r.ok:
            raise GoogleError('sign-in', r.status_code, _message(r))
        data = r.json()
        claim = next((link.get('href') for link in data.get('links') or [] if link.get('rel') == 'claim'), None)
        if not data.get('jwt_token') or not claim:
            raise GoogleError('sign-in', 502, 'This instance does not hand out tokens.')
        self.pending = {'instance': base, 'token': data['jwt_token']}
        return claim

    def _user(self, base, token):
        r = self._request('GET', f'{base}/api/users/me', token, step='sign-in')
        if r.status_code in (401, 403):
            return None
        if not r.ok:
            raise GoogleError('sign-in', r.status_code, _message(r))
        return r.json().get('name') or ''

    def finish_sign_in(self):
        """Has the user claimed the token yet? Then it is kept."""
        if not self.pending:
            return False
        user = self._user(self.pending['instance'], self.pending['token'])
        if user is None:
            return False
        self._save({**self.pending, 'user': user})
        self.pending = None
        return True

    def use_token(self, instance, token):
        """A token the user copied from the instance's own settings page."""
        base, token = _url(instance), (token or '').strip()
        user = self._user(base, token) if token else None
        if user is None:
            raise GoogleError('sign-in', 401, 'The instance does not accept this token.')
        self._save({'instance': base, 'token': token, 'user': user})

    def _save(self, config):
        self.config_file.write_text(json.dumps(config), encoding='utf-8')

    def _signed(self):
        c = self.config()
        if not c:
            raise RuntimeError('not_signed_in')
        return c['instance'], c['token']

    def _json(self, r, step):
        if r.status_code == 401:
            raise RuntimeError('not_signed_in')
        if not r.ok:
            raise GoogleError(step, r.status_code, _message(r))
        return r.json() if r.content else {}

    def create_set(self, title, count):
        base, token = self._signed()
        # One sequence per tour, sorted by capture time; the instance's own duplicate check stays on.
        r = self._request('POST', f'{base}/api/upload_sets', token, step='start', json={
            'title': (title or 'Tour')[:200], 'estimated_nb_files': int(count), 'no_split': True, 'sort_method': 'time-asc',
        })
        return self._json(r, 'start')['id']

    def add_file(self, set_id, name, data, blurred):
        base, token = self._signed()
        r = self._request('POST', f'{base}/api/upload_sets/{set_id}/files', token, step='upload', timeout=(15, 300),
                          files={'file': (name, data, 'image/jpeg')}, data={'isBlurred': 'true' if blurred else 'false'})
        if r.status_code == 409:  # sent before: Panoramax already has it
            return {'duplicate': True}
        out = self._json(r, 'upload')
        rejected = out.get('rejected') or None
        return {'pictureId': out.get('picture_id'), 'rejected': rejected and (rejected.get('message') or rejected.get('reason'))}

    def complete(self, set_id):
        base, token = self._signed()
        return self._json(self._request('POST', f'{base}/api/upload_sets/{set_id}/complete', token, step='complete'), 'complete')

    def status(self, set_id):
        base, token = self._signed()
        data = self._json(self._request('GET', f'{base}/api/upload_sets/{set_id}', token, step='status'), 'status')
        st = data.get('items_status') or {}
        return {
            'ready': bool(data.get('ready')),
            'prepared': st.get('prepared') or 0,
            'preparing': (st.get('preparing') or 0) + (st.get('not_processed') or 0),
            'rejected': (st.get('rejected') or 0) + (st.get('broken') or 0),
            'sequences': len(data.get('associated_collections') or []),
            'instance': base,
        }
