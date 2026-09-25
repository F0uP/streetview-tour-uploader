"""
System tray icon for the desktop app, so it runs without a console window.

Menu: open the editor (also on double-click), open the folder holding the
Google credentials and log file, quit. Needs pystray + Pillow; when they are
missing (e.g. a bare `python run_editor.py` setup) run_editor.py falls back to
the old console mode instead.
"""

import importlib.util
import locale
import os
import subprocess
import sys

# The menu follows the Windows display language (German or English).
DE = {
    'Open editor': 'Editor öffnen',
    'Open data folder': 'Datenordner öffnen',
    'Quit': 'Beenden',
    'Version {v}': 'Version {v}',
    'Updated to version {v}.': 'Auf Version {v} aktualisiert.',
    'Running in the notification area - right-click the icon to quit.':
        'Läuft im Infobereich – Rechtsklick auf das Symbol zum Beenden.',
}


def _german():
    try:
        if sys.platform == 'win32':
            import ctypes
            return ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x07
        return (locale.getlocale()[0] or '').lower().startswith('de')
    except Exception:
        return False


GERMAN = _german()


def tr(s, **kw):
    return (DE.get(s, s) if GERMAN else s).format(**kw)


def available():
    return all(importlib.util.find_spec(m) for m in ('pystray', 'PIL'))


def run(icon_path, version, open_editor, data_dir, on_quit, message=None):
    """Show the tray icon and block until the user picks Quit (or stop() is called)."""
    import pystray
    from PIL import Image

    def open_data_folder():
        if sys.platform == 'win32':
            os.startfile(str(data_dir))
        else:
            subprocess.Popen(['xdg-open' if sys.platform.startswith('linux') else 'open', str(data_dir)])

    def quit_app(icon):
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem(tr('Open editor'), lambda icon: open_editor(), default=True),
        pystray.MenuItem(tr('Open data folder'), lambda icon: open_data_folder()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(tr('Version {v}', v=version), None, enabled=False),
        pystray.MenuItem(tr('Quit'), quit_app),
    )
    icon = pystray.Icon('StreetviewTourEditor', Image.open(icon_path), 'Streetview Tour Editor', menu)
    run.icon = icon

    def setup(icon):
        icon.visible = True
        if message:
            try:
                icon.notify(message, 'Streetview Tour Editor')
            except Exception:
                pass

    icon.run(setup=setup)


def stop():
    icon = getattr(run, 'icon', None)
    if icon:
        icon.stop()
