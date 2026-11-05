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

from conftest import REPO, free_port, open_editor, start
from helpers import KIEL, STEP_8M, b64, drop, jpg

SHOWN = "id => document.getElementById(id).classList.contains('show')"
NAMES = "[...document.querySelectorAll('#node-list .nrow-name')].map(e => e.textContent)"


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
    other = tmp_path / 'Anderes.vrtour'
    shutil.copy(project, other)
    unrelated = tmp_path / 'Fremd.vrtour'
    with zipfile.ZipFile(unrelated, 'w') as z:
        z.writestr('project.json', json.dumps({'projectId': 'p2', 'nodes': []}))
    assert [v['name'] for v in r._other_versions(project)] == ['Anderes.vrtour']
