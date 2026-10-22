"""North from the photos: neighbouring panoramas turned to match a reference by their horizons."""
import random

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from helpers import KIEL, STEP_8M, b64, drop, jpg

W, H = 2048, 1024


def street(seed):
    """A panorama with façade-like bands around the horizon (and some noise)."""
    rnd = random.Random(seed)
    im = Image.new('RGB', (W, H), (110, 140, 190))
    d = ImageDraw.Draw(im)
    x = 0
    while x < W:
        w = rnd.randint(25, 140)
        top = rnd.randint(250, 430)
        d.rectangle([x, top, x + w, 760], fill=tuple(rnd.randint(30, 230) for _ in range(3)))
        for _ in range(rnd.randint(0, 4)):  # windows
            wx, wy = x + rnd.randint(0, max(1, w - 12)), rnd.randint(top + 10, 700)
            d.rectangle([wx, wy, wx + 10, wy + 16], fill=tuple(rnd.randint(0, 255) for _ in range(3)))
        x += w
    d.rectangle([0, 760, W, H], fill=(70, 70, 70))
    noise = Image.effect_noise((W, H), 18).convert('RGB')
    return ImageChops.add(im, noise, scale=2.0, offset=-30).filter(ImageFilter.GaussianBlur(1))


def turned(im, degrees):
    """The same place with the camera turned: the picture moves sideways (wrapping round)."""
    return ImageChops.offset(im, round(W * degrees / 360), 0)


def test_photos_are_turned_to_match_the_reference(editor):
    page = editor.page
    base = street(7)
    photos = [
        ('A.jpg', jpg(image=base, lat=KIEL[0], lng=KIEL[1], heading=0)),                          # north is right
        ('B.jpg', jpg(image=turned(base, 30), lat=KIEL[0], lng=KIEL[1] + STEP_8M)),               # -> 330°
        ('C.jpg', jpg(image=turned(base, -45), lat=KIEL[0], lng=KIEL[1] + 2 * STEP_8M)),          # -> 45°
        ('D.jpg', jpg(image=street(99), lat=KIEL[0], lng=KIEL[1] + 3 * STEP_8M)),                 # somewhere else
    ]
    drop(page, [b64(n, d) for n, d in photos])
    for i in range(4):
        page.click(f'#node-list .nrow >> nth={i}', modifiers=['Control'])
    page.click('#multibar-connect')
    page.keyboard.press('Escape')

    page.click('#btools')
    page.click('button[data-run=northmatch]')
    assert page.locator('#mn-ref option').all_inner_texts() == ['A (0.0°)']
    page.click('#mn-go')
    page.wait_for_selector('#mn-actions:not([hidden])', timeout=60000)
    assert page.inner_text('#mn-status') == '2 photo(s) matched clearly, 1 uncertain (not ticked).'
    rows = page.locator('#mn-list .check-row')
    assert rows.count() == 3
    ticked = page.evaluate("[...document.querySelectorAll('#mn-list input')].map(i => i.checked)")
    assert ticked == [True, True, False]
    page.click('#mn-apply')

    headings = {}
    for i, name in enumerate('ABCD'):
        page.click(f'#node-list .nrow >> nth={i}')
        headings[name] = page.input_value('#ni-h')
    assert headings['A'] == '0.0' and headings['D'] == ''
    assert abs(float(headings['B']) - 330) < 1.5, headings
    assert abs(float(headings['C']) - 45) < 1.5, headings
    page.click('#node-list .nrow >> nth=1')
    assert page.inner_text('#heading-meta') == 'matched to the neighbouring photos'

    page.keyboard.press('Control+z')  # one step back
    page.click('#node-list .nrow >> nth=1')
    assert page.input_value('#ni-h') == ''
