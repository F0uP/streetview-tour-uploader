"""Street View Publish API: upload photos, link them, read them back, update and delete them.

Every call takes the signed-in user's credentials (see run_editor.py for the
sign-in). The Google libraries are imported on demand, so plain editing never
needs them installed.
"""

import base64
import io
import os
import time

# SVT_STREETVIEW_API: the tests point the app at a fake Google with it.
BASE_URL = os.environ.get('SVT_STREETVIEW_API') or 'https://streetviewpublish.googleapis.com/v1'


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


def _upload_photo(creds, data, filename, lat, lng, heading=None, place_id=None, job=None, level=None):
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
    if level:  # the floor, inside buildings: {'number': 1, 'name': '1st floor'}
        pose['level'] = level
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
    """Update position, heading, connections and place of already uploaded photos.

    items: [{id, lat, lng, heading|None, connections: [photo id, ...], placeId?}]
    placeId is only there when the place should change ('' or None removes it).
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
        if 'level' in it:  # only when the floor is to change (None removes it)
            mask.append('pose.level')
            if it['level']:
                pose['level'] = {'number': int(it['level']['number']), 'name': str(it['level'].get('name') or '')[:3]}
        body = {
            'photoId': {'id': it['id']},
            'pose': pose,
            'connections': [{'target': {'id': t}} for t in it.get('connections', [])],
        }
        if 'placeId' in it:
            mask.append('places')
            body['places'] = [{'placeId': it['placeId']}] if it['placeId'] else []
        result = {'id': it['id'], 'ok': True}
        for attempt in range(2):
            try:
                sv_service.photo().update(id=it['id'], updateMask=','.join(mask), body=body).execute()
                break
            except Exception as e:
                # googleapiclient's HttpError carries Google's own message in .reason
                status = getattr(getattr(e, 'resp', None), 'status', None)
                result = {'id': it['id'], 'ok': False, 'status': status, 'error': getattr(e, 'reason', None) or str(e)}
                if attempt or status != 400 or 'places' not in mask:
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
# connects them. The video goes from the editor through this app to Google as a
# stream, never whole in memory (videos are often several GB).

class _StreamReader:
    """`total` bytes from a stream (the editor's request), handed on piece by piece, with progress."""

    def __init__(self, stream, total, job):
        self.stream, self.total, self.job, self.sent = stream, total, job, 0

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


def _session(creds):
    requests, *_ = _google_libs()
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {creds.token}'})
    return session


def _upload_video(creds, stream, total, content_type='video/mp4', job=None):
    """Send the video bytes to Google; returns the upload URL that _create_sequence refers to."""
    session = _session(creds)
    if job:
        UPLOAD_PROGRESS[job] = {'phase': 'starting', 'sent': 0, 'total': total}
    r = session.post(f'{BASE_URL}/photoSequence:startUpload', json={})
    if not r.ok:
        raise GoogleError('start', r.status_code, _google_message(r))
    upload_url = r.json()['uploadUrl']
    r = session.post(upload_url, data=_StreamReader(stream, total, job), headers={
        'Content-Type': content_type or 'video/mp4',
        'X-Goog-Upload-Protocol': 'raw',
        'X-Goog-Upload-Content-Length': str(total),
    })
    if not r.ok:
        raise GoogleError('upload', r.status_code, _google_message(r))
    return upload_url


def _create_sequence(creds, upload_url, gps=None, capture_time=None):
    """Have Google make photos of the uploaded video. gps: [{lat, lng, time (RFC 3339)}] from a GPX
    track - without it, Google takes the GPS from the video's camera motion track (CAMM)."""
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
    r = _session(creds).post(f'{BASE_URL}/photoSequence', params={'inputType': 'VIDEO'}, json=body)
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
        r = session.get(f'{BASE_URL}/photoSequences', params=params)
        if not r.ok:
            raise GoogleError('list', r.status_code, _google_message(r))
        data = r.json()
        ops += data.get('photoSequences') or []
        token = data.get('nextPageToken')
        if not token:
            break
    out = []
    for op in ops[:50]:
        sid = _sequence_id(op)
        if not sid:
            continue
        # The list holds little more than the ID once a sequence is done: ask for each.
        r = session.get(f'{BASE_URL}/photoSequence/{sid}', params={'view': 'BASIC'})
        out.append(_sequence_facts(r.json() if r.ok else op))
    out.sort(key=lambda s: s.get('uploadTime') or '', reverse=True)
    return out


def _delete_sequence(creds, sid):
    r = _session(creds).delete(f'{BASE_URL}/photoSequence/{sid}')
    if not r.ok and r.status_code != 404:
        raise GoogleError('delete', r.status_code, _google_message(r))
