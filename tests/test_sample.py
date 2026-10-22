"""The sample tour on the start screen: opens in the editor's language and is never uploaded."""
from helpers import FakeGoogle

NAMES = "[...document.querySelectorAll('#node-list .nrow-name')].map(e => e.textContent)"


def test_sample_tour_opens_and_is_not_uploaded(editor):
    page = editor.page
    google = FakeGoogle(page)
    page.click('#es-sample')
    page.wait_for_function("document.querySelectorAll('#node-list .nrow').length === 7")
    assert sorted(page.evaluate(NAMES)) == sorted(['Start', 'Bakery', 'Crossroads', 'Bus stop', 'Fountain', 'End', 'Courtyard'])
    assert page.inner_text('#proj-title') == 'Sample tour.vrtour'
    assert page.evaluate("document.querySelectorAll('.leaflet-overlay-pane path').length") >= 6  # the links

    page.click('#bupload')
    page.wait_for_timeout(500)
    assert page.inner_text('#upl-summary').startswith('The sample tour is not uploaded to Google - its photos are drawn, not real.')
    assert page.is_disabled('#upl-start')
    assert page.inner_text('#upc-missing') == '0'
    assert google.uploads == []


def test_sample_tour_in_german(make_editor):
    ed = make_editor(locale='de-DE')
    page = ed.page
    page.click('#btools')
    page.click('button[data-run=sample]')
    page.wait_for_function("document.querySelectorAll('#node-list .nrow').length === 7")
    assert 'Bäckerei' in page.evaluate(NAMES) and 'Innenhof' in page.evaluate(NAMES)
    assert page.inner_text('#proj-title') == 'Beispieltour.vrtour'
    page.click('#node-list .nrow:has-text("Start")')
    hotspots = page.evaluate("[...document.querySelectorAll('#hs-list .hs-row .t')].map(e => e.textContent)")
    assert [h.split(' ', 1)[-1] for h in hotspots] == ['Willkommen'], hotspots
