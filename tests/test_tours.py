"""Several tours on one map: adding projects, exporting one tour, a start per tour, names."""
import io
import json
import zipfile

from helpers import drop, file_item, vrtour

TOURS = """[...document.querySelectorAll('#tour-list .tour-row')].map(r =>
  [r.querySelector('.tn').textContent, r.querySelector('.tc').textContent, r.style.getPropertyValue('--tc')])"""
STARS = "[...document.querySelectorAll('.leaflet-marker-icon .nd.start')].map(e => e.querySelector('.nl').textContent)"


def add(page, path, choice='.btn.g'):
    drop(page, [file_item(path)], confirm=None)
    if page.evaluate("document.getElementById('choice-back').classList.contains('show')"):
        page.click(f'#choice-buttons {choice}')
        page.wait_for_timeout(1200)


def test_two_projects_on_one_map(editor, tmp_path):
    page = editor.page
    a = vrtour(tmp_path / 'Holstenstrasse.vrtour', 'Holstenstrasse', 54.3233, 10.1228, start='n2')
    b = vrtour(tmp_path / 'Rathaus.vrtour', 'Rathaus', 54.3260, 10.1300, start='n2')

    add(page, a)  # on an empty map: simply opened
    assert page.title().startswith('Holstenstrasse.vrtour')
    assert page.inner_text('#st').startswith('Nodes 3 · Links 2') and not page.is_visible('#tours')

    drop(page, [file_item(b)], confirm=None)
    assert page.inner_text('#choice-title') == 'Rathaus.vrtour'
    assert page.locator('#choice-buttons .btn').all_inner_texts() == ['Add to the map', 'Open instead', 'Cancel']
    page.click('#choice-buttons .btn.g')
    page.wait_for_timeout(1200)
    assert page.inner_text('#st').startswith('Nodes 6 · Links 4 · Tours 2')
    assert page.title() == '• Holstenstrasse + Rathaus.vrtour – Tour Editor'
    assert page.evaluate(TOURS) == [['Holstenstrasse', '3', '#8b5cf6'], ['Rathaus', '3', '#0ea5e9']]
    assert page.evaluate(STARS) == ['Holstenstrasse-2 start', 'Rathaus-2 start']
    colours = set(page.evaluate("[...document.querySelectorAll('path.leaflet-interactive')].map(p => p.getAttribute('stroke'))"))
    assert colours == {'#8b5cf6', '#0ea5e9'}

    # the same project again: asked first (and declined here)
    editor.answer = lambda message: 'already on the map' not in message
    add(page, a)
    assert any('3 of its points are already on the map' in d for d in editor.dialogs)
    assert page.inner_text('#st').startswith('Nodes 6 · Links 4 · Tours 2')

    # one tour as a website: the export asks which
    page.click('#bproject')
    page.click('button[data-run=website]')
    assert page.locator('#exp-tour option').all_inner_texts() == ['Holstenstrasse · 3 point(s)', 'Rathaus · 3 point(s)', 'Everything on the map · 6 point(s)']
    page.select_option('#exp-tour', index=1)
    with page.expect_download() as dl:
        page.click('#exp-go')
    assert dl.value.suggested_filename == 'Rathaus-website.zip'
    z = zipfile.ZipFile(io.BytesIO(open(dl.value.path(), 'rb').read()))
    assert sorted(n for n in z.namelist() if n.startswith('photos/')) == ['photos/n40-Rathaus1.jpg', 'photos/n41-Rathaus2.jpg', 'photos/n42-Rathaus3.jpg']
    assert '<title>Rathaus</title>' in z.read('index.html').decode()

    # the other one as CSV, from its row in the side panel
    page.click('#tour-list .tour-row >> nth=0 >> [data-export]')
    page.check('input[name=exp-format][value=csv]')
    with page.expect_download() as dl:
        page.click('#exp-go')
    rows = open(dl.value.path(), encoding='utf-8').read().strip().splitlines()
    assert dl.value.suggested_filename == 'Holstenstrasse.csv' and len(rows) == 4

    # a tour as a project file of its own
    page.evaluate("window.showSaveFilePicker = undefined")
    page.click('#tour-list .tour-row >> nth=1 >> [data-export]')
    page.check('input[name=exp-format][value=project]')
    with page.expect_download() as dl:
        page.click('#exp-go')
    project = json.loads(zipfile.ZipFile(dl.value.path()).read('project.json'))
    assert dl.value.suggested_filename == 'Rathaus.vrtour'
    assert (len(project['nodes']), len(project['conns']), list(project['tours'].values())) == (3, 2, ['Rathaus'])
    assert len(project['startIds']) == 1

    # moving the start stays inside the tour
    page.click('#node-list .nrow >> nth=0')
    page.click('#qm-star')
    assert page.evaluate(STARS) == ['Holstenstrasse-1 start', 'Rathaus-2 start']

    # renaming
    page.dblclick('#tour-list .tour-row >> nth=1 >> .tn')
    page.keyboard.press('Control+a')
    page.keyboard.type('Town hall')
    page.keyboard.press('Enter')
    assert page.evaluate(TOURS)[1][0] == 'Town hall'

    # the preview shows the tour of the selected point
    page.click('#node-list .nrow >> nth=4')
    page.click('#bpreview')
    page.wait_for_timeout(2500)
    assert page.inner_text('#preview-tour') == '· Town hall'
    page.keyboard.press('Escape')

    # all of it survives a restart (autosave)
    page.wait_for_timeout(2000)
    page.reload()
    page.wait_for_selector('#restore-banner.show')
    page.click('#restore-yes')
    page.wait_for_timeout(1500)
    assert page.inner_text('#st').startswith('Nodes 6 · Links 4 · Tours 2')
    assert [t[0] for t in page.evaluate(TOURS)] == ['Holstenstrasse', 'Town hall']


def test_opening_instead_replaces_the_map(editor, tmp_path):
    page = editor.page
    add(page, vrtour(tmp_path / 'A.vrtour', 'A', 54.3233, 10.1228))
    add(page, vrtour(tmp_path / 'B.vrtour', 'B', 54.3260, 10.1300), choice='.btn:nth-child(2)')
    assert page.inner_text('#st').startswith('Nodes 3 · Links 2')
    assert page.title().startswith('B.vrtour')
