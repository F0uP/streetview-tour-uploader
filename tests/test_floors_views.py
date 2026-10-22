"""Floors inside buildings, and how often the photos were viewed on Google."""
from helpers import KIEL, STEP_8M, FakeGoogle, b64, drop, jpg, run_upload

DIMMED = "[...document.querySelectorAll('.leaflet-marker-icon .nd')].map(e => e.classList.contains('dim'))"


def test_floors_are_set_filtered_and_published(editor):
    page = editor.page
    google = FakeGoogle(page)
    drop(page, [b64(f'f{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=90)) for k in range(3)])
    assert not page.is_visible('.floor-filter')

    # the first photo is on the first floor; the other two, picked together, on the ground floor
    page.click('#node-list .nrow >> nth=0')
    page.click('details[data-sec=position] summary')
    page.fill('#ni-level', '1')
    page.keyboard.press('Tab')
    page.fill('#ni-level-name', '1.OG')  # Google allows 3 characters
    page.keyboard.press('Tab')
    page.keyboard.press('Escape')
    page.click('#node-list .nrow >> nth=1', modifiers=['Control'])
    page.click('#node-list .nrow >> nth=2', modifiers=['Control'])
    page.select_option('#multibar-level', '0')
    page.keyboard.press('Escape')

    assert page.locator('.floor-filter button').all_inner_texts() == ['All', '1.O', '0']
    page.click('.floor-filter button[data-floor="1"]')
    assert page.evaluate(DIMMED) == [False, True, True]
    page.click('.floor-filter button[data-floor="all"]')
    assert page.evaluate(DIMMED) == [False, False, False]

    page.click('#bupload')
    run_upload(page)
    levels = {q['filename']: (q.get('level'), q.get('level_name')) for q in google.queries}
    assert levels == {'f0.jpg': ('1', '1.O'), 'f1.jpg': ('0', ''), 'f2.jpg': ('0', '')}
    page.click('#up-finish')
    page.click('#upl-close')

    # the floor removed later: pushed to Google without uploading again
    page.click('#node-list .nrow >> nth=0')
    page.fill('#ni-level', '')
    page.keyboard.press('Tab')
    google.updates.clear()
    page.click('#bupload')
    page.wait_for_timeout(800)
    assert page.inner_text('#upc-changed') == '1'
    run_upload(page)
    pushed = [i for batch in google.updates for i in batch if i['id'] == 'g0']
    assert pushed and pushed[-1]['level'] is None


def test_views_per_tour_and_photo(editor):
    page = editor.page
    google = FakeGoogle(page)
    drop(page, [b64(f'v{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M)) for k in range(3)])
    page.click('#bupload')
    run_upload(page)
    page.click('#up-finish')
    page.click('#upl-close')
    google.views = {'g0': 1200, 'g1': 34, 'g2': 5}
    page.click('#btools')
    page.click('button[data-run=views]')
    page.wait_for_timeout(1500)
    assert page.inner_text('#views-sum') == '1,239 views in total.'
    rows = page.locator('#views-list .check-row .msg').all_inner_texts()
    assert rows == ['1,200 views', '34 views', '5 views']
    page.click('#views-list .check-row >> nth=0 >> button')
    assert '1,200 views' in page.inner_text('#ph-n')
