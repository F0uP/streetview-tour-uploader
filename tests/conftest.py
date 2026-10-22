"""The editor, served by its own app on a free port with a temporary profile, in headless Chromium.

    pip install -r requirements-test.txt
    python -m playwright install chromium     (or: set SVT_BROWSER_CHANNEL=chrome)
    pytest tests
"""
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def launch_app(data, env=None):
    """The app in browser mode (no window, no browser opened) with the profile `data`; yields it, then stops it."""
    key = secrets.token_urlsafe(32)
    (data / '.editor_key').write_text(key, encoding='utf-8')  # an existing key: nothing is opened
    port = free_port()
    log = open(data / 'server.log', 'w', encoding='utf-8')
    env = {**os.environ, 'SVT_DATA_DIR': str(data), 'SVT_PORT': str(port), 'PYTHONUNBUFFERED': '1', **(env or {})}
    proc = subprocess.Popen([sys.executable, str(REPO / 'run_editor.py'), '--browser', '--no-browser', '--console', '--keep-running'],
                            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f'http://127.0.0.1:{port}'
    for _ in range(150):
        try:
            urllib.request.urlopen(f'{base}/editor.html', timeout=1)
            break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError((data / 'server.log').read_text(encoding='utf-8'))
            time.sleep(0.2)
    yield SimpleNamespace(base=base, port=port, key=key, data=data, url=f'{base}/editor.html#k={key}')
    proc.terminate()
    proc.wait(10)
    log.close()


@pytest.fixture(scope='session')
def server(tmp_path_factory):
    yield from launch_app(tmp_path_factory.mktemp('profile'))


CHANNEL = os.environ.get('SVT_BROWSER_CHANNEL') or None


@pytest.fixture(scope='session')
def pw():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope='session')
def browser(pw):
    # Software WebGL where there is no graphics card (CI): face detection needs WebGL.
    b = pw.chromium.launch(channel=CHANNEL, headless=True, args=['--enable-unsafe-swiftshader'])
    yield b
    b.close()


def open_editor(browser, server, locale='en-US', **context):
    ctx = browser.new_context(viewport={'width': 1400, 'height': 950}, locale=locale, accept_downloads=True, **context)
    ctx.add_init_script("try { localStorage.setItem('tourDone', '1') } catch {}")
    page = ctx.new_page()
    ed = SimpleNamespace(page=page, context=ctx, errors=[], dialogs=[], answer=lambda message: True)
    page.on('pageerror', lambda e: ed.errors.append(str(e)))

    def on_dialog(d):
        ed.dialogs.append(d.message)
        d.accept() if ed.answer(d.message) else d.dismiss()
    page.on('dialog', on_dialog)
    return ed


def start(ed, server):
    page = ed.page
    page.goto(server.url)
    page.wait_for_selector('#map.leaflet-container')
    page.wait_for_timeout(800)
    if page.evaluate("document.getElementById('restore-banner').classList.contains('show')"):
        page.click('#restore-no')
    return ed


@pytest.fixture
def editor(browser, server):
    """An editor page in a fresh browser profile; fails the test on any page error."""
    ed = start(open_editor(browser, server), server)
    yield ed
    errors = list(ed.errors)
    ed.context.close()
    assert not errors, errors


@pytest.fixture
def make_editor(browser, server):
    """For tests that need their own browser settings (offline, German, ...)."""
    made = []

    def make(**context):
        locale = context.pop('locale', 'en-US')
        init = context.pop('init_script', None)
        routes = context.pop('routes', None)
        ed = open_editor(browser, server, locale=locale, **context)
        if init:
            ed.context.add_init_script(init)
        if routes:
            routes(ed)
        made.append(ed)
        return start(ed, server)
    yield make
    errors = [e for ed in made for e in ed.errors]
    for ed in made:
        ed.context.close()
    assert not errors, errors
