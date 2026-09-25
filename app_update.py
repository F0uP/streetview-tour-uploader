"""
In-app updates for the installed Windows app.

The editor asks the local server (run_editor.py) whether GitHub has a newer
release. If so, and the app was installed with the Setup.exe, one click
downloads that release's Setup.exe, checks it against the SHA256SUMS.txt
published next to it, and runs it silently. The running app then exits so the
installer can replace its files; the installer starts the new version again
afterwards (see the [Run] section of installer/StreetviewTourEditor.iss).

Portable and source (python run_editor.py) installs can't replace themselves,
so for them the editor only links to the release page.
"""

import hashlib
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

GITHUB_REPO = "F0uP/streetview-tour-uploader"
API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"
SETUP_RE = re.compile(r"^StreetviewTourEditor-(.+)-Setup\.exe$")
SUMS_NAME = "SHA256SUMS.txt"
CHECK_TTL = 3600  # GitHub allows 60 unauthenticated API calls per hour

_cache = {'at': 0.0, 'info': None}
_lock = threading.Lock()
status = {'state': 'idle', 'progress': 0.0, 'error': None}


def parse_version(v):
    """'1.2.3' -> (1, 2, 3, 1); pre-release / dev builds sort below the release: (1, 2, 3, 0)."""
    m = re.match(r'^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$', (v or '').strip())
    if not m:
        return None
    nums = tuple(int(x or 0) for x in m.groups()[:3])
    return nums + ((0,) if m.group(4) else (1,))


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def is_installed():
    """True for a copy installed by the Setup.exe (Inno Setup leaves its uninstaller next to the app)."""
    return getattr(sys, 'frozen', False) and (Path(sys.executable).parent / "unins000.exe").exists()


def _open(url, timeout=15):
    req = Request(url, headers={
        'User-Agent': 'StreetviewTourEditor-updater',
        'Accept': 'application/vnd.github+json',
    })
    return urlopen(req, timeout=timeout)


def check(current, force=False):
    """Return info about the latest GitHub release compared with `current`."""
    with _lock:
        if not force and _cache['info'] and time.time() - _cache['at'] < CHECK_TTL:
            info = dict(_cache['info'])
        else:
            with _open(API_LATEST) as r:
                rel = json.loads(r.read())
            assets = {a['name']: a['browser_download_url'] for a in rel.get('assets', [])}
            setup = next((n for n in assets if SETUP_RE.match(n)), None)
            info = {
                'latest': (rel.get('tag_name') or '').lstrip('v'),
                'notes': (rel.get('body') or '')[:4000],
                'pageUrl': rel.get('html_url'),
                'publishedAt': rel.get('published_at'),
                'setupName': setup,
                'setupUrl': assets.get(setup) if setup else None,
                'sumsUrl': assets.get(SUMS_NAME),
            }
            _cache.update(at=time.time(), info=dict(info))
    info['current'] = current
    info['newer'] = is_newer(info['latest'], current)
    info['canInstall'] = bool(is_installed() and info['setupUrl'] and info['sumsUrl'])
    return info


def _download(url, dest, progress=None):
    if not url.startswith(DOWNLOAD_PREFIX):
        raise RuntimeError(f"Refusing to download from an unexpected URL: {url}")
    with _open(url, timeout=60) as r, open(dest, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)


def _expected_hash(sums_text, name):
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip('*') == name:
            return parts[0].lower()
    return None


def start_install(current, on_exit):
    """Download, verify and launch the latest Setup.exe in the background, then call on_exit()."""
    if status['state'] in ('downloading', 'installing'):
        return
    status.update(state='downloading', progress=0.0, error=None)

    def work():
        try:
            info = check(current, force=True)
            if not info['newer']:
                raise RuntimeError("Already on the latest version.")
            if not info['canInstall']:
                raise RuntimeError("This release has no installer to update with - download it from the release page.")
            folder = Path(tempfile.mkdtemp(prefix="StreetviewTourEditor-update-"))
            setup_path = folder / info['setupName']
            sums_path = folder / SUMS_NAME
            _download(info['sumsUrl'], sums_path)
            _download(info['setupUrl'], setup_path, progress=lambda p: status.update(progress=p))

            expected = _expected_hash(sums_path.read_text(encoding='utf-8', errors='replace'), info['setupName'])
            actual = hashlib.sha256(setup_path.read_bytes()).hexdigest()
            if not expected or expected != actual:
                raise RuntimeError("The downloaded installer failed its checksum test - not installing it.")

            status.update(state='installing', progress=1.0)
            flags = 0
            if sys.platform == 'win32':
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            # /SILENT shows only a progress window; the installer keeps the
            # previous install mode (per user / all users) and restarts the app.
            subprocess.Popen(
                [str(setup_path), '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS'],
                creationflags=flags, close_fds=True,
            )
            time.sleep(1.5)  # let the editor tab see the "installing" state before we go away
            on_exit()
        except Exception as e:
            status.update(state='error', error=str(e))

    threading.Thread(target=work, daemon=True).start()
