"""Project files: older ones are brought up to date, newer ones refused, saved ones are version 2."""
import json
import zipfile

from helpers import drop, file_item, vrtour


def test_an_older_project_gets_its_tour_and_saves_as_version_2(editor, tmp_path):
    page = editor.page
    drop(page, [file_item(vrtour(tmp_path / 'Altstadt.vrtour', 'Altstadt', 54.3233, 10.1228, start='n2'))], confirm=None)
    assert page.inner_text('#st') == 'Nodes 3 · Links 2'
    page.evaluate("window.showSaveFilePicker = undefined")
    page.click('#bproject')
    with page.expect_download() as dl:
        page.click('button[data-run=exportproject]')
    data = json.loads(zipfile.ZipFile(dl.value.path()).read('project.json'))
    assert data['version'] == 2
    assert list(data['tours'].values()) == ['Altstadt']
    assert {n['tourId'] for n in data['nodes']} == set(data['tours'])
    assert data['startIds'] == ['n2'] and data['startId'] == 'n2'


def test_a_project_from_a_newer_editor_is_refused(editor, tmp_path):
    page = editor.page
    path = vrtour(tmp_path / 'Future.vrtour', 'Future', 54.3233, 10.1228, extra={'version': 3})
    drop(page, [file_item(path)], confirm=None)
    page.wait_for_timeout(500)
    assert any('Could not open Future.vrtour: It was saved by a newer version of the editor' in d for d in editor.dialogs)
    assert not page.is_visible('#node-list-panel')
