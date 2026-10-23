"""Publishing to Google (faked): uploads, a refused Place ID, links, changes without uploading again."""
from helpers import KIEL, STEP_8M, FakeGoogle, b64, drop, jpg, run_upload


def four_linked(page):
    drop(page, [b64(f'p{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=90)) for k in range(4)])
    for i in range(4):
        page.click(f'#node-list .nrow >> nth={i}', modifiers=['Control'])
    page.click('#multibar-connect')
    assert page.inner_text('#st').startswith('Nodes 4 · Links 3')


def test_upload_with_a_place_id_google_refuses(editor):
    page = editor.page
    google = FakeGoogle(page, reject_place=lambda pid: True)
    four_linked(page)
    page.click('#bupload')
    page.wait_for_timeout(800)
    assert page.inner_text('#upc-new') == '4'
    page.fill('#upl-place', 'https://www.google.com/maps/place/?q=place_id:ChIJWRONGWRONGWRONG123')
    page.wait_for_timeout(200)
    assert page.input_value('#upl-place') == 'ChIJWRONGWRONGWRONG123'
    run_upload(page)
    # tried with the place once, then without it for this and every other photo
    assert google.uploads[:2] == [('p0.jpg', 'ChIJWRONGWRONGWRONG123'), ('p0.jpg', None)]
    assert [u[1] for u in google.uploads[2:]] == [None, None, None]
    assert 'did not accept the Place ID' in page.inner_text('#up-notice')
    links = {i['id']: sorted(i['connections']) for batch in google.updates for i in batch}
    assert links == {'g0': ['g1'], 'g1': ['g0', 'g2'], 'g2': ['g1', 'g3'], 'g3': ['g2']}
    page.click('#up-finish')
    assert page.locator('.leaflet-marker-icon .gs').count() == 4


def test_a_moved_point_is_updated_without_uploading_again(editor):
    page = editor.page
    google = FakeGoogle(page)
    four_linked(page)
    page.click('#bupload')
    run_upload(page)
    page.click('#up-finish')
    page.click('#upl-close')
    google.updates.clear()
    uploads = len(google.uploads)

    page.click('#node-list .nrow >> nth=1')
    page.click('details[data-sec=position] summary')
    page.fill('#ni-lat', str(KIEL[0] + 0.0001))
    page.keyboard.press('Enter')
    page.click('#bupload')
    page.wait_for_timeout(800)
    assert page.inner_text('#upc-changed') == '1'
    run_upload(page)
    assert len(google.uploads) == uploads
    moved = [i for batch in google.updates for i in batch if i['id'] == 'g1']
    assert moved and abs(moved[-1]['lat'] - (KIEL[0] + 0.0001)) < 1e-9
