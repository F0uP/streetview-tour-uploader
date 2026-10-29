"""Places on Google Maps (Places and Geocoding APIs faked): the rule, shown and changeable, per photo."""
import json

from helpers import FakeGoogle, drop, file_item, run_upload, vrtour

TOURS = """[...document.querySelectorAll('#upl-place-rows .place-tour')].map(r => ({
  tour: r.querySelector('.pn')?.textContent || '', mode: r.querySelector('.place-mode').value,
  one: r.querySelector('.place-one')?.value, why: r.querySelector('.why').textContent }))"""
PHOTOS = """(i) => [...document.querySelectorAll('#upl-place-rows .place-tour')[i].querySelectorAll('.place-photo')].map(r =>
  r.querySelector('select').selectedOptions[0].textContent)"""


class FakeMaps:
    def __init__(self, page):
        self.calls = []
        self.quota_out = False
        self.configured = True
        page.route('**/api/places', lambda r: r.fulfill(json={'ok': True, 'configured': self.configured}))
        page.route('**/api/places/nearby', self.nearby)
        page.route('**/api/places/geocode', self.geocode)
        page.route('**/api/places/key', lambda r: r.fulfill(json={'ok': True, 'configured': True, 'places': True, 'geocoding': False,
                                                              'errors': {'geocoding': 'This API project is not authorized to use this API.'}}))

    def nearby(self, route):
        body = json.loads(route.request.post_data)
        self.calls.append(('nearby', round(body['lat'], 4)))
        lat, lng = body['lat'], body['lng']
        if lat < 54.325:  # the museum
            places = [{'id': 'ChIJmuseum0000000000', 'name': 'Stadtmuseum', 'type': 'Museum', 'lat': lat + 0.0001, 'lng': lng},
                      {'id': 'ChIJcafe00000000000000', 'name': 'Café am Markt', 'type': 'Café', 'lat': lat, 'lng': lng + 0.0009}]
        else:
            places = [{'id': 'BADkiosk00000000000000', 'name': 'Kiosk', 'type': 'Kiosk', 'lat': lat + 0.0006, 'lng': lng}]
        route.fulfill(json={'ok': True, 'places': places, 'fetched': 1})

    def geocode(self, route):
        pts = json.loads(route.request.post_data)['points']
        self.calls.append(('geocode', len(pts)))
        if self.quota_out:
            return route.fulfill(json={'ok': False, 'error': 'You have exceeded your daily request quota for this API.'})
        out = []
        for i, p in enumerate(pts):
            off = 0.0006 if p['lng'] > 10.143 else 0.0001  # the walk's last photo is far from any house
            out.append({'address': {'id': f'ChIJaddr{p["lng"]:.4f}', 'name': f'Allee {12 + i * 4}', 'lat': p['lat'] + off, 'lng': p['lng']},
                        'street': {'id': 'ChIJallee000000000000', 'name': 'Allee', 'lat': p['lat'], 'lng': p['lng']}})
        route.fulfill(json={'ok': True, 'results': out, 'fetched': len(pts)})


def two_tours(page, tmp_path):
    drop(page, [file_item(vrtour(tmp_path / 'Museum.vrtour', 'Museum', 54.3233, 10.1228, step=0.0001))], confirm=None)
    drop(page, [file_item(vrtour(tmp_path / 'Allee.vrtour', 'Allee', 54.3300, 10.1400, step=0.002))], confirm=None)
    page.click('#choice-buttons .btn.g')
    page.wait_for_timeout(1200)


def open_upload(page):
    page.click('#bupload')
    page.wait_for_function("document.querySelectorAll('#upl-place-rows .place-mode:not([disabled])').length > 0")
    page.wait_for_timeout(500)


def test_places_per_tour_and_per_photo(make_editor, tmp_path):
    fakes = {}

    def routes(ed):  # before the page loads: it asks about the key right away
        fakes['maps'] = FakeMaps(ed.page)
        fakes['google'] = FakeGoogle(ed.page, reject_place=lambda pid: pid.startswith('BAD'))

    editor = make_editor(routes=routes)
    page, maps, google = editor.page, fakes['maps'], fakes['google']
    two_tours(page, tmp_path)
    page.click('#node-list .nrow >> nth=3')
    assert page.inner_text('#place-line') == 'Place on Google Maps: looked up when you open the upload'

    # the daily limit is reached: the walk stays undecided, and the dialog says so
    maps.quota_out = True
    open_upload(page)
    museum, walk = page.evaluate(TOURS)
    assert museum['mode'] == 'auto' and 'Stadtmuseum less than 40 m away' in museum['why'] and '3 at a building or business' in museum['why']
    assert 'spreads over 259 m' in walk['why'] and '3 with the position only' in walk['why']
    assert 'exceeded your daily request quota' in page.inner_text('#upl-place-rows')
    page.click('#upl-close')

    # next time it works: a house number, or the street
    maps.quota_out = False
    maps.calls.clear()
    open_upload(page)
    assert '2 at a house number, 1 at a street' in page.evaluate(TOURS)[1]['why']
    assert maps.calls == [('geocode', 3)]
    assert page.inner_text('#upl-place-cost') == 'Sent to Google for this: 0 place search(es), 3 address lookup(s).'
    page.click('#upl-close')
    maps.calls.clear()
    open_upload(page)
    assert maps.calls == [] and page.inner_text('#upl-place-cost') == 'No lookups at Google were needed for this.'

    page.click('#upl-place-rows .place-tour >> nth=1 >> summary')
    page.wait_for_timeout(500)
    assert page.evaluate(PHOTOS, 1) == ['Allee 12 · house number · 11 m', 'Allee 16 · house number · 11 m', 'Allee · street']

    # one photo of the walk at the kiosk - which Google refuses
    page.select_option('#upl-place-rows .place-tour >> nth=1 >> .place-photo >> nth=0 >> select', 'BADkiosk00000000000000')
    assert page.evaluate(TOURS)[1]['mode'] == 'photo'
    assert page.inner_text('#place-line') == 'Place on Google Maps: Kiosk · building or business · 146 m (chosen)'

    run_upload(page)
    # a few at a time: the order they arrive in may vary
    assert sorted(google.uploads, key=str) == sorted([
        ('Museum1.jpg', 'ChIJmuseum0000000000'), ('Museum2.jpg', 'ChIJmuseum0000000000'), ('Museum3.jpg', 'ChIJmuseum0000000000'),
        ('Allee1.jpg', 'BADkiosk00000000000000'), ('Allee1.jpg', None), ('Allee2.jpg', 'ChIJaddr10.1420'),
        ('Allee3.jpg', 'ChIJallee000000000000')], key=str)
    assert google.uploads.index(('Allee1.jpg', 'BADkiosk00000000000000')) < google.uploads.index(('Allee1.jpg', None))
    assert 'did not accept the place "Kiosk"' in page.inner_text('#up-notice')
    assert page.inner_text('#up-list .up-row >> nth=3 >> .up-pl') == '→ position only - Google did not take the place'
    page.click('#up-finish')

    # no place for the museum after all: sent without uploading again
    page.select_option('#upl-place-rows .place-tour >> nth=0 >> .place-mode', 'none')
    assert page.inner_text('#upc-changed') == '3'
    google.updates.clear()
    run_upload(page)
    assert sorted({(i['id'], i.get('placeId', '-')) for batch in google.updates for i in batch}) == [('g0', None), ('g1', None), ('g2', None)]
    page.keyboard.press('Escape')

    # one place for the whole walk: its street is offered
    open_upload(page)
    page.select_option('#upl-place-rows .place-tour >> nth=1 >> .place-mode', 'tour')
    page.wait_for_timeout(600)
    options = page.locator('#upl-place-rows .place-tour >> nth=1 >> .place-one option').all_inner_texts()
    assert options[0] == 'Allee · street' and 'No place - position only' in options
    page.select_option('#upl-place-rows .place-tour >> nth=1 >> .place-one', 'ChIJallee000000000000')
    assert '3 at a street' in page.evaluate(TOURS)[1]['why']
    page.keyboard.press('Escape')

    # the setup says which API works
    page.click('#btools')
    page.click('button[data-run=places]')
    page.fill('#places-key', 'AIzaGOODKEY_0123456789abcdefghijklm')
    page.click('#places-save')
    page.wait_for_timeout(400)
    status = page.inner_text('#places-status')
    assert 'Places API: works' in status and 'Geocoding API: not working' in status


def test_without_a_key_the_place_id_can_be_typed(editor):
    page = editor.page
    FakeGoogle(page)
    page.click('#bupload')
    page.wait_for_timeout(800)
    assert page.is_visible('#upl-place')
    assert page.inner_text('.place-tip').startswith('Recommended:')
