"""A bug report to save and attach to an issue: versions, the app's log and what the
editor page sends along (its last messages and errors, a few counts).

Nothing secret goes in: not the Google sign-in, the client secret or the Places API
key, no photos and no projects. What looks like a token or key in the log is blanked,
and the user's home folder is written as ~.
"""

import io
import json
import platform
import re
import struct
import time
import zipfile
from importlib import metadata
from pathlib import Path

SECRET_PATTERNS = [
    (re.compile(r'ya29\.[\w.\-]+'), 'ya29.<removed>'),          # Google access tokens
    (re.compile(r'1//[\w\-]{20,}'), '1//<removed>'),             # Google refresh tokens
    (re.compile(r'GOCSPX-[\w\-]+'), 'GOCSPX-<removed>'),         # OAuth client secrets
    (re.compile(r'AIza[\w\-]{30,}'), 'AIza<removed>'),           # API keys
    (re.compile(r'(?i)\b(access_token|refresh_token|client_secret|id_token|api_?key|key|token|code|k)=([^&\s"\']+)'), r'\1=<removed>'),
    (re.compile(r'(?i)"(access_token|refresh_token|client_secret|id_token|token|key|api_key)"\s*:\s*"[^"]*"'), r'"\1": "<removed>"'),
    (re.compile(r'(?i)(Bearer\s+)[\w.\-]+'), r'\1<removed>'),
]
LIBRARIES = ['pywebview', 'requests', 'google-auth', 'google-auth-oauthlib',
             'Pillow', 'pystray', 'pythonnet', 'pyobjc-core', 'PyGObject']
LOG_TAIL = 1_000_000  # the last MB of the log is plenty


def scrub(text, secrets=()):
    """`text` without tokens, keys and the user's home folder."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, '<removed>')
    for rx, replacement in SECRET_PATTERNS:
        text = rx.sub(replacement, text)
    home = str(Path.home())
    if len(home) > 3:
        text = text.replace(home, '~').replace(home.replace('\\', '/'), '~')
    return text


def _libraries():
    found = []
    for name in LIBRARIES:
        try:
            found.append(f'{name} {metadata.version(name)}')
        except metadata.PackageNotFoundError:
            pass
    return ', '.join(found) or '-'


def _profile(data_dir):
    """The files in the profile folder: names and sizes only, never their contents."""
    lines = []
    for p in sorted(Path(data_dir).iterdir()) if Path(data_dir).is_dir() else []:
        if p.is_file():
            lines.append(f'  {p.name}  ({p.stat().st_size:,} bytes)')
        elif p.is_dir():
            lines.append(f'  {p.name}/')
    return lines


def build(app_version, data_dir, page, *, packaged, window, signed_in, places_key, secrets=()):
    """The report as the bytes of a zip file. page: what the editor sent (a dict)."""
    data_dir = Path(data_dir)
    summary = [
        'Streetview Tour Editor - bug report',
        f'Created: {time.strftime("%Y-%m-%d %H:%M:%S %Z")}',
        '',
        f'App version: {app_version}' + (' (installed package)' if packaged else ' (from source)'),
        f'Shown in: {"its own window" if window else "a browser"}',
        f'Python: {platform.python_version()} ({struct.calcsize("P") * 8} bit)',
        f'System: {platform.platform()} ({platform.machine()})',
        f'Libraries: {_libraries()}',
        '',
        f'Signed in to Google: {"yes" if signed_in else "no"}',
        f'Places API key set: {"yes" if places_key else "no"}',
        'The sign-in (token.json), the client secret and the API key are NOT in this report.',
        '',
        f'Profile folder: {data_dir}',
        *_profile(data_dir),
    ]
    log = data_dir / 'editor.log'
    log_text = ''
    if log.is_file():
        with open(log, 'rb') as f:
            f.seek(max(0, log.stat().st_size - LOG_TAIL))
            log_text = f.read().decode('utf-8', errors='replace')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('report.txt', scrub('\n'.join(summary) + '\n', secrets))
        z.writestr('editor-page.json', scrub(json.dumps(page, indent=2, ensure_ascii=False), secrets))
        z.writestr('editor.log', scrub(log_text, secrets) or '(no log file - the app was started from a terminal, which shows the log)\n')
    return buf.getvalue()
