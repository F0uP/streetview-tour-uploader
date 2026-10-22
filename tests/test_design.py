"""Checklist chips, the review before publishing, the message history and the panorama focus mode."""
from helpers import KIEL, STEP_8M, FakeGoogle, b64, drop, jpg, run_upload

CHIPS = "[...document.querySelectorAll('#checklist-chips .st-chip')].map(c => c.className.split(' ')[1] + ': ' + c.textContent)"
REVIEW = "[...document.querySelectorAll('#review-list .check-row')].map(r => r.className.split(' ')[1] + ': ' + r.querySelector('.msg').textContent)"
SHOWN = "id => document.getElementById(id).classList.contains('show')"


def three_linked(page, north=(90, 90, None)):
    drop(page, [b64(f's{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=h)) for k, h in enumerate(north)])
    for i in range(3):
        page.click(f'#node-list .nrow >> nth={i}', modifiers=['Control'])
    page.click('#multibar-connect')
    page.keyboard.press('Escape')


def test_checklist_and_review(editor):
    page = editor.page
    google = FakeGoogle(page)
    three_linked(page)
    chips = page.evaluate(CHIPS)
    assert chips == ['ok: 3 photo(s)', 'todo: 1 without north', 'todo: Faces & plates: 3 not checked', 'info: Places 0/3', 'todo: Not on Google yet'], chips

    # a chip leads to what is left: the point without north
    page.click('#checklist-chips .st-chip:has-text("without north")')
    assert page.input_value('#ni-n') == 's2'
    page.fill('#ni-h', '45')
    page.press('#ni-h', 'Enter')
    page.wait_for_function("document.querySelector('#checklist-chips').textContent.includes('North set')")

    # the review comes before anything goes up; "Back" uploads nothing
    page.click('#bupload')
    page.wait_for_timeout(500)
    page.click('#upl-start')
    page.wait_for_function(SHOWN, arg='review-back')
    assert page.inner_text('#review-sum') == '3 new photo(s) · 0 update(s) of photos on Google'
    rows = page.evaluate(REVIEW)
    assert rows[:3] == ['ok: 3 photo(s), each at its position', 'ok: North is set for every photo.',
                        'ok: 2 link(s): Google shows them as arrows between the photos.'], rows
    assert 'warn: 3 photo(s) not checked for faces and licence plates - photos published this way are not blurred by Google.' in rows
    page.click('#review-cancel')
    page.wait_for_timeout(300)
    assert google.uploads == []

    run_upload(page)
    assert len(google.uploads) == 3
    page.click('#upl-close')
    assert 'ok: On Google' in page.evaluate(CHIPS)


def test_messages_are_kept(editor):
    page = editor.page
    three_linked(page)
    assert not page.is_hidden('#notes-badge')  # "Connected 3 nodes in sequence", ...
    page.click('#bnotes')
    msgs = page.evaluate("[...document.querySelectorAll('#notes-list .note-msg')].map(e => e.textContent)")
    assert 'Connected 3 nodes in sequence' in msgs, msgs
    assert page.is_hidden('#notes-badge')
    page.click('#notes-clear')
    page.click('#bnotes')
    assert page.inner_text('#notes-list') == 'No messages yet.'
    page.keyboard.press('Escape')
    page.evaluate("alert('Something went wrong')")
    page.click('#bnotes')
    assert page.evaluate("[...document.querySelectorAll('#notes-list .note.alert .note-msg')].map(e => e.textContent)") == ['Something went wrong']


def test_panorama_focus_mode(editor):
    page = editor.page
    three_linked(page, north=(90, 90, 90))
    page.click('#node-list .nrow >> nth=1')
    page.wait_for_timeout(800)
    page.keyboard.press('f')
    assert 'focus' in page.get_attribute('#pano-wrap', 'class')
    box = page.locator('#pano-wrap').bounding_box()
    assert box['width'] == 1400 and box['height'] == 950
    assert page.inner_text('#pano-focus-bar b') == 's1'
    bar = page.locator('#pano-focus-bar').bounding_box()
    assert bar['y'] < 40 and bar['height'] < 60, bar  # a bar at the top, not over the panorama
    walk = page.evaluate("[...document.querySelectorAll('#pano-focus-bar [data-go]')].map(b => b.textContent)")
    assert sorted(walk) == ['→ s0', '→ s2'], walk
    page.click('#pano-focus-bar [data-go]:has-text("s2")')
    page.wait_for_function("document.querySelector('#pano-focus-bar b').textContent === 's2'")
    assert page.input_value('#ni-n') == 's2'
    page.keyboard.press('Escape')
    assert 'focus' not in page.get_attribute('#pano-wrap', 'class')
    assert page.input_value('#ni-n') == 's2'  # Esc only left the focus mode
