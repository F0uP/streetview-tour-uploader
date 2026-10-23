"""Google Maps lookups for tying photos to places: buildings and businesses
(Places API, Nearby Search) and house numbers and streets (Geocoding API).

Optional, recommended. Both need an API key of the user's own (and a billing
account; the monthly free usage covers far more than the app needs). The key is
kept by the app (run_editor.py) and handed in here; results are cached for the run.
"""

from google_api import GoogleError, _google_libs, _google_message

PLACES_URL = "https://places.googleapis.com/v1/places:searchNearby"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
ADDRESS_TYPES = {'street_address', 'premise', 'subpremise'}
PLACES_CACHE = {}


def _request(step, method, url, **kwargs):
    """A request to Google Maps. Without a connection, the error must not name the URL:
    for the Geocoding API it carries the API key (requests puts it in its messages)."""
    requests, *_ = _google_libs()
    try:
        return requests.request(method, url, timeout=20, **kwargs)
    except requests.RequestException as e:
        reason = 'timed out' if isinstance(e, requests.Timeout) else 'no connection'
        raise GoogleError(step, 0, f'Google Maps could not be reached ({reason}).') from None


def _geocode(key, lat, lng, lang='en'):
    """The house number and the street at a spot: {'address': {...} | None, 'street': {...} | None}."""
    cache_key = ('geo', key, round(lat, 5), round(lng, 5), lang)
    if cache_key in PLACES_CACHE:
        return PLACES_CACHE[cache_key]
    r = _request('geocode', 'GET', GEOCODE_URL, params={
        'latlng': f'{lat},{lng}', 'key': key, 'language': lang,
        'result_type': 'street_address|premise|subpremise|route',
    })
    try:
        data = r.json()
    except ValueError:
        data = {}
    status = data.get('status')
    if status not in ('OK', 'ZERO_RESULTS'):
        raise GoogleError('geocode', r.status_code, data.get('error_message') or status or r.reason)
    out = {'address': None, 'street': None}
    for res in data.get('results', []):
        types = set(res.get('types', []))
        loc = (res.get('geometry') or {}).get('location') or {}
        full = res.get('formatted_address') or ''
        entry = {'id': res.get('place_id'), 'address': full, 'lat': loc.get('lat'), 'lng': loc.get('lng')}
        if not entry['id']:
            continue
        if not out['address'] and types & ADDRESS_TYPES:
            out['address'] = {**entry, 'name': full.split(',')[0]}
        if not out['street'] and 'route' in types:
            part = next((c for c in res.get('address_components', []) if 'route' in c.get('types', [])), {})
            out['street'] = {**entry, 'name': part.get('long_name') or full.split(',')[0]}
    PLACES_CACHE[cache_key] = out
    return out


def _places_nearby(key, lat, lng, radius=50.0, lang='en'):
    """Places around a spot, closest first: [{id, name, address, type, lat, lng}]."""
    cache_key = (key, round(lat, 5), round(lng, 5), round(radius), lang)
    if cache_key in PLACES_CACHE:
        return PLACES_CACHE[cache_key]
    r = _request('places', 'POST', PLACES_URL, headers={
        'X-Goog-Api-Key': key,
        'X-Goog-FieldMask': 'places.id,places.displayName,places.formattedAddress,places.location,places.primaryTypeDisplayName',
    }, json={
        'locationRestriction': {'circle': {'center': {'latitude': lat, 'longitude': lng}, 'radius': radius}},
        'maxResultCount': 10, 'rankPreference': 'DISTANCE', 'languageCode': lang,
    })
    if not r.ok:
        raise GoogleError('places', r.status_code, _google_message(r))
    places = [{
        'id': p['id'],
        'name': (p.get('displayName') or {}).get('text') or p.get('formattedAddress') or p['id'],
        'address': p.get('formattedAddress'),
        'type': (p.get('primaryTypeDisplayName') or {}).get('text'),
        'lat': (p.get('location') or {}).get('latitude'),
        'lng': (p.get('location') or {}).get('longitude'),
    } for p in r.json().get('places', []) if p.get('id')]
    PLACES_CACHE[cache_key] = places
    return places
