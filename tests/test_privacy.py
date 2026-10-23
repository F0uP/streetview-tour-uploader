"""Faces and plates: all photos checked on this computer, then reviewed one by one in the blur tool."""
import base64
import io

import pytest
from PIL import Image

from helpers import KIEL, STEP_8M, b64, drop, face_pano, jpg

ROWS = "[...document.querySelectorAll('#pv-list .check-row')].map(r => r.textContent)"
SHOWN = "id => document.getElementById(id).classList.contains('show')"
CHIPS = "[...document.querySelectorAll('#checklist-chips .st-chip')].map(c => c.className.split(' ')[1] + ': ' + c.textContent)"


def needs_webgl(page):
    if not page.evaluate("!!document.createElement('canvas').getContext('webgl2')"):
        pytest.skip('this browser offers no WebGL, which the detection needs')


def check_photos(page, plates=False, skip=True):
    page.set_checked('#pv-faces', True)
    page.set_checked('#pv-plates', plates)
    page.set_checked('#pv-skip', skip)
    page.click('#pv-go')
    page.wait_for_function("!document.getElementById('pv-go').disabled && document.getElementById('pv-status').textContent.startsWith('Checked')",
                           timeout=180000)
    return page.inner_text('#pv-status')


def test_check_all_photos_review_and_blur(editor):
    page = editor.page
    needs_webgl(page)
    face = jpg(image=face_pano(220), lat=KIEL[0], lng=KIEL[1])
    drop(page, [b64('person.jpg', face)] + [b64(f'empty{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M)) for k in (1, 2)])

    page.click('#btools')
    page.click('button[data-run=privacy]')
    status = check_photos(page)
    assert status.startswith('Checked 3 of 3 photo(s): 1 with faces or vehicles (1 face(s), 0 vehicle(s)).'), status
    rows = page.evaluate(ROWS)
    assert len(rows) == 1 and 'person' in rows[0] and '1 face(s)' in rows[0], rows

    # "Review" opens the blur tool with the suggestion; applying goes back to the list
    page.click('#pv-list .check-row button')
    page.wait_for_function(SHOWN, arg='blur-back')
    assert page.inner_text('#blur-count') == '1 area(s) marked.'
    page.click('#blur-apply')
    page.wait_for_function(SHOWN, arg='privacy-back')
    rows = page.evaluate(ROWS)
    assert 'blurred' in rows[0] and rows[0].endswith('Again'), rows

    # checked photos are skipped next time (the blurred one too); the face is gone from the photo
    page.set_checked('#pv-skip', True)
    page.click('#pv-go')
    assert page.inner_text('#pv-status') == 'Nothing to check.'
    status = check_photos(page, skip=False)
    assert status.startswith('Checked 3 of 3 photo(s): 0 with faces'), status

    # what was checked stays with the tour - also after a restart
    page.keyboard.press('Escape')
    assert 'ok: Faces & plates checked' in page.evaluate(CHIPS)
    page.wait_for_timeout(1500)  # the autosave
    page.reload()
    page.wait_for_function(SHOWN, arg='restore-banner')
    page.click('#restore-yes')
    page.wait_for_function("document.querySelectorAll('#node-list .nrow').length === 3")
    assert 'ok: Faces & plates checked' in page.evaluate(CHIPS)


def test_a_face_on_the_seam(editor):
    """A face where the panorama's right edge meets its left one: found whole, blurred on both sides."""
    page = editor.page
    needs_webgl(page)
    im = face_pano(220)
    w, h = im.size
    rolled = Image.new('RGB', (w, h))
    rolled.paste(im.crop((w // 2, 0, w, h)), (0, 0))
    rolled.paste(im.crop((0, 0, w // 2, h)), (w // 2, 0))
    buf = io.BytesIO()
    rolled.save(buf, 'JPEG', quality=90)
    boxes = page.evaluate("""async b => {
      const det = await import('/js/detect.js');
      const bmp = await createImageBitmap(await (await fetch('data:image/jpeg;base64,' + b)).blob());
      return det.findFaces(bmp);
    }""", base64.b64encode(buf.getvalue()).decode())
    assert len(boxes) == 2, boxes
    right, left = sorted(boxes, key=lambda b: -b['x'])
    assert abs(right['x'] + right['w'] - w) < 1 and left['x'] == 0 and left.get('seam') is True, boxes


def test_plates_in_the_blur_tool(editor):
    page = editor.page
    needs_webgl(page)
    zone = page.evaluate("import('/js/detect.js').then(d => d.plateZone({ x: 100, y: 200, w: 400, h: 200 }))")
    assert zone == {'x': 160, 'y': 300, 'w': 280, 'h': 90}

    drop(page, [b64('street.jpg', jpg(lat=KIEL[0], lng=KIEL[1]))])
    page.click('#node-list .nrow')
    page.click('#btools')
    page.click('button[data-run=blur]')
    page.wait_for_function(SHOWN, arg='blur-back')
    page.click('#blur-plates')
    page.wait_for_function("document.getElementById('blur-count').textContent.startsWith('No vehicles')", timeout=120000)
    assert not page.is_disabled('#blur-plates')
