"""Photos from Google into the editor (and their changes back), renaming points, KML and GPX,
sharing the tour with a link, QR code and embed code."""
import json
import xml.etree.ElementTree as ET

from helpers import KIEL, STEP_8M, FakeGoogle, jpg, run_upload

SHOWN = "id => document.getElementById(id).classList.contains('show')"
NAMES = "[...document.querySelectorAll('#node-list .nrow-name')].map(e => e.textContent)"
PHOTOS = [
    {'id': 'g-a', 'status': 'published', 'shareLink': 'https://maps.app.goo.gl/aaa', 'uploadTime': '2026-03-01T10:00:00Z',
     'lat': KIEL[0], 'lng': KIEL[1], 'heading': 90.0, 'level': None, 'connections': ['g-b'], 'viewCount': 12,
     'placeId': 'ChIJplace', 'placeName': 'Holstenplatz'},
    {'id': 'g-b', 'status': 'published', 'shareLink': 'https://maps.app.goo.gl/bbb', 'uploadTime': '2026-03-01T10:01:00Z',
     'lat': KIEL[0], 'lng': KIEL[1] + STEP_8M, 'heading': 90.0, 'level': {'number': 1, 'name': '1'}, 'connections': ['g-a', 'g-c'],
     'viewCount': 3},
    {'id': 'g-c', 'status': 'processing', 'uploadTime': '2026-03-01T10:02:00Z',
     'lat': KIEL[0], 'lng': KIEL[1] + 2 * STEP_8M, 'heading': None, 'connections': ['g-b']},
    {'id': 'g-far', 'status': 'published', 'uploadTime': '2025-01-01T10:00:00Z', 'lat': 48.1, 'lng': 11.5, 'heading': 0.0, 'connections': []},
]


class Google(FakeGoogle):
    def __init__(self, page):
        super().__init__(page)
        self.downloads = []
        page.route('**/api/google-photo', self._photo)

    def listed(self):
        return PHOTOS

    def _photo(self, route):
        pid = json.loads(route.request.post_data)['id']
        self.downloads.append(pid)
        if pid == 'g-c':  # Google offers no download for this one
            return route.fulfill(json={'ok': False, 'error': 'Google offers no download of this photo.'})
        route.fulfill(body=jpg(lat=KIEL[0], lng=KIEL[1]), content_type='image/jpeg')


def load_from_google(page, everything=True):
    page.click('#btools')
    page.click('button[data-run=fromgoogle]')
    page.wait_for_function("!document.getElementById('fg-go').disabled || document.getElementById('fg-sum').textContent.includes('not in this project')")
    if everything:
        page.check('#fg-all')
    page.click('#fg-go')
    page.wait_for_function("document.getElementById('fg-status').textContent.includes('loaded from Google')", timeout=60000)


def test_photos_from_google_and_their_changes_back(editor):
    page = editor.page
    google = Google(page)
    load_from_google(page)
    assert '4 photo(s) loaded from Google.' in page.inner_text('#fg-status')
    assert '1 of them without the photo itself' in page.inner_text('#fg-status')
    page.keyboard.press('Escape')
    assert page.evaluate(NAMES) == ['Google photo 1', 'Google photo 2', 'Google photo 3', 'Google photo 4']
    assert page.inner_text('#st').startswith('Nodes 4 · Links 2')  # the links as Google has them
    assert sorted(google.downloads) == ['g-a', 'g-b', 'g-c', 'g-far']

    # Google's direction, floor and place came along; nothing counts as changed
    page.click('#node-list .nrow >> nth=1')  # g-a (the list is in upload order: g-far came first)
    assert page.input_value('#ni-h') == '90.0'
    assert page.inner_text('#heading-meta') == 'as on Google'
    page.click('#bupload')
    page.wait_for_timeout(500)
    assert page.inner_text('#upc-new') == '0' and page.inner_text('#upc-changed') == '0'
    assert page.is_disabled('#upl-start')
    page.click('#upl-close')

    # turned by hand - only that change goes to Google, nothing is uploaded
    page.fill('#ni-h', '120')
    page.press('#ni-h', 'Enter')
    page.click('#bupload')
    page.wait_for_timeout(500)
    assert page.inner_text('#upc-changed') == '1'
    run_upload(page)
    assert google.uploads == []
    (items,) = google.updates
    assert [(i['id'], i['heading'], sorted(i['connections'])) for i in items] == [('g-a', 120.0, ['g-b'])]
    assert items[0]['placeId'] == 'ChIJplace'


def test_only_the_photos_in_view_and_none_twice(editor):
    page = editor.page
    Google(page)
    load_from_google(page)  # all four
    page.keyboard.press('Escape')
    page.click('#btools')
    page.click('button[data-run=fromgoogle]')
    page.wait_for_function("document.getElementById('fg-sum').textContent.length > 0 && !document.getElementById('fg-sum').textContent.startsWith('Asking')")
    assert page.inner_text('#fg-sum') == 'All your photos on Google are in this project already.'
    assert page.is_disabled('#fg-go')


def test_rename_points(editor):
    page = editor.page
    Google(page)
    load_from_google(page)
    page.keyboard.press('Escape')
    page.click('#btools')
    page.click('button[data-run=rename]')
    page.wait_for_function(SHOWN, arg='rename-back')
    page.click('[data-rn-pattern="{tour} {n}"]')
    preview = page.evaluate("[...document.querySelectorAll('#rn-preview .rn-row b')].map(e => e.textContent)")
    assert preview == ['Photos from Google 1', 'Photos from Google 2', 'Photos from Google 3', 'Photos from Google 4'], preview
    page.fill('#rn-pattern', '{place}')  # names from places; the same name twice gets a number
    page.click('#rn-go')
    assert page.evaluate(NAMES) == ['Google photo 1', 'Holstenplatz', 'Google photo 3', 'Google photo 4']  # no place: the name stays
    page.keyboard.press('Control+z')
    assert page.evaluate(NAMES) == ['Google photo 1', 'Google photo 2', 'Google photo 3', 'Google photo 4']


def test_kml_gpx_and_share(editor):
    page = editor.page
    Google(page)
    load_from_google(page)
    page.keyboard.press('Escape')
    with page.expect_download() as dl:
        page.click('#bproject')
        page.click('button[data-run=kml]')
        if page.evaluate("document.getElementById('exp-back').classList.contains('show')"):
            page.click('#exp-go')
    kml = ET.parse(dl.value.path()).getroot()
    ns = {'k': 'http://www.opengis.net/kml/2.2'}
    marks = kml.findall('.//k:Placemark', ns)
    assert len([m for m in marks if m.find('k:Point', ns) is not None]) == 4
    assert len([m for m in marks if m.find('k:LineString', ns) is not None]) == 2
    assert 'https://maps.app.goo.gl/aaa' in [m.findtext('k:description', '', ns) for m in marks]

    with page.expect_download() as dl:
        page.click('#bproject')
        page.click('button[data-run=gpx]')
        if page.evaluate("document.getElementById('exp-back').classList.contains('show')"):
            page.click('#exp-go')
    gpx = ET.parse(dl.value.path()).getroot()
    ns = {'g': 'http://www.topografix.com/GPX/1/1'}
    assert len(gpx.findall('g:wpt', ns)) == 4
    assert [len(seg.findall('g:trkpt', ns)) for seg in gpx.findall('.//g:trkseg', ns)] == [3]

    page.click('#bproject')
    page.click('button[data-run=share]')
    page.wait_for_function(SHOWN, arg='share-back')
    assert page.input_value('#share-maps-link').startswith('https://maps.app.goo.gl/')
    assert page.evaluate("document.querySelectorAll('#share-maps-qr svg path').length") == 1
    page.fill('#share-site-url', 'https://example.org/tour/')
    embed = page.input_value('#share-site-embed')
    assert embed.startswith('<iframe src="https://example.org/tour/"') and 'allowfullscreen' in embed
    with page.expect_download() as dl:
        page.click('[data-qr="site:png"]')
    assert open(dl.value.path(), 'rb').read(8) == b'\x89PNG\r\n\x1a\n'
