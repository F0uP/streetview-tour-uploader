"""Move or turn a tour, and snap it to paths (OpenStreetMap faked)."""
import math

from helpers import KIEL, STEP_8M, b64, drop, jpg

M_PER_DEG = 111320
POS = "[...document.querySelectorAll('.leaflet-marker-icon')].length"


def positions(page, n=3):
    out = []
    for i in range(n):
        page.click(f'#node-list .nrow >> nth={i}')
        if not page.is_visible('#ni-lat'):
            page.click('details[data-sec=position] summary')
        out.append((float(page.input_value('#ni-lat')), float(page.input_value('#ni-lng'))))
    return out


def metres(a, b):
    return ((b[1] - a[1]) * M_PER_DEG * math.cos(math.radians(a[0])), (b[0] - a[0]) * M_PER_DEG)


def test_move_turn_and_snap_a_tour(editor):
    page = editor.page
    drop(page, [b64(f's{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=90)) for k in range(3)])
    for i in range(3):
        page.click(f'#node-list .nrow >> nth={i}', modifiers=['Control'])
    page.click('#multibar-connect')
    page.keyboard.press('Escape')
    before = positions(page)

    # 5 m east, then 1 m north
    page.click('#btools')
    page.click('button[data-run=shift]')
    page.select_option('#sh-step', '5')
    page.click('#shift-back [data-move="1,0"]')
    page.select_option('#sh-step', '1')
    page.click('#shift-back [data-move="0,1"]')
    assert page.inner_text('#sh-sum') == 'So far: moved 5 m to the east.'
    page.click('#sh-done')
    after = positions(page)
    for a, b in zip(before, after):
        dx, dy = metres(a, b)
        assert abs(dx - 5) < 0.05 and abs(dy - 1) < 0.05, (dx, dy)

    # one undo takes the whole session back
    page.keyboard.press('Escape')
    page.click('#map', position={'x': 40, 'y': 40})
    page.keyboard.press('Control+z')
    assert positions(page) == before

    # turned 90° clockwise about the centre: the line east-west becomes north-south
    page.click('#btools')
    page.click('button[data-run=shift]')
    for _ in range(18):
        page.click('#shift-back [data-turn="5"]')
    page.click('#sh-done')
    turned = positions(page)
    dx, dy = metres(turned[0], turned[2])
    assert abs(dx) < 0.1 and abs(dy + 16) < 0.2, (dx, dy)  # the first point is now north of the last

    # snapped onto a path 4 m north of the original line
    page.keyboard.press('Control+z')
    path_lat = KIEL[0] + 4 / M_PER_DEG
    page.route('https://overpass-api.de/**', lambda r: r.fulfill(json={'elements': [
        {'type': 'way', 'geometry': [{'lat': path_lat, 'lon': KIEL[1] - 0.001}, {'lat': path_lat, 'lon': KIEL[1] + 0.001}]}]}))
    page.click('#btools')
    page.click('button[data-run=shift]')
    page.click('#sh-snap')
    page.wait_for_timeout(800)
    assert '3 of 3 point(s) moved onto a path, by 4 m at most.' in page.evaluate("[...document.querySelectorAll('.toast')].map(t => t.textContent).join(' ')")
    page.click('#sh-done')
    assert all(abs(lat - path_lat) < 1e-7 for lat, _ in positions(page))
