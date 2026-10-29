"""The side panel and the map: steps with their tools, the photos of a tour as a strip (J / K / N),
names only where they fit, a wider side panel, and the view cone that sets north on the map."""
from helpers import KIEL, STEP_8M, b64, drop, jpg

CHIPS = "[...document.querySelectorAll('#checklist-chips .st-chip')].map(c => c.className.split(' ')[1] + ': ' + c.textContent)"
SELECTED = "document.querySelector('#node-list .nrow.sel .nrow-name')?.textContent"


def three_linked(page, headings=(90, None, 90)):
    drop(page, [b64(f's{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=h)) for k, h in enumerate(headings)])
    for i in range(3):
        page.click(f'#node-list .nrow >> nth={i}', modifiers=['Control'])
    page.click('#multibar-connect')
    page.keyboard.press('Escape')


def test_steps_strip_and_keys(editor):
    page = editor.page
    three_linked(page)
    assert page.evaluate(CHIPS)[:3] == ['ok: 3 photo(s)', 'ok: 2 link(s)', 'todo: 1 without north']
    # the first open step shows its tools
    tools = page.evaluate("[...document.querySelectorAll('#checklist-tools .btn')].map(b => b.textContent)")
    assert tools == ['Headings from walking direction…', 'North from the photos…', 'North from the sun…']
    page.click('#checklist-chips .st-chip[data-step=privacy]')
    page.keyboard.press('Escape')
    assert 'Cover the tripod…' in page.inner_text('#checklist-tools')

    # the strip: the tour in walking order; J / K walk it, N goes to the next photo without north
    page.click('#node-list .nrow >> nth=0')
    assert page.evaluate("document.querySelectorAll('#fs-list .fs-item').length") == 3
    assert page.evaluate("document.querySelectorAll('#fs-list .fs-item.open').length") >= 1  # s1: no north
    page.keyboard.press('j')
    assert page.evaluate(SELECTED) == 's1'
    page.keyboard.press('k')
    assert page.evaluate(SELECTED) == 's0'
    page.keyboard.press('n')
    assert page.evaluate(SELECTED) == 's1'
    page.click('#fs-list .fs-item >> nth=2')
    assert page.evaluate(SELECTED) == 's2'

    # few points: all names shown; zoomed out with many they would only show on hover
    assert 'labels' in page.get_attribute('#map', 'class')


def test_wider_side_panel(editor):
    page = editor.page
    before = page.locator('#sb').bounding_box()['width']
    handle = page.locator('#sb-resize').bounding_box()
    page.mouse.move(handle['x'] + 2, handle['y'] + 200)
    page.mouse.down()
    page.mouse.move(handle['x'] + 202, handle['y'] + 200, steps=6)
    page.mouse.up()
    assert abs(page.locator('#sb').bounding_box()['width'] - (before + 200)) < 4
    page.dblclick('#sb-resize')
    assert page.locator('#sb').bounding_box()['width'] == before


def test_view_cone_sets_north(editor):
    page = editor.page
    drop(page, [b64('cone.jpg', jpg(lat=KIEL[0], lng=KIEL[1]))])  # no compass: north unknown
    page.click('#node-list .nrow >> nth=0')
    page.wait_for_selector('path.view-cone.unknown', state='attached', timeout=20000)
    page.wait_for_timeout(500)
    tip = page.locator('.view-tip').bounding_box()
    marker = page.locator('.leaflet-marker-icon .nd').bounding_box()
    cx, cy = marker['x'] + marker['width'] / 2, marker['y'] + marker['height'] / 2
    # drag the tip to the east of the point: the view (photo's centre, as the panorama opens) faces east
    page.mouse.move(tip['x'] + tip['width'] / 2, tip['y'] + tip['height'] / 2)
    page.mouse.down()
    page.mouse.move(cx + 60, cy - 20, steps=4)
    page.mouse.move(cx + 120, cy, steps=6)
    page.mouse.up()
    page.wait_for_function("document.getElementById('ni-h').value !== ''")
    assert abs(float(page.input_value('#ni-h')) - 90) < 4
    assert page.inner_text('#heading-meta') == 'set by hand'
    page.wait_for_selector('path.view-cone:not(.unknown)', state='attached')
