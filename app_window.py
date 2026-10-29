"""
The editor in its own window, without a browser around it.

The window is the operating system's own web engine, driven by pywebview:
Edge WebView2 on Windows, WKWebView on macOS and WebKitGTK on Linux. It shows
the page the local server serves. Compared with a browser tab:

- Closing the window quits the app. Nothing keeps running in the background,
  and no forgotten tab keeps talking to the server.
- The app keeps its website data (the autosaved session, settings) in its own
  profile, not in whatever browser happens to be the default.
- Everything that isn't the editor - links to Google, the release page, the
  Google sign-in - opens in the default browser.

Before the window closes, the page gets a moment to finish its autosave, and
while an upload is running the user is asked first. Size, position and the
maximized state are remembered for the next start.

AppWindow does all of this with pywebview's own API; the subclasses add what
each platform needs on top. When no window can be shown (pywebview, WebView2 or
WebKitGTK missing), window_class() returns None and run_editor.py opens the
editor in the browser instead.
"""

import ctypes
import importlib.util
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

# Background of the editor page per theme, so the window doesn't flash while it loads.
BACKGROUND = {'dark': '#0e0d12', 'light': '#f5f5f8'}
MIN_SIZE = (960, 600)
DEFAULT_GEOMETRY = {'width': 1440, 'height': 900, 'maximized': True}
# Tells the smoke test (run_editor.py --smoke-test) that the page really works.
SMOKE_CHECK = """(async () => {
  const info = await (await fetch('/api/app-info')).json();
  const ready = !!document.querySelector('#map.leaflet-container') && typeof window.__svtOpenPending === 'function';
  return (ready ? 'ok' : 'page incomplete') + ' - version ' + info.version + ', window ' + info.window;
})()"""


def window_class():
    """The app window for this computer, or None when only the browser can show the editor."""
    if importlib.util.find_spec('webview') is None:
        return None
    if sys.platform == 'win32':
        cls = WindowsAppWindow
    elif sys.platform == 'darwin':
        cls = MacAppWindow
    elif sys.platform.startswith('linux'):
        cls = GtkAppWindow
    else:
        return None
    return cls if cls.available() else None


class AppWindow:
    GUI = None

    @staticmethod
    def available():
        return False

    def __init__(self, url, title, state_file, storage_dir, icon=None, debug=False, cdp_port=None, smoke_test=False):
        self.url = url
        self.title = title
        self.state_file = Path(state_file)
        self.storage_dir = Path(storage_dir)
        self.icon = icon
        self.debug = debug
        self.cdp_port = cdp_port
        self.smoke_test = smoke_test
        self.smoke_result = None
        self.window = None
        self.busy_question = None  # set by the page while e.g. an upload runs
        self._may_close = False
        self._closing = False
        self._geometry = dict(DEFAULT_GEOMETRY)
        self._loaded_once = False
        self.dark = True  # the editor's theme; the page reports changes (set_theme)

    # ── public, callable from any thread ──────────────
    def set_busy(self, question):
        self.busy_question = question or None

    def set_theme(self, dark):
        """The editor switched between light and dark: the window frame follows."""
        self.dark = bool(dark)
        self._geometry['theme'] = 'dark' if self.dark else 'light'
        try:
            self._apply_theme()
        except Exception:
            pass

    def set_title(self, title):
        if self.window:
            self.window.set_title(title or self.title)

    def focus(self):
        """Bring the window to the front (a second launch of the app asks for this)."""
        if self.window:
            self.window.restore()
            self.window.show()

    def run_js(self, script):
        """Run a script in the page without waiting for it."""
        if self.window:
            threading.Thread(target=self.window.evaluate_js, args=(script,), daemon=True).start()

    def pick(self, kind, suggested_name='', filters=(), all_files_label='All files', multiple=False):
        """The system's open / save / folder dialog; returns a path or None
        (with multiple=True: a list of paths, empty when cancelled).

        filters: [(description, ['*.vrtour', ...]), ...]. Blocks until the user is done.
        """
        import webview
        dialog = {'open': webview.FileDialog.OPEN, 'save': webview.FileDialog.SAVE,
                  'folder': webview.FileDialog.FOLDER}[kind]
        types = []
        for desc, pats in filters:
            # pywebview only takes letters, digits and spaces in a filter's description.
            label = re.sub(r'[^\w ]+', ' ', desc).strip() or 'Files'
            types.append(f"{label} ({';'.join(pats)})")
        if kind == 'open' or not types:
            types.append(f'{all_files_label} (*.*)')
        result = self.window.create_file_dialog(dialog, save_filename=suggested_name, allow_multiple=multiple,
                                                file_types=tuple(types) if kind != 'folder' else ())
        if multiple:
            return [result] if isinstance(result, str) else list(result or ())
        if not result:
            return None
        return result if isinstance(result, str) else result[0]

    def quit(self):
        """Close without asking (the in-app update needs the files free; the page saved already)."""
        self._may_close = True
        if self.window:
            self.window.destroy()

    def run(self):
        """Show the window and block until it is closed."""
        import webview

        self._prepare()
        webview.settings['ALLOW_DOWNLOADS'] = True
        webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True
        if self.cdp_port:  # automated tests only (see run_editor.py --cdp-port)
            webview.settings['REMOTE_DEBUGGING_PORT'] = self.cdp_port

        geo = self._load_geometry()
        self._geometry = dict(geo)
        self.dark = geo.get('theme', 'dark' if self._system_prefers_dark() else 'light') == 'dark'
        self._geometry['theme'] = 'dark' if self.dark else 'light'
        self.window = webview.create_window(
            self.title, self.url,
            width=geo['width'], height=geo['height'], x=geo.get('x'), y=geo.get('y'),
            maximized=geo['maximized'], min_size=MIN_SIZE,
            background_color=BACKGROUND['dark' if self.dark else 'light'], text_select=True,
        )
        events = self.window.events
        events.before_show += self._on_before_show
        events.shown += self._on_shown
        events.loaded += self._on_loaded
        events.closing += self._on_closing
        events.resized += self._on_resized
        events.moved += self._on_moved
        events.maximized += self._on_maximized
        events.restored += self._on_restored

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        webview.start(
            gui=self.GUI, debug=self.debug,
            private_mode=False, storage_path=str(self.storage_dir),
            icon=str(self.icon) if self.icon else None,
        )

    # ── hooks for the platforms ───────────────────────
    def _prepare(self):
        """Before pywebview starts, on the main thread."""

    def _on_before_show(self):
        """The native window exists but isn't shown yet."""

    def _on_shown(self):
        pass

    def _apply_theme(self):
        """Recolour the window frame for self.dark (any thread)."""

    @staticmethod
    def _system_prefers_dark():
        """Before the page has said anything: the system's setting decides."""
        return True

    def _ask_now(self):
        """Closing while busy: True/False if the platform asked right away, None to ask from a thread."""
        return True if not self.busy_question else None

    def _hide_now(self):
        self.window.hide()

    def _read_geometry_now(self):
        """Size and position for the next start; the platforms with a better source override this."""
        return self._geometry

    @staticmethod
    def _on_a_screen(x, y, width):
        return True

    # ── page loaded: smoke test ───────────────────────
    def _on_loaded(self):
        if self.smoke_test and not self._loaded_once:
            threading.Thread(target=self._run_smoke_test, daemon=True).start()
        self._loaded_once = True

    def _run_smoke_test(self):
        time.sleep(3)  # let the page finish starting up
        done = threading.Event()
        result = {}
        self.window.evaluate_js(SMOKE_CHECK, callback=lambda value: (result.update(value=value), done.set()))
        done.wait(30)
        self.smoke_result = result.get('value')
        print(f"Smoke test: {self.smoke_result}", flush=True)
        self.quit()

    # ── closing ───────────────────────────────────────
    def _on_closing(self):
        """Runs on the window's thread; returning False keeps the window open."""
        if self._may_close:
            self._save_geometry()
            return True
        if self._closing:
            return False
        answer = self._ask_now()
        if answer is False:
            return False
        self._closing = True
        if answer:
            self._geometry = self._read_geometry_now()
            self._hide_now()  # feels instant; the page finishes its autosave meanwhile
        threading.Thread(target=self._finish_close, args=(answer is None,), daemon=True).start()
        return False

    def _finish_close(self, ask):
        if ask:
            if not self.window.create_confirmation_dialog(self.title, self.busy_question):
                self._closing = False
                return
            self.window.hide()
        started = time.time()
        done = threading.Event()
        try:
            self.window.evaluate_js('window.__svtBeforeClose ? window.__svtBeforeClose() : true',
                                    callback=lambda _: done.set())
        except Exception:
            done.set()
        saved = done.wait(10)
        print(f"Closing: autosave {'finished' if saved else 'timed out'} after {time.time() - started:.1f} s")
        self._may_close = True
        self.window.destroy()

    # ── remembered size and position ──────────────────
    def _on_resized(self, width, height):
        if not self._geometry.get('maximized') and width and height:
            self._geometry.update(width=int(width), height=int(height))

    def _on_moved(self, x, y):
        if not self._geometry.get('maximized'):
            self._geometry.update(x=int(x), y=int(y))

    def _on_maximized(self):
        self._geometry['maximized'] = True

    def _on_restored(self):
        self._geometry['maximized'] = False

    def _save_geometry(self):
        if not self._geometry:
            return
        state = dict(self._geometry, theme='dark' if self.dark else 'light')
        try:
            self.state_file.write_text(json.dumps(state), encoding='utf-8')
        except OSError:
            pass

    def _load_geometry(self):
        try:
            geo = json.loads(self.state_file.read_text(encoding='utf-8'))
            width, height = max(int(geo['width']), MIN_SIZE[0]), max(int(geo['height']), MIN_SIZE[1])
        except (OSError, ValueError, KeyError, TypeError):
            return dict(DEFAULT_GEOMETRY)
        result = {'width': width, 'height': height, 'maximized': bool(geo.get('maximized'))}
        if geo.get('theme') in ('dark', 'light'):
            result['theme'] = geo['theme']
        try:
            x, y = int(geo['x']), int(geo['y'])
        except (KeyError, TypeError, ValueError):
            return result
        if self._on_a_screen(x, y, width):
            result.update(x=x, y=y)  # else: centred (the monitor it was on is gone)
        return result


# ── Windows: Edge WebView2 ───────────────────────────
WEBVIEW2_RUNTIME = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
# Title bar = the editor's top bar, as COLORREF (0x00BBGGRR): dark #141319 / light #ffffff,
# with its text colour #ecebf1 / #17161c.
TITLE_COLOURS = {True: (0x191314, 0xF1EBEC), False: (0xFFFFFF, 0x1C1617)}
# Right-click menu: only what helps in text fields (paste a Place ID, copy a link).
CONTEXT_MENU_ITEMS = {'cut', 'copy', 'paste', 'pasteAndMatchStyle', 'selectAll', 'undo', 'redo', 'emoji', 'copyLinkLocation'}


def webview2_version():
    """Version of the installed Edge WebView2 runtime, or None."""
    if sys.platform != 'win32':
        return None
    import winreg
    places = [
        (winreg.HKEY_LOCAL_MACHINE, rf'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_RUNTIME}'),
        (winreg.HKEY_LOCAL_MACHINE, rf'SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_RUNTIME}'),
        (winreg.HKEY_CURRENT_USER, rf'SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_RUNTIME}'),
    ]
    for root, path in places:
        try:
            with winreg.OpenKey(root, path) as key:
                version, _ = winreg.QueryValueEx(key, 'pv')
        except OSError:
            continue
        if version and version != '0.0.0.0':
            return version
    return None


class WindowsAppWindow(AppWindow):
    GUI = 'edgechromium'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hwnd = None

    @staticmethod
    def available():
        return webview2_version() is not None

    def focus(self):
        hwnd = self.hwnd
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() != hwnd:
            # Windows only lets the app in front hand over the focus; otherwise
            # flash the taskbar button until the user switches to the window.
            class FLASHWINFO(ctypes.Structure):
                _fields_ = [('cbSize', ctypes.c_uint), ('hwnd', ctypes.c_void_p), ('dwFlags', ctypes.c_uint),
                            ('uCount', ctypes.c_uint), ('dwTimeout', ctypes.c_uint)]
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
            user32.FlashWindowEx(ctypes.byref(info))

    def pick(self, kind, suggested_name='', filters=(), all_files_label='All files', multiple=False):
        # pywebview would open the dialog on the calling (server) thread, which
        # Windows' dialogs refuse: they need the window's STA thread.
        from System import Func, Type
        import System.Windows.Forms as WinForms
        form = self.window.native
        result = {}

        def show():
            try:
                if kind == 'folder':
                    from webview.platforms.winforms import OpenFolderDialog  # the modern folder picker
                    paths = OpenFolderDialog.show(form)
                    result['path'] = paths[0] if paths else None
                    return
                dialog = WinForms.SaveFileDialog() if kind == 'save' else WinForms.OpenFileDialog()
                parts = [f"{desc} ({';'.join(pats)})|{';'.join(pats)}" for desc, pats in filters]
                if kind == 'open' or not parts:
                    parts.append(f'{all_files_label} (*.*)|*.*')
                dialog.Filter = '|'.join(parts)
                if kind == 'save':
                    dialog.FileName = suggested_name
                    dialog.OverwritePrompt = True
                    dialog.AddExtension = True
                elif multiple:
                    dialog.Multiselect = True
                if dialog.ShowDialog(form) == WinForms.DialogResult.OK:
                    result['path'] = dialog.FileName
                    result['paths'] = list(dialog.FileNames) if multiple else None
            except Exception as e:
                result['error'] = str(e)

        form.Invoke(Func[Type](show))
        if 'error' in result:
            raise RuntimeError(result['error'])
        if multiple:
            return result.get('paths') or ([result['path']] if result.get('path') else [])
        return result.get('path')

    def _prepare(self):
        try:
            # Taskbar grouping and pinning use the app's own identity, not python.exe's.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('StreetviewTourEditor.App')
        except (AttributeError, OSError):
            pass
        try:
            # The frame follows the editor's theme; pywebview would follow the Windows setting.
            from webview.platforms import winforms
            winforms.BrowserView.BrowserForm.is_dark_theme = lambda form: self.dark
        except Exception:
            pass

    def _on_before_show(self):
        # The form exists, WebView2 is still starting: hook in before the first page loads
        # (some settings only apply to pages loaded after they were changed).
        self.window.native.webview.CoreWebView2InitializationCompleted += self._on_webview_ready

    def _on_webview_ready(self, sender, args):
        """Runs on the window's thread, right after pywebview's own setup."""
        if not args.IsSuccess:
            return
        from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
        form = self.window.native
        core = sender.CoreWebView2
        core.Settings.AreDefaultContextMenusEnabled = True  # filtered in _on_context_menu
        core.ContextMenuRequested += self._on_context_menu
        core.DocumentTitleChanged += lambda s, _: setattr(form, 'Text', s.DocumentTitle or self.title)
        # pywebview reports every request (each map tile) to Python; nothing here
        # listens, so spare the editor that round trip.
        try:
            core.RemoveWebResourceRequestedFilter('*', CoreWebView2WebResourceContext.All)
        except Exception:
            pass

    def _on_context_menu(self, _, args):
        if self.debug:
            return  # keep "Inspect" and friends
        from Microsoft.Web.WebView2.Core import CoreWebView2ContextMenuItemKind
        items = args.MenuItems
        keep_prev_sep = False  # drop separators at the edges and next to each other
        for i in range(items.Count - 1, -1, -1):
            item = items[i]
            if item.Kind == CoreWebView2ContextMenuItemKind.Separator:
                if not keep_prev_sep:
                    items.RemoveAt(i)
                keep_prev_sep = False
            elif item.Name in CONTEXT_MENU_ITEMS:
                keep_prev_sep = True
            else:
                items.RemoveAt(i)
        if items.Count and items[0].Kind == CoreWebView2ContextMenuItemKind.Separator:
            items.RemoveAt(0)
        if items.Count == 0:
            args.Handled = True  # no menu at all instead of an empty one

    def _on_shown(self):
        self.hwnd = int(self.window.native.Handle.ToInt64())
        self._apply_theme()

    def _apply_theme(self):
        if not self.hwnd:
            return
        caption, text = TITLE_COLOURS[self.dark]
        try:
            dwm = ctypes.windll.dwmapi
            # dark mode (light or dark caption buttons), caption colour, caption text
            for attr, value in ((20, int(self.dark)), (35, caption), (36, text)):
                v = ctypes.c_int(value)
                dwm.DwmSetWindowAttribute(self.hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
        except (AttributeError, OSError):
            pass  # older Windows: default title bar

    @staticmethod
    def _system_prefers_dark():
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                return winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] == 0
        except OSError:
            return True

    def _ask_now(self):
        # Already on the window's thread here: ask right away, with the window as owner.
        if not self.busy_question:
            return True
        import System.Windows.Forms as WinForms
        answer = WinForms.MessageBox.Show(
            self.window.native, self.busy_question, self.title,
            WinForms.MessageBoxButtons.YesNo, WinForms.MessageBoxIcon.Warning,
            WinForms.MessageBoxDefaultButton.Button2)
        return answer == WinForms.DialogResult.Yes

    def _hide_now(self):
        self.window.native.Hide()

    def _read_geometry_now(self):
        try:
            import System.Windows.Forms as WinForms
            form = self.window.native
            state = form.WindowState
            bounds = form.Bounds if state == WinForms.FormWindowState.Normal else form.RestoreBounds
            scale = ctypes.windll.user32.GetDpiForWindow(self.hwnd) / 96 or 1
            return {
                'x': round(bounds.X / scale), 'y': round(bounds.Y / scale),
                'width': round(bounds.Width / scale), 'height': round(bounds.Height / scale),
                'maximized': state == WinForms.FormWindowState.Maximized,
            }
        except Exception:
            return self._geometry

    @staticmethod
    def _on_a_screen(x, y, width):
        """Is the window's title bar on a connected monitor?"""
        class POINT(ctypes.Structure):
            _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]
        try:
            user32 = ctypes.windll.user32
            scale = user32.GetDpiForSystem() / 96 or 1
            point = POINT(int((x + min(width, 200) / 2) * scale), int((y + 10) * scale))
            return bool(user32.MonitorFromPoint(point, 0))  # MONITOR_DEFAULTTONULL
        except (AttributeError, OSError):
            return True


# ── macOS: WKWebView ─────────────────────────────────
class MacAppWindow(AppWindow):
    GUI = 'cocoa'

    @staticmethod
    def available():
        return all(importlib.util.find_spec(m) for m in ('AppKit', 'WebKit'))

    def _prepare(self):
        self._set_appearance()

    def _apply_theme(self):
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._set_appearance)

    def _set_appearance(self):
        try:
            # The window chrome follows the editor's theme.
            from AppKit import NSAppearance, NSApplication
            name = 'NSAppearanceNameDarkAqua' if self.dark else 'NSAppearanceNameAqua'
            NSApplication.sharedApplication().setAppearance_(NSAppearance.appearanceNamed_(name))
        except Exception:
            pass

    @staticmethod
    def _system_prefers_dark():
        try:
            from Foundation import NSUserDefaults
            return NSUserDefaults.standardUserDefaults().stringForKey_('AppleInterfaceStyle') == 'Dark'
        except Exception:
            return True


# ── Linux: WebKitGTK ─────────────────────────────────
class GtkAppWindow(AppWindow):
    GUI = 'gtk'
    APP_ID = 'streetview-tour-editor'  # = StartupWMClass in the .desktop file: the dock shows the app's icon

    @staticmethod
    def available():
        if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            return False
        try:
            import gi
            gi.require_version('Gtk', '3.0')
        except (ImportError, ValueError):
            return False
        for version in ('4.1', '4.0'):  # the WebKitGTK versions pywebview can use
            try:
                gi.require_version('WebKit2', version)
                return True
            except ValueError:
                continue
        return False

    def _prepare(self):
        try:
            from gi.repository import GLib
            GLib.set_prgname(self.APP_ID)
            GLib.set_application_name(self.title)
        except Exception:
            pass

    def _on_before_show(self):
        self._set_gtk_theme()

    def _apply_theme(self):
        from gi.repository import GLib
        GLib.idle_add(self._set_gtk_theme)

    def _set_gtk_theme(self):
        try:
            from gi.repository import Gtk
            settings = Gtk.Settings.get_default()
            if settings:
                settings.set_property('gtk-application-prefer-dark-theme', self.dark)
        except Exception:
            pass
        return False  # (as GLib idle callback: run once)
