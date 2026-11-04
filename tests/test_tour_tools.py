"""Floor plans, photos along a path, a re-shoot, the horizon, sharpness and exposure, the check
of all photos one after the other, the start view, views over time with the tour report, and
the command-line upload of a project file."""
import argparse
import csv
import io
import json
import math
import re
import sys
import zipfile

from PIL import Image, ImageFilter

from conftest import REPO
from helpers import KIEL, STEP_8M, FakeGoogle, b64, drop, face_pano, file_item, jpg, run_upload, vrtour
from test_privacy import check_photos, needs_webgl

sys.path.insert(0, str(REPO / 'uploader'))

SHOWN = "id => document.getElementById(id).classList.contains('show')"


def png(width, height, color=(200, 40, 40)):
    out = io.BytesIO()
    Image.new('RGB', (width, height), color).save(out, 'PNG')
    return out.getvalue()


def export_project(page, tmp_path):
    page.evaluate("window.showSaveFilePicker = undefined")
    page.click('#bproject')
    with page.expect_download() as dl:
        page.click('button[data-run=exportproject]')
    path = tmp_path / 'saved.vrtour'
    dl.value.save_as(path)
    return path


def csv_rows(page):
    page.click('#bproject')
    with page.expect_download() as dl:
        page.click('#bcsv')
    return list(csv.DictReader(open(dl.value.path(), encoding='utf-8')))


def linked(page, names, step=STEP_8M):
    drop(page, [b64(f'{name}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * step, heading=90, color=(40 + 50 * k, 90, 120)))
                for k, name in enumerate(names)])
    for i in range(len(names)):
        page.click(f'#node-list .nrow >> nth={i}', modifiers=['Control'])
    page.click('#multibar-connect')
    page.keyboard.press('Escape')


def drag(page, selector, dx, dy):
    box = page.locator(selector).bounding_box()
    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=6)
    page.mouse.up()


def test_floor_plans_are_fitted_kept_and_shown_by_floor(editor, make_editor, tmp_path):
    page = editor.page
    drop(page, [b64('hall.jpg', jpg(lat=KIEL[0], lng=KIEL[1]))])
    page.click('#btools')
    page.click('button[data-run=floorplans]')
    assert page.inner_text('#plan-list') == 'No floor plans yet.'
    page.set_input_files('#plan-file', files=[{'name': 'Ground floor.png', 'mimeType': 'image/png', 'buffer': png(400, 200)}])
    page.wait_for_function("!document.getElementById('plan-adjust-bar').hidden")
    page.wait_for_function("(document.querySelector('.plan-img')?.style.transform || '').startsWith('matrix(')")

    # the middle moves the plan
    before = page.locator('.plan-img').bounding_box()
    drag(page, '.plan-handle.move', 60, 40)
    after = page.locator('.plan-img').bounding_box()
    assert abs(after['x'] - before['x'] - 60) < 3 and abs(after['y'] - before['y'] - 40) < 3

    # the corner turns it: a quarter turn clockwise about the middle, the size kept
    mid = page.locator('.plan-handle.move').bounding_box()
    corner = page.locator('.plan-handle.turn').bounding_box()
    vx, vy = corner['x'] - mid['x'], corner['y'] - mid['y']
    drag(page, '.plan-handle.turn', -vy - vx, vx - vy)
    page.click('#plan-adjust-done')
    assert page.locator('#plan-adjust-bar').is_hidden()

    # a floor of its own
    page.click('#btools')
    page.click('button[data-run=floorplans]')
    page.fill('.plan-row .plan-floor input', '1')
    page.keyboard.press('Tab')
    page.keyboard.press('Escape')

    path = export_project(page, tmp_path)
    with zipfile.ZipFile(path) as z:
        project = json.loads(z.read('project.json'))
        plan = project['plans'][0]
        assert plan['imagePath'] in z.namelist() and z.read(plan['imagePath'])[:4] == b'\x89PNG'
    assert (plan['name'], plan['level'], plan['aspect']) == ('Ground floor', 1, 0.5)
    assert abs(plan['rotation'] - 90) < 2, plan
    assert 'image' not in plan

    # in another editor: the plan comes with the project
    other = make_editor().page
    drop(other, [file_item(path)], confirm=None)
    other.wait_for_function("document.querySelectorAll('.plan-img').length === 1")

    # and with the autosave, after a restart
    page.wait_for_timeout(1500)
    page.reload()
    page.wait_for_function(SHOWN, arg='restore-banner')
    page.click('#restore-yes')
    page.wait_for_function("document.querySelectorAll('.plan-img').length === 1")


def test_photos_placed_along_a_drawn_path(editor):
    page = editor.page
    taken = ['2026:05:01 14:00:00', '2026:05:01 14:00:10', '2026:05:01 14:00:40']
    drop(page, [b64(f'walk{k}.jpg', jpg(taken=t, color=(40 + 60 * k, 90, 120))) for k, t in enumerate(taken)])
    page.click('#btools')
    page.click('button[data-run=pathplace]')
    assert page.locator('#path-bar').is_visible()
    assert page.inner_text('#path-done') == 'Place 3 photo(s)'
    page.check('#path-north')

    box = page.locator('#map').bounding_box()
    y = box['y'] + box['height'] - 140
    page.mouse.click(box['x'] + 120, y)
    page.mouse.click(box['x'] + 220, y)
    page.mouse.click(box['x'] + 320, y)
    page.keyboard.press('Backspace')  # the last point back
    page.mouse.click(box['x'] + 520, y)
    page.click('#path-done')
    assert page.locator('#path-bar').is_hidden()
    assert page.inner_text('#st').startswith('Nodes 3 · Links 2')

    # walked at an even pace: 10 s of 40 s is a quarter of the way; going east
    rows = {r['filename']: r for r in csv_rows(page)}
    lat = [float(rows[f'walk{k}.jpg']['lat']) for k in range(3)]
    lng = [float(rows[f'walk{k}.jpg']['lng']) for k in range(3)]
    assert max(lat) - min(lat) < 1e-6
    assert lng[0] < lng[1] < lng[2]
    assert abs((lng[1] - lng[0]) / (lng[2] - lng[0]) - 0.25) < 0.01
    assert all(abs(float(rows[f'walk{k}.jpg']['heading']) - 90) < 1 for k in range(3))
    assert rows['walk1.jpg']['connects_to'] == 'walk0.jpg;walk2.jpg'


def test_a_reshoot_takes_the_place_of_the_photo_on_google(editor):
    page = editor.page
    google = FakeGoogle(page)
    linked(page, ['p0', 'p1', 'p2'])
    page.click('#bupload')
    run_upload(page)
    page.click('#up-finish')
    page.click('#upl-close')

    # the place has changed: a new photo of the middle point, a metre off
    drop(page, [b64('new1.jpg', jpg(lat=KIEL[0] + 0.00001, lng=KIEL[1] + STEP_8M, color=(220, 200, 40)))])
    page.click('#btools')
    page.click('button[data-run=reshoot]')
    page.wait_for_function(SHOWN, arg='reshoot-back')
    rows = page.locator('#rs-list .check-row').all_inner_texts()
    assert len(rows) == 1 and 'new1' in rows[0] and 'replaces p1' in rows[0], rows
    page.click('#rs-go')
    assert page.inner_text('#st').startswith('Nodes 3 · Links 2')

    # the next upload: the new photo goes up, linked like the old one, which is deleted
    google.updates.clear()
    page.click('#bupload')
    page.wait_for_timeout(800)
    assert page.inner_text('#upc-new') == '1'
    run_upload(page)
    assert google.uploads[-1][0] == 'new1.jpg' and len(google.uploads) == 4
    assert google.deleted == ['g1']
    links = {i['id']: sorted(i['connections']) for batch in google.updates for i in batch if 'connections' in i}
    assert links.get('g3') == ['g0', 'g2'] and links.get('g0') == ['g3'] and links.get('g2') == ['g3'], links


def test_the_horizon_levelled_by_hand(editor):
    page = editor.page
    google = FakeGoogle(page)
    drop(page, [b64('tilted.jpg', jpg(lat=KIEL[0], lng=KIEL[1], heading=90))])
    page.click('#node-list .nrow >> nth=0')
    page.click('details[data-sec=horizon] summary')
    page.wait_for_function("document.getElementById('hz-note').textContent === 'as the camera saved it'")
    page.fill('#hz-pitch', '3')
    page.fill('#hz-roll', '-1.5')
    page.wait_for_function("document.getElementById('hz-note').textContent === 'levelled by hand'")
    assert (page.inner_text('#hz-pitch-v'), page.inner_text('#hz-roll-v')) == ('3.0°', '-1.5°')

    # Google gets the values with the photo
    page.click('#bupload')
    run_upload(page)
    assert (google.queries[0]['pitch'], google.queries[0]['roll']) == ('3', '-1.5')
    page.click('#up-finish')
    page.click('#upl-close')

    # the exported tour is turned by them (and by north)
    page.click('#bproject')
    page.click('button[data-run=website]')
    with page.expect_download() as dl:
        page.click('#site-go')
    node = json.loads(zipfile.ZipFile(dl.value.path()).read('tour-config.json'))['nodes'][0]
    assert node['panoData'] == {'poseHeading': 0, 'posePitch': 0, 'poseRoll': 0}
    c = node['sphereCorrection']
    assert abs(c['pan'] + math.pi / 2) < 0.1 and abs(c['tilt']) > 0.01 and abs(c['roll']) > 0.01, c

    # back to the camera's values: Google is told them, not left with the old ones
    page.click('#hz-reset')
    page.wait_for_function("document.getElementById('hz-note').textContent === 'as the camera saved it'")
    page.click('#bupload')
    page.wait_for_timeout(800)
    assert page.inner_text('#upc-changed') == '1'
    run_upload(page)
    sent = [i for batch in google.updates for i in batch if i['id'] == 'g0'][-1]
    assert (sent['pitch'], sent['roll']) == (0, 0), sent


def noise(seed):
    return Image.effect_noise((2048, 1024), 60 + seed).convert('RGB')


def test_tour_check_finds_blurred_and_badly_exposed_photos(editor):
    page = editor.page
    photos = {
        'sharp': noise(0),
        'blurred': noise(1).filter(ImageFilter.GaussianBlur(12)),
        'dark': Image.new('RGB', (2048, 1024), (2, 2, 2)),
        'white': Image.new('RGB', (2048, 1024), (255, 255, 255)),
    }
    drop(page, [b64(f'{name}.jpg', jpg(image=im, lat=KIEL[0], lng=KIEL[1] + k * STEP_8M)) for k, (name, im) in enumerate(photos.items())])
    page.click('#btools')
    page.click('button[data-run=check]')
    page.wait_for_function(SHOWN, arg='check-back')
    page.click('#check-quality')
    page.wait_for_function("document.getElementById('check-north-status').textContent.includes('may be blurred')", timeout=60000)
    found = {}
    for row in page.locator('#check-list .check-row').all():
        msg = row.locator('.msg').inner_text()
        if re.search('blurred|Overexposed|dark', msg):
            found.setdefault(row.locator('.nm').inner_text(), []).append(msg.split(':')[0].split(' (')[0])
    assert 'sharp' not in found, found
    assert found['blurred'] == ['Looks blurred']
    assert 'Very dark' in found['dark'] and 'Overexposed' in found['white'], found


def test_the_check_of_all_photos_goes_on_to_the_next(editor):
    page = editor.page
    needs_webgl(page)
    drop(page, [b64(f'person{k}.jpg', jpg(image=face_pano(220, shift=300 * k), lat=KIEL[0], lng=KIEL[1] + k * STEP_8M)) for k in range(2)])
    page.click('#btools')
    page.click('button[data-run=privacy]')
    status = check_photos(page)
    assert '2 with faces' in status, status
    page.click('#pv-list .check-row >> nth=0 >> button')
    page.wait_for_function(SHOWN, arg='blur-back')
    first = page.inner_text('#blur-title-name')
    assert page.inner_text('#blur-batch') == '2 photo(s) with findings still to look at'
    page.keyboard.press('Backspace')  # the suggestion is not a face after all
    page.keyboard.press('Enter')  # nothing to blur: on to the next photo
    page.wait_for_function("name => document.getElementById('blur-title-name').textContent !== name", arg=first)
    assert page.inner_text('#blur-batch') == '1 photo(s) with findings still to look at'
    page.keyboard.press('Enter')  # blurred: none left, back to the list
    page.wait_for_function("!document.getElementById('blur-back').classList.contains('show')", timeout=60000)
    rows = page.locator('#pv-list .check-row').all_inner_texts()
    assert len(rows) == 2 and any('blurred' in r for r in rows), rows


def test_the_tour_starts_where_the_preview_looks(editor):
    page = editor.page
    linked(page, ['s0', 's1'])
    page.click('#bpreview')
    page.wait_for_function("document.getElementById('preview-frame').contentWindow.psv?.viewer?.state.ready", timeout=60000)
    page.evaluate("document.getElementById('preview-frame').contentWindow.psv.viewer.rotate({ yaw: 1.2, pitch: 0 })")
    page.click('#preview-startview')
    page.click('#preview-website')
    page.wait_for_function(SHOWN, arg='site-back')
    with page.expect_download() as dl:
        page.click('#site-go')
    index = zipfile.ZipFile(dl.value.path()).read('index.html').decode()
    assert abs(float(re.search(r'defaultYaw:(-?[\d.]+)', index).group(1)) - 1.2) < 0.01


def test_views_over_time_and_the_tour_report(editor):
    page = editor.page
    google = FakeGoogle(page)
    linked(page, ['r0', 'r1', 'r2'])
    page.click('#bupload')
    run_upload(page)
    page.click('#up-finish')
    page.click('#upl-close')
    # each look at the views notes them for the day: from the second day on, a chart
    page.clock.set_fixed_time('2026-10-01T10:00:00Z')
    google.views = {'g0': 1000, 'g1': 30, 'g2': 5}
    page.click('#btools')
    page.click('button[data-run=views]')
    page.wait_for_function("document.getElementById('views-sum').textContent === '1,035 views in total.'")
    assert page.locator('#views-chart svg').count() == 0
    page.keyboard.press('Escape')
    page.clock.set_fixed_time('2026-10-20T10:00:00Z')
    google.views = {'g0': 1200, 'g1': 34, 'g2': 5}
    page.click('#btools')
    page.click('button[data-run=views]')
    page.wait_for_function("document.getElementById('views-sum').textContent === '1,239 views in total.'")
    assert page.locator('#views-chart svg').count() == 1
    page.keyboard.press('Escape')

    page.click('#bproject')
    with page.expect_download() as dl:
        page.click('button[data-run=tourreport]')
    assert dl.value.suggested_filename.endswith('-report.html')
    report = open(dl.value.path(), encoding='utf-8').read()
    assert '1,239' in report or '1.239' in report
    assert report.count('src="data:image/jpeg') == 3
    assert '<svg class="views-chart"' in report and 'r1' in report


def test_the_command_line_uploads_a_project(monkeypatch, tmp_path, editor):
    import upload_tour
    path = vrtour(tmp_path / 'Halle.vrtour', 'Halle', KIEL[0], KIEL[1])
    with zipfile.ZipFile(path) as z:
        entries = {i.filename: z.read(i) for i in z.infolist()}
    project = json.loads(entries['project.json'])
    n1, n2, n3 = project['nodes']
    n1['placeId'] = 'ChIJplace'
    n2.update(level={'number': 1, 'name': '1'}, pitch=2.5, roll=-1.25)
    n3['placeId'] = None  # no place, on purpose
    entries['project.json'] = json.dumps(project).encode()
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        for name, data in entries.items():
            z.writestr(name, data)

    uploads, updates = [], []

    def upload(creds, data, filename, lat, lng, heading=None, place_id=None, level=None, pitch=None, roll=None, **_):
        uploads.append({'filename': filename, 'place': place_id, 'level': level, 'pitch': pitch, 'roll': roll, 'size': len(data)})
        return f'up{len(uploads)}'

    monkeypatch.setattr(upload_tour, 'get_credentials', lambda: None)
    monkeypatch.setattr(upload_tour, '_upload_photo', upload)
    monkeypatch.setattr(upload_tour, '_photo_status', lambda creds, ids: {
        i: {'status': 'processing', 'resolvedId': i, 'lat': KIEL[0], 'lng': KIEL[1], 'heading': 90.0} for i in ids})
    monkeypatch.setattr(upload_tour, '_update_photos', lambda creds, items: updates.extend(items) or [{'id': i['id'], 'ok': True} for i in items])
    args = argparse.Namespace(project=str(path), dry_run=False, connect_only=False, wait=0, delete_old=False, place_id='ChIJdefault')
    upload_tour.run_project(args)

    assert [(u['filename'], u['place']) for u in uploads] == [('Halle1.jpg', 'ChIJplace'), ('Halle2.jpg', 'ChIJdefault'), ('Halle3.jpg', None)]
    assert (uploads[1]['level'], uploads[1]['pitch'], uploads[1]['roll']) == ({'number': 1, 'name': '1'}, 2.5, -1.25)
    assert {i['id']: i['connections'] for i in updates} == {'up1': ['up2'], 'up2': ['up1', 'up3'], 'up3': ['up2']}
    assert not upload_tour.journal_of(path).exists()

    # written back into the project: the photos as they were, the IDs new
    with zipfile.ZipFile(path) as z:
        saved = json.loads(z.read('project.json'))
        assert all(z.read(name) == data for name, data in entries.items() if name != 'project.json')
    assert [n['gsvPhotoId'] for n in saved['nodes']] == ['up1', 'up2', 'up3']
    assert all(n['gsvConfirmed'] for n in saved['nodes'])
    # a new version of the project, as editors that have it open count them
    assert saved['rev'] and saved['savedBy']['name'] == 'upload_tour.py'

    # the editor takes it as it is: on Google, nothing to send again
    page = editor.page
    drop(page, [file_item(path)], confirm=None)
    page.wait_for_function("document.querySelectorAll('#node-list .nrow').length === 3")
    assert page.locator('.leaflet-marker-icon .gs').count() == 3
    assert page.locator('.leaflet-marker-icon .gs.changed').count() == 0

    # a second run has nothing to upload
    uploads.clear()
    upload_tour.run_project(args)
    assert uploads == []
