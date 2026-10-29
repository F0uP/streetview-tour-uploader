"""The photo tools: blurring keeps the capture time, the tripod cover, north from the sun, photos
exported with position and north, and the tour check's new findings."""
import io
import math
import zipfile
from datetime import datetime, timezone

from PIL import Image, ImageDraw

from helpers import KIEL, STEP_8M, FakeGoogle, b64, drop, jpg, run_upload

SHOWN = "id => document.getElementById(id).classList.contains('show')"
CHIPS = "[...document.querySelectorAll('#checklist-chips .st-chip')].map(c => c.className.split(' ')[1] + ': ' + c.textContent)"


def exif_of(data):
    im = Image.open(io.BytesIO(data))
    exif = im.getexif()
    return im, exif, exif.get_ifd(0x8769), exif.get_ifd(0x8825)


def open_blur(page, row=0):
    page.click(f'#node-list .nrow >> nth={row}')
    page.click('#bn-blur')
    page.wait_for_function(SHOWN, arg='blur-back')
    page.wait_for_timeout(300)


def drag_box(page):
    box = page.locator('#blur-canvas').bounding_box()
    page.mouse.move(box['x'] + box['width'] * 0.4, box['y'] + box['height'] * 0.4)
    page.mouse.down()
    page.mouse.move(box['x'] + box['width'] * 0.55, box['y'] + box['height'] * 0.55, steps=5)
    page.mouse.up()


def test_the_gpx_tool_places_photos(editor):
    """Tools → "Place photos from GPX track…" opens that tool (it ran the GPX export)."""
    page = editor.page
    drop(page, [b64('a.jpg', jpg(lat=KIEL[0], lng=KIEL[1]))])
    page.click('#btools')
    page.click('button[data-run=gpxplace]')
    page.wait_for_function(SHOWN, arg='gpx-back')


def test_blurring_keeps_time_and_position_and_never_loses_work(editor):
    page = editor.page
    google = FakeGoogle(page)
    drop(page, [b64('a.jpg', jpg(lat=KIEL[0], lng=KIEL[1], heading=90, taken='2026:05:01 14:00:00')),
                b64('b.jpg', jpg(lat=KIEL[0], lng=KIEL[1] + STEP_8M, heading=90))])

    # a box drawn, then Esc: asked first (and kept when the answer is no)
    open_blur(page)
    drag_box(page)
    assert page.inner_text('#blur-count') == '1 area(s) marked.'
    editor.answer = lambda message: False
    page.keyboard.press('Escape')
    assert any('not applied' in d for d in editor.dialogs)
    assert page.evaluate(SHOWN, 'blur-back')
    # a click beside the dialog doesn't close it
    page.mouse.click(5, 300)
    assert page.evaluate(SHOWN, 'blur-back')
    editor.answer = lambda message: True
    page.select_option('#blur-style', 'blur')
    page.click('#blur-apply')
    page.wait_for_function("!document.getElementById('blur-back').classList.contains('show')")

    # nothing to hide in the other photo: it counts as checked
    open_blur(page, 1)
    page.click('#blur-none')
    page.wait_for_function("!document.getElementById('blur-back').classList.contains('show')")
    assert 'ok: Faces & plates checked' in page.evaluate(CHIPS)

    page.click('#bupload')
    page.wait_for_timeout(500)
    run_upload(page)
    im, exif, sub, gps = exif_of(google.bodies['a.jpg'])
    assert sub.get(0x9003) == '2026:05:01 14:00:00' and sub.get(0x9011) == '+02:00'
    assert gps.get(1) == 'N' and abs(gps[2][0] + gps[2][1] / 60 + gps[2][2] / 3600 - KIEL[0]) < 1e-5
    assert abs(float(gps[17]) - 90) < 0.01
    assert google.bodies['a.jpg'].count(bytes([0xFF, 0xD8])) == 1  # no thumbnail (a second JPEG) inside
    assert b'GPano' in google.bodies['a.jpg'][:4096]  # still a 360° photo


def test_photos_export_with_position_and_north(editor):
    page = editor.page
    drop(page, [b64('a.jpg', jpg(lat=KIEL[0], lng=KIEL[1], heading=10, taken='2026:05:01 14:00:00', xmp_heading=10)),
                b64('b.png', jpg(fmt='PNG'), 'image/png')])
    page.click('#node-list .nrow >> nth=0')
    page.fill('#ni-h', '123.5')
    page.press('#ni-h', 'Enter')
    page.click('details[data-sec=position] summary')
    page.fill('#ni-lat', str(KIEL[0] + 0.001))
    page.keyboard.press('Enter')
    page.click('#bproject')
    with page.expect_download() as dl:
        page.click('button[data-run=photos]')
    z = zipfile.ZipFile(dl.value.path())
    assert sorted(z.namelist()) == ['README.txt', 'photos/a.jpg']  # the PNG can't carry it: left out
    data = z.read('photos/a.jpg')
    _, _, sub, gps = exif_of(data)
    lat = gps[2][0] + gps[2][1] / 60 + gps[2][2] / 3600
    assert abs(lat - (KIEL[0] + 0.001)) < 1e-5 and abs(float(gps[17]) - 123.5) < 0.01
    assert sub.get(0x9003) == '2026:05:01 14:00:00'
    assert b'PoseHeadingDegrees="123.5"' in data[:8192]


def test_the_tripod_is_covered(editor):
    page = editor.page
    google = FakeGoogle(page)
    im = Image.new('RGB', (2048, 1024), (70, 120, 70))  # grass
    ImageDraw.Draw(im).rectangle([0, 960, 2048, 1024], fill=(10, 10, 10))  # the tripod, a black band at the bottom
    drop(page, [b64('t.jpg', jpg(image=im, lat=KIEL[0], lng=KIEL[1], heading=0))])
    page.click('#btools')
    page.click('button[data-run=nadir]')
    page.wait_for_function(SHOWN, arg='nadir-back')
    page.check('input[name=nadir-style][value=color]')
    page.click('#nadir-go')
    page.wait_for_function("document.getElementById('nadir-status').textContent.startsWith('The tripod is covered in 1')")
    page.click('#nadir-back .close')
    page.click('#bupload')
    page.wait_for_timeout(500)
    run_upload(page)
    out = Image.open(io.BytesIO(google.bodies['t.jpg'])).convert('RGB')
    r, g, b = out.getpixel((1000, 1010))
    assert g > 90 and r < 110, (r, g, b)  # green like the ground around it, not black


def sun_reference(when, lat, lng):
    """The sun's azimuth and altitude after NOAA's short formulas (good to about half a degree)."""
    g = 2 * math.pi / 365 * (when.timetuple().tm_yday - 1 + (when.hour + when.minute / 60 - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    ha = math.radians(((when.hour * 60 + when.minute + eqtime + 4 * lng) / 4) - 180)
    phi = math.radians(lat)
    alt = math.degrees(math.asin(math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)))
    az = (math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi))) + 180) % 360
    return az, alt


def sunny_pano(sun_x, sun_y, width=2048):
    """Sky above, ground below, a small bright sun at (sun_x, sun_y)."""
    h = width // 2
    im = Image.new('RGB', (width, h), (110, 150, 205))
    d = ImageDraw.Draw(im)
    d.rectangle([0, h // 2, width, h], fill=(90, 90, 80))
    d.ellipse([sun_x - 9, sun_y - 9, sun_x + 9, sun_y + 9], fill=(255, 255, 255))
    return im


def test_north_from_the_sun(editor):
    page = editor.page
    when = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)  # 12:00 in Kiel (CEST)
    sun = page.evaluate("""async ([t, lat, lng]) => (await import('/js/sun.js')).sunPosition(new Date(t), lat, lng)""",
                        [when.timestamp() * 1000, KIEL[0], KIEL[1]])
    az, alt = sun_reference(when, *KIEL)
    assert abs(sun['azimuth'] - az) < 1 and abs(sun['altitude'] - alt) < 1, (sun, az, alt)
    # A camera facing 200°: the sun (at its azimuth) is (azimuth - 200)° to the right of the photo's centre.
    yaw = (sun['azimuth'] - 200 + 540) % 360 - 180
    W = 2048
    x, y = int((yaw + 180) / 360 * W), int((90 - sun['altitude']) / 180 * (W // 2))
    drop(page, [b64('sun.jpg', jpg(image=sunny_pano(x, y), lat=KIEL[0], lng=KIEL[1], taken='2026:06:21 12:00:00', offset='+02:00'))])
    page.click('#btools')
    page.click('button[data-run=sunnorth]')
    page.wait_for_function(SHOWN, arg='sun-back')
    page.click('#sun-go')
    page.wait_for_function("!document.getElementById('sun-actions').hidden", timeout=30000)
    assert page.inner_text('#sun-status').startswith('1 photo(s) with a clear sun')
    page.click('#sun-apply')
    page.click('#node-list .nrow >> nth=0')
    heading = float(page.input_value('#ni-h'))
    assert math.isclose(heading, 200, abs_tol=1.5), heading
    assert page.inner_text('#heading-meta') == 'from the sun'


def test_tour_check_finds_the_same_photo_twice(editor):
    page = editor.page
    drop(page, [b64('a.jpg', jpg(lat=KIEL[0], lng=KIEL[1], heading=90))])
    page.click('#node-list .nrow >> nth=0')
    page.click('#qm-copy')  # duplicated: the same photo at a second point
    page.click('#btools')
    page.click('button[data-run=check]')
    page.wait_for_function(SHOWN, arg='check-back')
    page.wait_for_function("!document.getElementById('check-list').textContent.includes('Checking')")
    assert 'The same photo as a - it would be on Google twice' in page.inner_text('#check-list')
