"""Working on one project file together: merging two versions, two editors on one file in a
cloud folder (each with its own copy of it, synced when the test says so), and in the app:
who else has it open and the copies a cloud folder keeps."""
import importlib
import io
import json
import os
import shutil
import sys
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest
from PIL import Image

from conftest import REPO, free_port, open_editor, start
from helpers import KIEL, STEP_8M, b64, drop, jpg

SHOWN = "id => document.getElementById(id).classList.contains('show')"
NAMES = "[...document.querySelectorAll('#node-list .nrow-name')].map(e => e.textContent)"
ROWS = "document.querySelectorAll('#node-list .nrow').length"


# ── Merging (merge.js) ────────────────────────────
def node(id, name, **kw):
    return {'id': id, 'name': name, 'lat': 54.32, 'lng': 10.12, 'hasGps': True, 'heading': None, 'origHeading': None,
            'viewYaw': 0, 'origViewYaw': 0, 'headingSource': 'manual', 'photoName': f'{name}.jpg',
            'photoPath': f'photos/{id}/{name}.jpg', 'photoType': 'image/jpeg', 'photoSize': 1000, 'captureTime': None,
            'captureOffset': None, 'blurred': False, 'privacyChecked': False, 'gsvPhotoId': None, 'gsvStatus': None,
            'gsvShareLink': None, 'gsvSig': None, 'gsvConfirmed': False, 'gsvUploadedAt': None, 'gsvObsolete': [],
            'hotspots': [], 'tourId': 't1', 'pitch': None, 'roll': None, 'viewsLog': [], 'startView': None, **kw}


def project(nodes, conns=(), nxt=10):
    return {'version': 2, 'nodes': nodes, 'conns': [{'id': f'c{a}{b}', 'from': a, 'to': b} for a, b in conns],
            'startIds': ['n1'], 'tours': {'t1': 'Hof'}, 'plans': [], 'gsvRemoved': [], 'nxt': nxt}


def merge(page, base, ours, theirs):
    return page.evaluate("""async ([base, ours, theirs]) => {
        const { mergeProjects } = await import('./js/merge.js');
        const { data, report } = mergeProjects(base, ours, theirs);
        return { data, report: { ...report, photos: Object.fromEntries(report.photos), plans: Object.fromEntries(report.plans),
                                 renamed: Object.fromEntries(report.renamed) } };
    }""", [base, ours, theirs])


def test_two_versions_are_merged(editor):
    page = editor.page
    tor, hof, garten, halle = node('n1', 'Tor'), node('n2', 'Hof'), node('n3', 'Garten'), node('n4', 'Halle')
    base = project([tor, hof, garten, halle], [('n1', 'n2'), ('n2', 'n3'), ('n3', 'n4')])
    ours = project([{**tor, 'lat': 54.3201}, {**hof, 'name': 'Innenhof'}, {**garten, 'heading': 90},
                    {**halle, 'privacyChecked': True}, node('n10', 'Neu')],
                   [('n1', 'n2'), ('n3', 'n4'), ('n4', 'n10')], nxt=11)
    theirs = project([{**tor, 'heading': 45}, hof, {**garten, 'heading': 180}, node('n10', 'Weg')],
                     [('n1', 'n2'), ('n2', 'n3'), ('n1', 'n3'), ('n3', 'n10')], nxt=11)
    data, report = merge(page, base, ours, theirs).values()
    nodes = {n['id']: n for n in data['nodes']}
    # different parts of one point: both; the same part changed on both sides: ours, and said so
    assert (nodes['n1']['lat'], nodes['n1']['heading']) == (54.3201, 45)
    assert nodes['n2']['name'] == 'Innenhof' and nodes['n3']['heading'] == 90
    assert report['conflicts'] == [{'name': 'Garten', 'what': ['north']}]
    # deleted there, changed here: kept, with its links
    assert 'n4' in nodes and report['restored'] == ['Halle']
    # added on both sides under the same id: both, ours under a new one
    assert report['renamed'] == {'n10': 'n11'}
    assert (nodes['n10']['name'], nodes['n11']['name'], nodes['n11']['photoPath']) == ('Weg', 'Neu', 'photos/n11/Neu.jpg')
    assert report['added'] == ['Weg'] and report['photos']['n10'] == 'theirs' and report['photos']['n11'] == 'ours'
    pairs = sorted(tuple(sorted((c['from'], c['to']))) for c in data['conns'])
    assert pairs == [('n1', 'n2'), ('n1', 'n3'), ('n10', 'n3'), ('n11', 'n4'), ('n3', 'n4')]
    assert len({c['id'] for c in data['conns']}) == 5
    assert data['nxt'] >= 12


def test_an_upload_of_the_other_photo_is_out_of_date(editor):
    page = editor.page
    tor = node('n1', 'Tor')
    blurred = {**tor, 'blurred': True, 'privacyChecked': True, 'photoSize': 900}
    uploaded = {**tor, 'gsvPhotoId': 'T1', 'gsvStatus': 'pending', 'gsvUploadedAt': 123}
    data, report = merge(page, project([tor]), project([blurred]), project([uploaded])).values()
    n = data['nodes'][0]
    # the blurred photo stays; the unblurred one went up meanwhile: deleted with the next upload
    assert n['blurred'] and n['gsvPhotoId'] is None and n['gsvObsolete'] == ['T1']
    assert report['retired'] == ['Tor'] and report['conflicts'] == [] and report['photos']['n1'] == 'ours'


def test_an_older_editor_and_no_common_version(editor):
    page = editor.page
    tor = node('n1', 'Tor', pitch=2, roll=1)
    older = {k: v for k, v in tor.items() if k not in ('pitch', 'roll', 'photoSize')}
    data, report = merge(page, project([tor]), project([tor]), project([{**older, 'name': 'Tor Ost'}])).values()
    # keys an older editor doesn't write are not taken as removed
    assert (data['nodes'][0]['name'], data['nodes'][0]['pitch'], data['nodes'][0]['roll']) == ('Tor Ost', 2, 1)
    assert report['conflicts'] == []

    # without the version both started from: nothing is deleted, ours wins where both differ
    # (a point added there comes after the one before it there)
    data, report = merge(page, None, project([tor, node('n2', 'Hof')]),
                         project([{**tor, 'name': 'Tor Ost'}, node('n3', 'Weg')])).values()
    assert [n['name'] for n in data['nodes']] == ['Tor', 'Weg', 'Hof']
    assert report['conflicts'] == [{'name': 'Tor', 'what': ['name']}]


UPLOADED = {'gsvPhotoId': 'U1', 'gsvConfirmed': True, 'gsvStatus': 'published'}


def test_a_photo_on_google_is_not_retired_by_mistake(editor):
    page = editor.page
    tor, hof = node('n1', 'Tor', **UPLOADED), node('n2', 'Hof')
    # no common version: the same upload on both sides stays what it is
    data, report = merge(page, None, project([{**tor, 'privacyChecked': True}]), project([tor])).values()
    assert (data['nodes'][0]['gsvPhotoId'], data['nodes'][0]['gsvObsolete'], report['retired']) == ('U1', [], [])
    # deleted there, kept here: its photo is not one of those to delete on Google
    theirs = {**project([hof]), 'gsvRemoved': ['U1']}
    data, report = merge(page, project([tor, hof]), project([{**tor, 'name': 'Tor Ost'}, hof]), theirs).values()
    assert report['restored'] == ['Tor Ost'] and data['gsvRemoved'] == []


def test_records_are_no_changes_of_a_point(editor):
    page = editor.page
    tor, hof = node('n1', 'Tor', **UPLOADED), node('n2', 'Hof')
    counted = {**tor, 'gsvViews': 7, 'viewsLog': [['2026-11-05', 7]], 'gsvStatus': 'processing', 'captureTime': '2026-05-01T14:00:00'}
    data, report = merge(page, project([tor, hof]), project([counted, hof]), project([hof])).values()
    assert [n['name'] for n in data['nodes']] == ['Hof'] and report['removed'] == ['Tor']


def test_a_blurred_photo_is_never_given_up(editor):
    page = editor.page
    tor = node('n1', 'Tor')
    blurred = {**tor, 'blurred': True, 'privacyChecked': True, 'photoSize': 900}
    # "nothing to blur" here, or the capture time read here: nothing that speaks against the blurred photo
    for here in ({**tor, 'privacyChecked': True}, {**tor, 'captureTime': '2026-05-01T14:00:00'}):
        data, report = merge(page, project([tor]), project([here]), project([blurred])).values()
        assert data['nodes'][0]['blurred'] and report['photos']['n1'] == 'theirs' and report['conflicts'] == [], report
    # the photo changed here as well: the blurred one stays, and that is said
    covered = {**tor, 'photoSize': 1200}
    data, report = merge(page, project([tor]), project([covered]), project([blurred])).values()
    assert data['nodes'][0]['photoSize'] == 900 and report['blurKept'] == ['Tor'] and report['conflicts'] == []


# ── Two editors, one file in a cloud folder (Chrome / Edge) ──
class Disk:
    """One computer's copy of the cloud folder: the editor reads and writes it through a
    file handle like Chrome's; sync() is the cloud service copying a file over."""

    def __init__(self):
        self.files = {}

    def route(self, route):
        req = route.request
        name = unquote(urlparse(req.url).path.split('/__disk/', 1)[1])
        if req.method == 'PUT':
            self.files[name] = (req.post_data_buffer, time.time_ns() // 1000)
            return route.fulfill(status=200, json={})
        if name not in self.files:
            return route.fulfill(status=404, body='')
        data, mtime = self.files[name]
        route.fulfill(status=200, body=data, headers={'X-Mtime': str(mtime // 1000), 'Content-Type': 'application/octet-stream'})

    def sync_to(self, other, name='Hof.vrtour'):
        other.files[name] = self.files[name]

    def project(self, name='Hof.vrtour'):
        return json.loads(zipfile.ZipFile(io.BytesIO(self.files[name][0])).read('project.json'))


FAKE_FS = """
localStorage.setItem('collabName', %r);
(() => {
  class Handle {
    constructor(name) { this.kind = 'file'; this.name = name; }
    async getFile() {
      const r = await fetch('/__disk/' + encodeURIComponent(this.name), { cache: 'no-store' });
      if (!r.ok) throw new DOMException('gone', 'NotFoundError');
      return new File([await r.arrayBuffer()], this.name, { lastModified: Number(r.headers.get('X-Mtime')) });
    }
    async createWritable() {
      const parts = [], name = this.name;
      return { async write(d) { parts.push(d); },
               async close() { await fetch('/__disk/' + encodeURIComponent(name), { method: 'PUT', body: new Blob(parts) }); } };
    }
    async isSameEntry(other) { return other?.name === this.name; }
    async queryPermission() { return 'granted'; }
    async requestPermission() { return 'granted'; }
  }
  window.showOpenFilePicker = async () => [new Handle('Hof.vrtour')];
  window.showSaveFilePicker = async () => new Handle('Hof.vrtour');
})();
"""


def computer(make_editor, name, disk):
    return make_editor(init_script=FAKE_FS % name, routes=lambda ed: ed.page.route('**/__disk/**', disk.route)).page


def save(page):
    page.keyboard.press('Control+s')
    page.wait_for_function("document.getElementById('proj-dirty').hidden")


def rename(page, row, name):
    page.click(f'#node-list .nrow >> nth={row}')
    page.fill('#ni-n', name)
    page.evaluate("document.activeElement.blur()")


def look(page):
    page.evaluate("window.dispatchEvent(new Event('focus'))")


def toast_with(page, text):
    page.wait_for_function("text => [...document.querySelectorAll('.toast')].some(t => t.textContent.includes(text))", arg=text)


def photo(name, k, color):
    return b64(name, jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=90, color=color))


def test_two_computers_on_one_project_file(make_editor):
    disk_a, disk_b = Disk(), Disk()
    anna, bea = computer(make_editor, 'Anna', disk_a), computer(make_editor, 'Bea', disk_b)

    drop(anna, [photo(f'p{k}.jpg', k, (40 + 50 * k, 90, 120)) for k in range(3)])
    save(anna)
    disk_a.sync_to(disk_b)
    bea.keyboard.press('Control+o')
    bea.wait_for_function("document.querySelectorAll('#node-list .nrow').length === 3")

    # both work on it at the same time: each adds a photo and names the same point
    drop(anna, [photo('anna.jpg', 4, (200, 60, 60))])
    rename(anna, 1, 'Garden')
    save(anna)
    drop(bea, [photo('bea.jpg', 5, (60, 200, 60))])
    rename(bea, 1, 'Courtyard')

    # Anna's version arrives at Bea's: merged into her tour, her name for the point kept
    disk_a.sync_to(disk_b)
    look(bea)
    bea.wait_for_function("document.querySelectorAll('#node-list .nrow').length === 5")
    toast_with(bea, 'your version is kept')
    assert bea.evaluate(NAMES)[1] == 'Courtyard'
    assert not bea.locator('#proj-dirty').is_hidden()  # her own changes are not saved yet

    # Bea saves; at Anna's, the tour becomes the same
    save(bea)
    disk_b.sync_to(disk_a)
    look(anna)
    anna.wait_for_function("document.querySelectorAll('#node-list .nrow').length === 5")
    anna.wait_for_function(f"{NAMES}[1] === 'Courtyard'")
    toast_with(anna, 'Bea saved changes')
    assert anna.locator('#proj-dirty').is_hidden()  # nothing of her own left unsaved

    final = disk_a.project()
    assert sorted(n['photoName'] for n in final['nodes']) == ['anna.jpg', 'bea.jpg', 'p0.jpg', 'p1.jpg', 'p2.jpg']
    assert len({n['id'] for n in final['nodes']}) == 5
    assert final['savedBy']['name'] == 'Bea' and len(final['revs']) >= 2
    with zipfile.ZipFile(io.BytesIO(disk_a.files['Hof.vrtour'][0])) as z:
        assert all(n['photoPath'] in z.namelist() for n in final['nodes'])
        assert z.namelist()[-1] == 'project.json'  # at the end: saving again changes the file there

    # Anna lets the editor save by itself
    anna.click('#bproject')
    anna.click('button[data-run=collab]')
    anna.wait_for_function(SHOWN, arg='collab-back')
    assert 'Hof.vrtour' in anna.inner_text('#collab-file') and anna.input_value('#collab-name') == 'Anna'
    anna.check('#collab-autosave')
    anna.keyboard.press('Escape')
    rename(anna, 0, 'Gate')
    wait_for(lambda: disk_a.project()['nodes'][0]['name'] == 'Gate', seconds=30, page=anna)
    anna.wait_for_function("document.getElementById('proj-dirty').hidden")


def test_saved_under_another_name_it_is_a_project_of_its_own(make_editor):
    disk = Disk()
    page = computer(make_editor, 'Anna', disk)
    drop(page, [photo('p.jpg', 0, (40, 90, 120))])
    save(page)
    first = disk.project()
    page.keyboard.press('Control+Shift+s')
    wait_for(lambda: disk.project()['projectId'] != first['projectId'], page=page)
    copy = disk.project()
    assert copy['revs'] == []  # no version of the first file: not offered as a copy of it

    rename(page, 0, 'Tor')
    save(page)
    assert disk.project()['projectId'] == copy['projectId'] and disk.project()['revs'] == [copy['rev']]


class SlowDisk(Disk):
    """A disk that keeps a save waiting: the time to do something while it runs."""
    hold, held = False, None

    def route(self, route):
        if route.request.method == 'PUT' and self.hold:
            self.held = route
            return
        super().route(route)

    def release(self):
        self.hold = False
        route, self.held = self.held, None
        super().route(route)


def three(page):
    drop(page, [photo(f'p{k}.jpg', k, (40 + 50 * k, 90, 120)) for k in range(3)])
    page.wait_for_function(f"{ROWS} === 3")


def test_undoing_open_goes_back_to_the_project_before_it(make_editor):
    disk = Disk()
    bea = computer(make_editor, 'Bea', disk)
    drop(bea, [photo(f'b{k}.jpg', k, (40 + 50 * k, 90, 120)) for k in range(2)])
    bea.wait_for_function(f"{ROWS} === 2")
    save(bea)

    anna = computer(make_editor, 'Anna', disk)
    drop(anna, [photo('mine.jpg', 5, (200, 60, 60))])
    anna.wait_for_function(f"{ROWS} === 1")
    anna.keyboard.press('Control+o')
    anna.wait_for_function(f"{ROWS} === 2")
    assert anna.inner_text('#proj-title') == 'Hof.vrtour'
    anna.keyboard.press('Control+z')
    anna.wait_for_function(f"{ROWS} === 1")
    assert anna.inner_text('#proj-title') != 'Hof.vrtour'

    # saved now: into a file of its own, not over the project that was opened
    anna.evaluate("window.showSaveFilePicker = async () => { const [h] = await window.showOpenFilePicker(); h.name = 'Anna.vrtour'; return h; }")
    save(anna)
    assert len(disk.project()['nodes']) == 2 and len(disk.project('Anna.vrtour')['nodes']) == 1

    # and redone: the project that was opened again, with its file
    anna.keyboard.press('Control+y')
    anna.wait_for_function(f"{ROWS} === 2")
    assert anna.inner_text('#proj-title') == 'Hof.vrtour'


def test_what_changes_while_saving_is_not_lost(make_editor):
    disk = SlowDisk()
    page = computer(make_editor, 'Anna', disk)
    three(page)
    save(page)
    rename(page, 0, 'Before')
    disk.hold = True
    page.keyboard.press('Control+s')
    wait_for(lambda: disk.held, seconds=20, page=page)
    # a name and a photo change while the file is written
    rename(page, 1, 'Meanwhile')
    page.set_input_files('#ni-p', files=[{'name': 'p1.jpg', 'mimeType': 'image/jpeg',
                                          'buffer': jpg(lat=KIEL[0], lng=KIEL[1] + STEP_8M, color=(250, 0, 0))}])
    page.wait_for_timeout(1000)
    disk.release()
    page.wait_for_function("document.getElementById('save-status-text').textContent === 'Saved'")
    page.wait_for_timeout(500)
    assert 'Meanwhile' not in [n['name'] for n in disk.project()['nodes']]
    assert not page.locator('#proj-dirty').is_hidden()  # still to be saved, not shown as saved

    # the new photo is still the one of the point, and goes into the file
    save(page)
    saved = disk.project()
    assert 'Meanwhile' in [n['name'] for n in saved['nodes']]
    path = next(n['photoPath'] for n in saved['nodes'] if n['photoName'] == 'p1.jpg')
    with zipfile.ZipFile(io.BytesIO(disk.files['Hof.vrtour'][0])) as z:
        r, g, _ = Image.open(io.BytesIO(z.read(path))).convert('RGB').getpixel((100, 100))
    assert r > 200 and g < 60, (r, g)


def test_saving_again_does_not_read_every_photo(make_editor):
    disk = Disk()
    page = computer(make_editor, 'Anna', disk)
    three(page)
    save(page)
    page.evaluate("""() => { window.sizesRead = []; const read = Blob.prototype.arrayBuffer;
        Blob.prototype.arrayBuffer = function () { window.sizesRead.push(this.size); return read.call(this); }; }""")
    for name in ('First', 'Second'):
        rename(page, 0, name)
        save(page)
    assert not set(page.evaluate("window.sizesRead")) & {n['photoSize'] for n in disk.project()['nodes']}


def test_changes_of_others_leave_the_selection_and_the_view(make_editor):
    disk_a, disk_b = Disk(), Disk()
    anna, bea = computer(make_editor, 'Anna', disk_a), computer(make_editor, 'Bea', disk_b)
    three(anna)
    save(anna)
    disk_a.sync_to(disk_b)
    bea.keyboard.press('Control+o')
    bea.wait_for_function(f"{ROWS} === 3")
    bea.click('#node-list .nrow >> nth=0')
    box = bea.locator('#pano-view').bounding_box()
    bea.wait_for_timeout(1500)
    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    bea.mouse.move(x, y)
    bea.mouse.down()
    bea.mouse.move(x + 70, y, steps=5)
    bea.mouse.up()
    bea.wait_for_timeout(1500)
    bea.click('#node-list .nrow >> nth=1', modifiers=['Control'])
    bea.click('#node-list .nrow >> nth=2', modifiers=['Control'])
    bea.wait_for_function("document.getElementById('multibar').classList.contains('show')")
    view = bea.locator('.view-tip').bounding_box()  # the tip of the view cone on the map: where the panorama looks

    rename(anna, 2, 'Other')
    save(anna)
    disk_a.sync_to(disk_b)
    look(bea)
    bea.wait_for_function(f"{NAMES}[2] === 'Other'")
    bea.wait_for_timeout(1000)
    assert bea.evaluate("document.getElementById('multibar').classList.contains('show')")
    assert bea.locator('.view-tip').bounding_box() == view


def test_messages_lie_one_above_the_other(make_editor):
    disk_a, disk_b = Disk(), Disk()
    anna, bea = computer(make_editor, 'Anna', disk_a), computer(make_editor, 'Bea', disk_b)
    three(anna)
    save(anna)
    disk_a.sync_to(disk_b)
    bea.keyboard.press('Control+o')
    bea.wait_for_function(f"{ROWS} === 3")
    rename(anna, 1, 'Garden')
    rename(anna, 2, 'Yard')
    save(anna)
    rename(bea, 1, 'Courtyard')
    disk_a.sync_to(disk_b)
    look(bea)
    toast_with(bea, 'your version is kept')
    boxes = sorted(bea.evaluate("[...document.querySelectorAll('.toast')].map(t => { const r = t.getBoundingClientRect(); return [r.top, r.bottom]; })"))
    assert len(boxes) >= 2 and all(a[1] <= b[0] for a, b in zip(boxes, boxes[1:])), boxes


def test_a_file_of_a_newer_editor_is_neither_merged_nor_saved_over(make_editor):
    disk = Disk()
    page = computer(make_editor, 'Anna', disk)
    three(page)
    save(page)
    with zipfile.ZipFile(io.BytesIO(disk.files['Hof.vrtour'][0])) as z:
        entries = {i.filename: z.read(i) for i in z.infolist()}
    newer = json.loads(entries['project.json'])
    newer.update(version=99, rev='future', revs=newer['revs'] + [newer['rev']], tomorrow='kept')
    newer['nodes'][0]['name'] = 'Future'
    entries['project.json'] = json.dumps(newer).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        for name, data in entries.items():
            z.writestr(name, data)
    disk.files['Hof.vrtour'] = (out.getvalue(), time.time_ns() // 1000)
    look(page)
    toast_with(page, 'newer version of the editor')
    assert page.evaluate(NAMES)[0] != 'Future'

    rename(page, 1, 'Mine')
    page.keyboard.press('Control+s')
    page.wait_for_function("document.getElementById('save-status-text').textContent === 'Project save failed'", timeout=15000)
    assert disk.project().get('tomorrow') == 'kept'


def test_saving_by_itself_says_nothing_about_an_empty_tour(make_editor):
    disk = Disk()
    ed = make_editor(init_script=FAKE_FS % 'Anna' + "localStorage.setItem('collabAutosave', '1');",
                     routes=lambda e: e.page.route('**/__disk/**', disk.route))
    page = ed.page
    drop(page, [photo('a.jpg', 0, (40, 90, 120))])
    page.wait_for_function(f"{ROWS} === 1")
    save(page)
    page.click('#node-list .nrow >> nth=0')
    page.click('#bn-d')
    page.wait_for_timeout(10000)
    assert not [d for d in ed.dialogs if 'No nodes' in d], ed.dialogs


def test_only_a_name_set_goes_into_a_project_file(make_editor):
    disk = Disk()
    page = computer(make_editor, 'Anna', disk)
    page.evaluate("localStorage.removeItem('collabName')")
    drop(page, [photo('a.jpg', 0, (40, 90, 120))])
    page.wait_for_function(f"{ROWS} === 1")
    save(page)
    assert set(disk.project()['savedBy']) == {'at'}  # not the computer's user name
    # given to someone else, a tour says nothing about who saved it
    page.evaluate("localStorage.setItem('collabName', 'Anna')")
    page.evaluate("window.showSaveFilePicker = undefined")
    page.click('#bproject')
    with page.expect_download() as dl:
        page.click('button[data-run=exportproject]')
    assert 'savedBy' not in json.loads(zipfile.ZipFile(dl.value.path()).read('project.json'))


# ── In the app: who else has it open, the copies a cloud folder keeps ──
class FakeWindow:
    """The app window as the server sees it: its file dialogs pick `path`."""

    def __init__(self):
        self.path = None

    def pick(self, kind, **kw):
        return str(self.path)

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def app(tmp_path_factory, monkeypatch):
    os.environ.setdefault('SVT_DATA_DIR', str(tmp_path_factory.mktemp('collab-profile')))
    sys.path.insert(0, str(REPO))
    r = importlib.import_module('run_editor')
    win = FakeWindow()
    port = free_port()
    monkeypatch.setitem(r.APP, 'window', win)
    monkeypatch.setattr(r, 'PORT', port)
    monkeypatch.setattr(r, 'EDITOR_KEY', 'k' * 40)
    monkeypatch.setattr(r.Handler, 'log_message', lambda *a: None)
    trashed = []
    monkeypatch.setattr(r, '_to_trash', lambda p: (trashed.append(Path(p).name), Path(p).unlink()))
    r.ALLOWED_HOSTS.add(f'127.0.0.1:{port}')
    r.ALLOWED_ORIGINS.add(f'http://127.0.0.1:{port}')
    srv = ThreadingHTTPServer(('127.0.0.1', port), r.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SimpleNamespace(r=r, win=win, trashed=trashed, url=f'http://127.0.0.1:{port}/editor.html#k={"k" * 40}')
    srv.shutdown()
    srv.server_close()


def wait_for(condition, seconds=10, page=None):
    """Until condition() holds; with a page, Playwright goes on handling its routes meanwhile."""
    deadline = time.time() + seconds
    while not condition():
        assert time.time() < deadline, 'timed out'
        page.wait_for_timeout(100) if page else time.sleep(0.1)


def app_page(browser, app, name):
    ed = open_editor(browser, app)
    ed.context.add_init_script(f"localStorage.setItem('collabName', {json.dumps(name)})")
    start(ed, app)
    return ed


def test_in_the_app_who_is_there_and_the_copies_a_cloud_folder_keeps(browser, app, tmp_path, monkeypatch):
    folder = tmp_path / 'OneDrive'
    folder.mkdir()
    app.win.path = folder / 'Hof.vrtour'
    looked_for_versions = []
    other_versions = app.r._other_versions

    def counted(project):
        found = other_versions(project)
        looked_for_versions.append(project)
        return found
    monkeypatch.setattr(app.r, '_other_versions', counted)
    anna = app_page(browser, app, 'Anna')
    drop(anna.page, [photo(f'p{k}.jpg', k, (40 + 50 * k, 90, 120)) for k in range(3)])
    save(anna.page)
    # a note next to the project says Anna has it open (the cloud folder shares it too)
    wait_for(lambda: len(list(folder.glob('.Hof.vrtour.*.editing'))) == 1)
    wait_for(lambda: looked_for_versions)  # and her editor looked for other versions, before there is one

    # OneDrive kept a second version: someone saved a point more at the same time
    project = json.loads(zipfile.ZipFile(app.win.path).read('project.json'))
    assert project['projectId'] and project['rev']
    copy = folder / 'Hof-LAPTOP.vrtour'
    shutil.copy(app.win.path, copy)
    with zipfile.ZipFile(copy) as z:
        entries = {i.filename: z.read(i) for i in z.infolist()}
    extra = dict(project['nodes'][0], id='n90', name='Schuppen', photoPath='photos/n90/schuppen.jpg', photoName='schuppen.jpg', lat=KIEL[0] + 0.0002)
    entries['photos/n90/schuppen.jpg'] = entries[project['nodes'][0]['photoPath']]
    entries['project.json'] = json.dumps({**project, 'nodes': project['nodes'] + [extra], 'rev': 'laptop1',
                                          'revs': project['revs'] + [project['rev']], 'savedBy': {'name': 'Carl'}}).encode()
    with zipfile.ZipFile(copy, 'w') as z:
        for name, data in entries.items():
            z.writestr(name, data)

    # Bea opens the project: Anna is there, and the copy is offered to be merged in
    bea = app_page(browser, app, 'Bea')
    bea.page.keyboard.press('Control+o')
    bea.page.wait_for_function(SHOWN, arg='choice-back', timeout=20000)
    assert 'Hof-LAPTOP.vrtour' in bea.page.inner_text('#choice-text') and 'Carl' in bea.page.inner_text('#choice-text')
    bea.page.click('#choice-buttons .btn.g')
    bea.page.wait_for_function("document.querySelectorAll('#node-list .nrow').length === 4")
    wait_for(lambda: app.trashed == ['Hof-LAPTOP.vrtour'], page=bea.page)  # the last step: merged and saved before it
    assert not copy.exists() and bea.page.locator('#proj-dirty').is_hidden()
    assert len(json.loads(zipfile.ZipFile(app.win.path).read('project.json'))['nodes']) == 4
    bea.page.wait_for_function("!document.getElementById('collab-people').hidden")
    assert 'Anna' in bea.page.get_attribute('#collab-people', 'title')

    # Bea's saved version reaches Anna (in the app: read entry by entry). While the cloud
    # folder is still replacing the file nothing comes in - and no photo counts as gone.
    read_entry = app.r._zip_entry

    def being_replaced(path, name):
        if name.startswith('photos/'):
            raise RuntimeError('changed')
        return read_entry(path, name)
    monkeypatch.setattr(app.r, '_zip_entry', being_replaced)
    look(anna.page)
    anna.page.wait_for_timeout(1000)
    assert anna.page.locator('#node-list .nrow').count() == 3
    monkeypatch.setattr(app.r, '_zip_entry', read_entry)
    look(anna.page)
    anna.page.wait_for_function("document.querySelectorAll('#node-list .nrow').length === 4")
    toast_with(anna.page, 'Bea saved changes')
    anna.page.click('#node-list .nrow >> nth=3')
    anna.page.wait_for_function("document.querySelector('#pano-wrap').style.display !== 'none'")

    # leaving: the notes next to the project go
    for ed in (anna, bea):
        ed.page.goto('about:blank')
        ed.context.close()
    wait_for(lambda: not list(folder.glob('.Hof.vrtour.*.editing')))


def test_presence_notes_go_stale_and_entries_are_read_whole(tmp_path):
    os.environ.setdefault('SVT_DATA_DIR', str(tmp_path / 'profile'))
    sys.path.insert(0, str(REPO))
    r = importlib.import_module('run_editor')
    project = tmp_path / 'Hof.vrtour'
    with zipfile.ZipFile(project, 'w') as z:
        z.writestr('photos/n1/a.jpg', b'JPEG')
        z.writestr('project.json', json.dumps({'projectId': 'p1', 'nodes': []}))
    assert r._zip_entry(project, 'photos/n1/a.jpg')[0] == b'JPEG'
    assert r._presence(project, 'anna123', 'Anna') == []
    assert [o['name'] for o in r._presence(project, 'bea4567', 'Bea')] == ['Anna']
    note = project.with_name('.Hof.vrtour.anna123.editing')
    stale = json.loads(note.read_text())
    note.write_text(json.dumps({**stale, 'beat': stale['beat'] - 200_000}))  # Anna's computer went to sleep
    assert r._presence(project, 'bea4567', 'Bea') == []
    assert note.exists()
    note.write_text(json.dumps({**stale, 'beat': stale['beat'] - 90_000_000}))  # left behind by a crash: removed
    r._presence(project, 'bea4567', 'Bea')
    assert not note.exists()
    shutil.copy(project, tmp_path / 'Hof-LAPTOP.vrtour')
    shutil.copy(project, tmp_path / 'Hof (1).vrtour')
    with zipfile.ZipFile(tmp_path / 'Hof-Fremd.vrtour', 'w') as z:
        z.writestr('project.json', json.dumps({'projectId': 'p2', 'nodes': []}))
    # files named otherwise aren't even opened: in a cloud folder that would download them
    shutil.copy(project, tmp_path / 'Anderes.vrtour')
    shutil.copy(project, tmp_path / 'Hofgarten.vrtour')
    opened = []
    identity = r._project_identity
    r._project_identity = lambda path: (opened.append(Path(path).name), identity(path))[1]
    try:
        assert [v['name'] for v in r._other_versions(project)] == ['Hof (1).vrtour', 'Hof-LAPTOP.vrtour']
    finally:
        r._project_identity = identity
    assert 'Anderes.vrtour' not in opened and 'Hofgarten.vrtour' not in opened

    # a note that is not one of ours does no harm
    project.with_name('.Hof.vrtour.broken1.editing').write_text('[1, 2]')
    assert r._presence(project, 'bea4567', 'Bea') == []


def test_a_backup_of_the_project_is_not_offered_as_a_second_version(browser, app, tmp_path):
    folder = tmp_path / 'OneDrive'
    folder.mkdir()
    app.win.path = folder / 'Hof.vrtour'
    anna = app_page(browser, app, 'Anna')
    three(anna.page)
    save(anna.page)
    wait_for(lambda: list(folder.glob('.Hof.vrtour.*.editing')))
    shutil.copy(app.win.path, folder / 'Hof - backup.vrtour')
    bea = app_page(browser, app, 'Bea')
    bea.page.keyboard.press('Control+o')
    bea.page.wait_for_function(f"{ROWS} === 3")
    bea.page.wait_for_function("!document.getElementById('collab-people').hidden")
    bea.page.wait_for_timeout(1500)
    assert not bea.page.evaluate("document.getElementById('choice-back').classList.contains('show')")

    # an editor in a minimized window: it doesn't take changes in, but it is still there for the others
    note = next(n for n in folder.glob('.Hof.vrtour.*.editing') if 'Bea' in n.read_text(encoding='utf-8'))
    beat = json.loads(note.read_text(encoding='utf-8'))['beat']
    bea.page.evaluate("Object.defineProperty(document, 'visibilityState', { get: () => 'hidden' })")
    time.sleep(0.05)
    bea.page.clock.set_fixed_time(time.time() + 70)
    look(bea.page)
    wait_for(lambda: json.loads(note.read_text(encoding='utf-8'))['beat'] > beat, page=bea.page)
    for ed in (anna, bea):
        ed.context.close()
