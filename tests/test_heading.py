"""North and walking direction in the editor and the preview.

Two test panoramas: a red band where north is, a blue band to the east (the way
from A to B). A was taken facing north, B (20 m east) facing south.
"""
import io
import math

from PIL import Image, ImageDraw

from helpers import b64, drop, jpg

W, H = 2048, 1024


def pano(heading, lng):
    im = Image.new('RGB', (W, H), (60, 60, 60))
    d = ImageDraw.Draw(im)
    for yaw, col in ((0 - heading, (255, 0, 0)), (90 - heading, (0, 0, 255))):
        x = int(((yaw / 360.0) + 0.5) % 1.0 * W)
        for dx in (-W, 0, W):
            d.rectangle([x + dx - 40, 0, x + dx + 40, H], fill=col)
    return jpg(image=im, lat=54.3233, lng=lng, heading=heading, xmp_heading=heading)


def colour_at_centre(page, selector):
    box = page.locator(selector).bounding_box()
    img = Image.open(io.BytesIO(page.screenshot())).convert('RGB')
    r, g, b = img.getpixel((int(box['x'] + box['width'] / 2), int(box['y'] + box['height'] / 2)))
    return 'red' if r > 180 and g < 90 else 'blue' if b > 180 and r < 90 else 'other'


def test_north_and_walking_direction(editor):
    page = editor.page
    drop(page, [b64('A.jpg', pano(0, 10.1228)), b64('B.jpg', pano(180, 10.1231))])
    for i in range(2):  # the editor opens every photo facing north
        page.click(f'#node-list .nrow >> nth={i}')
        page.wait_for_timeout(1500)
        assert colour_at_centre(page, '#pano-view') == 'red', i

    page.click('#node-list .nrow >> nth=0', modifiers=['Control'])
    page.click('#node-list .nrow >> nth=1', modifiers=['Control'])
    page.click('#multibar-connect')
    page.click('#node-list .nrow >> nth=0')
    page.click('#bpreview')
    page.wait_for_timeout(4000)
    frame = [f for f in page.frames if f != page.main_frame][0]

    def look(yaw_deg):
        frame.evaluate(f"psv.viewer.rotate({{yaw: {math.radians(yaw_deg)}, pitch: 0}})")
        page.wait_for_timeout(500)
        return colour_at_centre(page, '#preview-frame')

    assert (look(0), look(90)) == ('red', 'blue')
    first = frame.evaluate("psv.tour.getCurrentNode().id")
    other = frame.evaluate("psv.tour.getCurrentNode().links[0].nodeId")
    frame.evaluate(f"psv.tour.setCurrentNode('{other}', {{}}, psv.tour.getCurrentNode().links[0])")
    page.wait_for_timeout(3500)
    assert first != other
    # after the jump A -> B you still look the way you walked: east
    assert round(math.degrees(frame.evaluate('psv.viewer.getPosition().yaw')), 1) == 90.0
    assert colour_at_centre(page, '#preview-frame') == 'blue'
    assert look(0) == 'red'
    page.click('[data-pvnudge="180"]')
    page.wait_for_timeout(600)
    assert look(0) != 'red' and page.inner_text('#pv-align-val') == '0.0°'
    page.click('[data-pvnudge="180"]')
    page.wait_for_timeout(600)
    assert look(0) == 'red'
