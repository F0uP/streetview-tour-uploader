"""The editor itself: starts without the internet, the start screen, German."""
from urllib.parse import urlparse

from helpers import KIEL, STEP_8M, b64, drop, jpg


def test_works_offline(make_editor):
    """Only map tiles may come from the internet; everything else ships with the app."""
    outside = []

    def routes(ed):
        def handle(route):
            url = route.request.url
            if urlparse(url).hostname in ('127.0.0.1', 'localhost') or url.startswith(('blob:', 'data:')):
                return route.continue_()
            outside.append(urlparse(url).hostname)
            route.abort('internetdisconnected')
        ed.context.route('**/*', handle)

    ed = make_editor(routes=routes)
    page = ed.page
    assert page.evaluate("typeof L === 'object' && !!document.querySelector('#map.leaflet-container')")
    assert page.evaluate("document.fonts.ready.then(() => [...document.fonts].some(f => f.family.includes('Manrope') && f.status === 'loaded'))")
    drop(page, [b64(f'{c}.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + i * STEP_8M, taken=f'2026:05:01 14:0{i}:00')) for i, c in enumerate('abc')])
    assert page.inner_text('#st').startswith('Nodes 3')
    page.click('#node-list .nrow >> nth=0')
    page.wait_for_selector('#pano-wrap canvas')
    page.click('#bpreview')
    page.frame_locator('#preview-frame').locator('.psv-container canvas').first.wait_for()
    assert set(outside) <= {'a.tile.openstreetmap.org', 'b.tile.openstreetmap.org', 'c.tile.openstreetmap.org',
                            'server.arcgisonline.com', 'tile.openstreetmap.org'}, set(outside)


def test_start_screen(editor):
    page = editor.page
    assert page.is_visible('#es-import') and page.is_visible('#es-open')
    assert not page.is_visible('#es-recent')  # nothing opened yet
    assert not page.is_visible('#node-list-panel')


def test_german(editor):
    page = editor.page
    page.click('#btools')
    page.click('button[data-run=lang]')
    page.wait_for_timeout(300)
    assert page.inner_text('#btools') == 'Werkzeuge'
    assert page.inner_text('#es-import') == 'Fotos importieren…'
    page.click('#bproject')
    assert 'Projekt hinzufügen…' in page.inner_text('#badd')
    page.keyboard.press('Escape')
    drop(page, [b64('a.jpg', jpg(lat=KIEL[0], lng=KIEL[1])), b64('b.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + STEP_8M))])
    assert page.inner_text('#st') == 'Punkte 2 · Verbindungen 0'
    assert 'Knoten' not in page.inner_text('body')
