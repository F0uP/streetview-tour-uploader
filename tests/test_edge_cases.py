"""Edge cases: a point removed whose photo is on Google, positions past ±180°, and the CSV
made from an exported tour-config.json with the editor's positions and north."""
import csv
import json
import subprocess
import sys

from conftest import REPO
from helpers import KIEL, STEP_8M, FakeGoogle, b64, drop, jpg, run_upload


def test_a_removed_point_loses_its_links_and_can_be_deleted_on_google(editor):
    page = editor.page
    google = FakeGoogle(page)
    links = {}  # what Google has: photo id -> its links
    page.route('**/api/photo-status', lambda r: r.fulfill(json={'ok': True, 'photos': {
        i: {'status': 'processing', 'resolvedId': i, 'connections': links.get(i, [])} for i in json.loads(r.request.post_data)['ids']}}))

    def remember(route):
        items = json.loads(route.request.post_data)['items']
        google.updates.append(items)
        links.update({i['id']: i['connections'] for i in items if 'connections' in i})
        route.fulfill(json={'ok': True, 'results': [{'id': i['id'], 'ok': True} for i in items]})
    page.route('**/api/update-photos', remember)

    drop(page, [b64(f'p{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=90)) for k in range(3)])
    for i in range(3):
        page.click(f'#node-list .nrow >> nth={i}', modifiers=['Control'])
    page.click('#multibar-connect')
    page.click('#bupload')
    page.wait_for_timeout(500)
    run_upload(page)
    page.click('#up-finish')
    page.click('#upl-close')
    assert links == {'g0': ['g1'], 'g1': ['g0', 'g2'], 'g2': ['g1']}

    # the middle point is removed from the tour: its neighbours lose the link to it on Google
    page.click('#node-list .nrow >> nth=1')
    page.click('#bn-d')
    google.updates.clear()
    page.click('#bupload')
    page.wait_for_timeout(500)
    assert not page.is_hidden('#upl-delete-removed-row')
    assert page.inner_text('#upl-delete-removed-label') == 'Also delete the photos of 1 point(s) removed from the tour on Google'
    page.check('#upl-delete-removed')
    run_upload(page)
    assert links['g0'] == [] and links['g2'] == []
    assert google.deleted == ['g1']
    page.click('#up-finish')
    assert page.is_hidden('#upl-delete-removed-row')  # done: nothing left to delete


def test_positions_stay_within_180_degrees(editor):
    page = editor.page
    drop(page, [b64('a.jpg', jpg(lat=KIEL[0], lng=KIEL[1]))])
    page.click('#node-list .nrow >> nth=0')
    page.click('details[data-sec=position] summary')
    page.fill('#ni-lng', '190.5')
    page.keyboard.press('Enter')
    assert abs(float(page.input_value('#ni-lng')) + 169.5) < 1e-6


def test_csv_from_tour_config_keeps_the_editors_positions(tmp_path):
    folder = tmp_path / 'photos'
    folder.mkdir()
    (folder / 'a.jpg').write_bytes(jpg(lat=KIEL[0], lng=KIEL[1], heading=10))  # the camera's GPS and compass
    (folder / 'b.jpg').write_bytes(jpg(lat=KIEL[0], lng=KIEL[1] + STEP_8M))
    config = {'startNodeId': 'n1', 'nodes': [
        {'id': 'n1', 'panorama': 'a.jpg', 'lat': 54.5, 'lng': 10.5, 'heading': 123.4, 'links': [{'nodeId': 'n2'}]},  # moved and turned in the editor
        {'id': 'n2', 'panorama': 'b.jpg', 'lat': None, 'lng': None, 'heading': None, 'links': [{'nodeId': 'n1'}, {'nodeId': 'gone'}]},
    ]}
    (tmp_path / 'tour-config.json').write_text(json.dumps(config), encoding='utf-8')
    out = tmp_path / 'tour.csv'
    subprocess.run([sys.executable, str(REPO / 'uploader' / 'gen_tour_csv.py'), '--config', str(tmp_path / 'tour-config.json'),
                    '--folder', str(folder), '--out', str(out)], check=True, capture_output=True)
    rows = list(csv.DictReader(out.open(encoding='utf-8')))
    assert (rows[0]['lat'], rows[0]['lng'], rows[0]['heading'], rows[0]['connects_to']) == ('54.5', '10.5', '123.4', 'b.jpg')
    assert abs(float(rows[1]['lat']) - KIEL[0]) < 1e-5 and rows[1]['connects_to'] == 'a.jpg'  # from EXIF; the unknown link left out
