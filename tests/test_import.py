"""The import assistant: photos or a folder in one go - placed, linked, turned to north."""
import random

from helpers import KIEL, STEP_8M, b64, drop, jpg

NAMES = "[...document.querySelectorAll('#node-list .nrow .nrow-name')].map(e => e.textContent)"


def walk_photos():
    """A walk east: g1 g2 [x without GPS] g3 g4 g5, one minute apart."""
    lat, lng = KIEL
    photos = {}
    for k, name in enumerate(['g1', 'g2', 'x', 'g3', 'g4', 'g5']):
        photos[f'{name}.jpg'] = jpg(color=(40 + 25 * k, 90, 120), lat=None if name == 'x' else lat,
                                    lng=lng + k * STEP_8M, taken=f'2026:05:01 14:{k:02d}:00')
    return photos


def test_import_places_links_and_turns_a_walk(editor, tmp_path):
    page = editor.page
    photos = walk_photos()
    order = list(photos.items())
    random.Random(3).shuffle(order)
    drop(page, [b64(n, d) for n, d in order], x=500, y=400, confirm=None)
    page.wait_for_function("document.getElementById('imp-back').classList.contains('show') && !document.getElementById('imp-go').disabled")
    assert page.inner_text('#imp-sum').startswith('6 photo(s) · 5 with GPS · 6 with capture time')
    assert page.is_visible('#imp-gpx-row')
    assert page.inner_text('#imp-go') == 'Import 6 photo(s)'
    page.click('#imp-go')
    page.wait_for_selector('#check-back.show')
    assert page.inner_text('#st') == 'Nodes 6 · Links 4'
    assert page.evaluate(NAMES) == ['g1', 'g2', 'x', 'g3', 'g4', 'g5']  # by capture time
    toast = page.evaluate("[...document.querySelectorAll('.toast')].map(t => t.textContent).join(' ')")
    assert 'Imported 6 photo(s): 4 link(s), 5 heading(s)' in toast and '1 without a position' in toast
    page.click('[data-close=check-back]')
    headings = {}
    for i, name in enumerate(page.evaluate(NAMES)):
        page.click(f'#node-list .nrow >> nth={i}')
        headings[name] = page.input_value('#ni-h')
    assert headings == {'g1': '90.0', 'g2': '90.0', 'x': '', 'g3': '90.0', 'g4': '90.0', 'g5': '90.0'}

    # one undo takes the whole import back
    page.keyboard.press('Escape')
    page.click('#map', position={'x': 50, 'y': 50})
    page.keyboard.press('Control+z')
    page.wait_for_timeout(400)
    assert not page.is_visible('#node-list-panel')

    # a GPX track places the photo without GPS; the camera clock is UTC+2
    folder = tmp_path / 'DCIM' / '100TEST'
    folder.mkdir(parents=True)
    for name, data in photos.items():
        (folder / name).write_bytes(data)
    (tmp_path / 'notes.txt').write_text('not a photo')
    (tmp_path / 'DCIM' / '._g1.jpg').write_bytes(b'resource fork')
    pts = ''.join(f'<trkpt lat="{KIEL[0]}" lon="{KIEL[1] + k * STEP_8M:.7f}"><time>2026-05-01T12:{k:02d}:00Z</time></trkpt>' for k in range(6))
    gpx = tmp_path / 'track.gpx'
    gpx.write_text(f'<?xml version="1.0"?><gpx version="1.1" creator="t"><trk><trkseg>{pts}</trkseg></trk></gpx>')
    page.keyboard.press('Control+i')
    page.set_input_files('#imp-files-input', [str(folder / n) for n in photos])
    page.wait_for_function("!document.getElementById('imp-go').disabled")
    page.set_input_files('#imp-gpx', str(gpx))
    page.click('#imp-go')
    page.wait_for_selector('#check-back.show')
    assert page.inner_text('#st') == 'Nodes 6 · Links 5'
    page.click('[data-close=check-back]')

    # the same photos again: skipped
    page.keyboard.press('Control+i')
    page.set_input_files('#imp-files-input', [str(folder / n) for n in photos])
    page.wait_for_timeout(1000)
    assert '6 already in the tour - skipped' in page.inner_text('#imp-sum')
    assert page.is_disabled('#imp-go')
    page.click('[data-close=imp-back]')

    # a whole folder, with subfolders; text files and resource forks are left out
    page.keyboard.press('Control+z')
    page.keyboard.press('Control+i')
    page.set_input_files('#imp-folder-input', str(tmp_path))
    page.wait_for_function("!document.getElementById('imp-go').disabled")
    assert page.inner_text('#imp-sum').startswith('6 photo(s)')
    page.click('[data-close=imp-back]')


def test_a_single_photo_lands_where_it_is_dropped(editor):
    page = editor.page
    drop(page, [b64('one.jpg', jpg(lat=KIEL[0], lng=KIEL[1]))], confirm=None)
    assert not page.evaluate("document.getElementById('imp-back').classList.contains('show')")
    assert page.inner_text('#st') == 'Nodes 1 · Links 0'
