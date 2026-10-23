"""Fixes and smaller additions: the review never skips the tour check, the Places key never shows up
in a message, the checklist (processing, tour progress), the bell, dialogs for the keyboard, the
focus mode's keys and map, a video dropped on the map."""
import sys

import pytest

from conftest import REPO
from helpers import KIEL, STEP_8M, FakeGoogle, b64, drop, jpg, mp4

sys.path.insert(0, str(REPO))
import google_api  # noqa: E402
import places  # noqa: E402

SHOWN = "id => document.getElementById(id).classList.contains('show')"
CHIPS = "[...document.querySelectorAll('#checklist-chips .st-chip')].map(c => c.className.split(' ')[1] + ': ' + c.textContent)"


def linked(page, n=3, heading=90, size=(2048, 1024), name='s', north=0.0):
    """n photos in a row going east, linked; `north`: that many degrees of latitude further north."""
    before = page.evaluate("document.querySelectorAll('#node-list .nrow').length")
    drop(page, [b64(f'{name}{k}.jpg', jpg(*size, lat=KIEL[0] + north, lng=KIEL[1] + k * STEP_8M, heading=heading)) for k in range(n)])
    for i in range(before, before + n):
        page.click(f'#node-list .nrow >> nth={i}', modifiers=['Control'])
    page.click('#multibar-connect')
    page.keyboard.press('Escape')


def test_back_on_the_review_checks_the_tour_again(editor):
    page = editor.page
    FakeGoogle(page)
    linked(page, size=(2000, 1500))  # not 2:1: the tour check stops the upload
    page.click('#bupload')
    page.wait_for_timeout(500)
    page.click('#upl-start')
    page.wait_for_function(SHOWN, arg='check-back')
    page.click('#check-continue')  # "Upload anyway"
    page.wait_for_function(SHOWN, arg='review-back')
    page.click('#review-cancel')  # "Back"
    page.wait_for_timeout(300)
    page.click('#upl-start')
    page.wait_for_function(SHOWN, arg='check-back')  # checked again, not skipped
    assert not page.evaluate("document.getElementById('review-back').classList.contains('show')")


def test_the_places_key_never_shows_in_an_error(monkeypatch):
    requests, *_ = google_api._google_libs()

    def offline(method, url, **kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}?key={kwargs.get('params', {}).get('key')}")
    monkeypatch.setattr(requests, 'request', offline)
    places.PLACES_CACHE.clear()
    with pytest.raises(google_api.GoogleError) as err:
        places._geocode('AIzaSecretKey1234567890abcdefghijklmn', 54.3, 10.1)
    assert 'AIza' not in str(err.value) and 'no connection' in err.value.message
    with pytest.raises(google_api.GoogleError) as err:
        places._places_nearby('AIzaSecretKey1234567890abcdefghijklmn', 54.3, 10.1)
    assert 'AIza' not in str(err.value)


def test_photos_google_still_processes(editor):
    """Right after an upload Google is busy for a while: that is not "changes for Google"."""
    page = editor.page

    class Busy(FakeGoogle):
        pass
    google = Busy(page)
    page.unroute('**/api/photo-status')
    page.route('**/api/photo-status', lambda r: r.fulfill(json={'ok': True, 'photos': {}}))  # not visible yet
    linked(page)
    page.click('#bupload')
    page.wait_for_timeout(500)
    page.click('#upl-start')
    page.wait_for_function(SHOWN, arg='review-back')
    assert page.evaluate("document.querySelectorAll('#review-thumbs img').length") == 3
    page.click('#review-go')
    page.wait_for_function("document.getElementById('up-cancel').textContent === \"Don't wait\"", timeout=60000)
    page.click('#up-cancel')
    page.wait_for_function("!document.getElementById('up-finish').hidden", timeout=60000)
    page.click('#upl-close')
    assert len(google.uploads) == 3
    assert 'info: Google is processing 3' in page.evaluate(CHIPS)


def test_tour_progress_and_the_bell(editor):
    page = editor.page
    linked(page)
    linked(page, name='t', north=0.001)  # a second tour
    rows = page.evaluate("[...document.querySelectorAll('#tour-list .tprog')].map(e => e.textContent)")
    assert rows and all(r.endswith('/5') for r in rows), rows
    # plain messages light a dot, an alert counts
    assert page.get_attribute('#notes-badge', 'class') == 'notes-badge dot'
    page.evaluate("alert('Something went wrong')")
    assert page.inner_text('#notes-badge') == '1'
    page.click('#bnotes')
    assert page.is_hidden('#notes-badge')


def test_dialogs_for_the_keyboard(editor):
    page = editor.page
    linked(page)
    page.click('#btools')
    page.click('button[data-run=rename]')
    page.wait_for_function(SHOWN, arg='rename-back')
    box = page.locator('#rename-back .modal')
    assert box.get_attribute('role') == 'dialog' and box.get_attribute('aria-modal') == 'true'
    assert page.inner_text(f"#{box.get_attribute('aria-labelledby')}").startswith('Rename points')
    page.wait_for_function("document.activeElement === document.querySelector('#rename-back .modal')")
    inside = []
    for _ in range(16):  # round and round, never out of the dialog
        page.keyboard.press('Tab')
        inside.append(page.evaluate("document.getElementById('rename-back').contains(document.activeElement)"))
    page.keyboard.press('Shift+Tab')
    inside.append(page.evaluate("document.getElementById('rename-back').contains(document.activeElement)"))
    assert all(inside)
    page.focus('#rename-back .close')
    page.keyboard.press('Enter')  # "Close" works from the keyboard
    assert not page.evaluate("document.getElementById('rename-back').classList.contains('show')")


def test_focus_mode_keys_and_map(editor):
    page = editor.page
    linked(page, heading=90)  # s0 - s1 - s2 going east, the photos facing east
    page.click('#node-list .nrow >> nth=0')
    page.wait_for_timeout(800)
    page.keyboard.press('f')
    page.wait_for_selector('#focus-map.leaflet-container')
    assert page.evaluate("document.querySelectorAll('#focus-map .leaflet-interactive').length") >= 5  # 2 links, 3 points
    for _ in range(3):  # from facing north, turned 3 x 30 degrees: looking east
        page.keyboard.press('ArrowRight')
    page.keyboard.press('ArrowUp')  # on to s1
    page.wait_for_function("document.querySelector('#pano-focus-bar b').textContent === 's1'")
    page.keyboard.press('ArrowDown')  # back west
    page.wait_for_function("document.querySelector('#pano-focus-bar b').textContent === 's0'")
    page.keyboard.press('Escape')


def test_a_video_dropped_on_the_map(editor):
    page = editor.page
    drop(page, [b64('walk.mp4', mp4(data=4000), 'video/mp4')])
    page.wait_for_function(SHOWN, arg='video-back')
    page.wait_for_function("document.getElementById('vd-name').textContent.startsWith('walk.mp4')")
