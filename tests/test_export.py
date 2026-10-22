"""Exports: the website runs without the internet, CSV, viewer files."""
import functools
import http.server
import threading
import zipfile
from urllib.parse import urlparse

from helpers import KIEL, STEP_8M, b64, drop, jpg


def three_linked(page):
    drop(page, [b64(f'w{k}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + k * STEP_8M, heading=90)) for k in range(3)])
    for i in range(3):
        page.click(f'#node-list .nrow >> nth={i}', modifiers=['Control'])
    page.click('#multibar-connect')


def test_website_carries_its_libraries(editor, pw, tmp_path):
    page = editor.page
    three_linked(page)
    page.click('#bproject')
    with page.expect_download() as dl:
        page.click('button[data-run=website]')
    assert dl.value.suggested_filename == 'tour-website.zip'
    site = tmp_path / 'site'
    with zipfile.ZipFile(dl.value.path()) as z:
        names = z.namelist()
        z.extractall(site)
    assert {'lib/leaflet/leaflet.js', 'lib/three/three.module.min.js', 'lib/psv/core/index.module.js', 'lib/LICENSES.txt'} <= set(names)
    assert len([n for n in names if n.startswith('photos/')]) == 3
    index = (site / 'index.html').read_text(encoding='utf-8')
    assert 'cdn.jsdelivr' not in index

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site)))
    srv.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ctx = editor.page.context.browser.new_context()
    ctx.route('**/*', lambda r: r.continue_() if urlparse(r.request.url).hostname == '127.0.0.1' else r.abort())
    viewer = ctx.new_page()
    errors = []
    viewer.on('pageerror', lambda e: errors.append(str(e)))
    viewer.goto(f'http://127.0.0.1:{srv.server_port}/index.html')
    viewer.locator('.psv-container canvas').first.wait_for()
    assert viewer.locator('.leaflet-container').count() == 1
    ctx.close()
    srv.shutdown()
    assert not errors, errors


def test_csv_and_viewer_files(editor):
    page = editor.page
    three_linked(page)
    page.click('#bproject')
    with page.expect_download() as dl:
        page.click('#bcsv')
    rows = open(dl.value.path(), encoding='utf-8').read().strip().splitlines()
    assert rows[0] == 'filename,lat,lng,heading,connects_to,memo'
    assert rows[2].startswith('w1.jpg,') and rows[2].split(',')[4] == 'w0.jpg;w2.jpg'

    names = []
    page.on('download', lambda d: names.append(d.suggested_filename))
    page.click('#bproject')
    page.click('button[data-run=viewer]')
    page.wait_for_timeout(1500)
    assert sorted(names) == ['tour-config.json', 'tour-viewer.html']
