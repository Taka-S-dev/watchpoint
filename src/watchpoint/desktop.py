"""The page in a window of its own, with no server and no browser.

The window is WebView2 (the Edge engine that ships with Windows) driven by pywebview.
The page is handed over as a string and calls Python through pywebview's bridge, so
no port is opened, and nothing of the browser the person uses every day (its
extensions, its other tabs, its saved sign-ins) can see the page or reach it.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .app import App
from .gui import FILE_TYPES, answer, api_routes, page_html

TITLE = "watchpoint"
PREF_KEY = re.compile(r"^wp-[a-z-]{1,30}$")
PREF_VALUE_MAX = 200
NOTE_TITLE_MAX, NOTE_BODY_MAX = 60, 250  # what a Windows notification shows anyway
DEFAULT_SIZE = (1280, 860)  # at 100 % scaling; the main display's scaling multiplies it
MIN_SIZE = (420, 480)  # likewise
SCREEN_MARGIN = 24  # kept free around a window that has to shrink to fit, at 100 %
GEOMETRY_PREF = "wp-window"  # "x,y,width,height" of the window when last closed, in physical pixels


@dataclass(frozen=True)
class Area:
    """A display without its taskbar, in physical pixels, and its scaling (2.0 at 200 %)."""

    x: int
    y: int
    width: int
    height: int
    scale: float


def per_monitor_dpi() -> None:
    """Draw at each display's own scaling (Per-Monitor V2), before pywebview starts.

    pywebview makes the process "system DPI aware": one scaling for every display, so on a display
    scaled differently from the main one Windows stretched the finished picture and small text broke
    up. Windows keeps the first awareness a process sets, so pywebview's later call changes nothing,
    and pywebview, which already reads each window's own DPI, then converts sizes per display.
    """
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):  # before Windows 10 1703: system awareness stays, as before
        pass


def _work_areas() -> list[Area]:
    """Every display without its taskbar, the main display first (read after per_monitor_dpi, so in
    physical pixels for every display alike)."""
    if os.name != "nt":
        return []
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]  # fmt: skip

    user32 = ctypes.windll.user32
    found: list[tuple[bool, Area]] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
    )

    def each(monitor, _dc, _rect, _data):
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            try:
                ctypes.windll.shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
            except (AttributeError, OSError):
                pass
            w = info.rcWork
            area = Area(w.left, w.top, w.right - w.left, w.bottom - w.top, (dpi_x.value or 96) / 96)
            found.append((bool(info.dwFlags & 1), area))  # 1: the main display
        return True

    try:
        user32.EnumDisplayMonitors(None, None, callback_type(each), 0)
    except (AttributeError, OSError):
        return []
    found.sort(key=lambda a: not a[0])
    return [a for _, a in found]


def initial_geometry(saved: str | None, areas: list[Area]) -> dict:
    """What create_window takes: where the window was last closed if that still fits a display, else the
    default size, shrunk to fit the main display above its taskbar, in the middle of it.

    The window is made on the main display, and pywebview multiplies what it is given by that display's
    scaling, so physical pixels are divided by it here. A fixed 1280 x 860 at the centre of the whole
    screen ran past the taskbar on a laptop scaled to 200 %.
    """
    if not areas:
        return {"width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1], "x": None, "y": None}
    s = areas[0].scale
    if saved:
        try:
            x, y, w, h = (int(v) for v in saved.split(","))
        except ValueError:
            x = y = w = h = 0
        for a in areas:
            big_enough = w >= MIN_SIZE[0] * a.scale and h >= MIN_SIZE[1] * a.scale
            inside = a.x <= x and a.y <= y and x + w <= a.x + a.width and y + h <= a.y + a.height
            if big_enough and inside:
                return {"width": round(w / s), "height": round(h / s), "x": round(x / s), "y": round(y / s)}
    a = areas[0]
    margin = SCREEN_MARGIN * s
    w = max(MIN_SIZE[0] * s, min(DEFAULT_SIZE[0] * s, a.width - 2 * margin))
    h = max(MIN_SIZE[1] * s, min(DEFAULT_SIZE[1] * s, a.height - 2 * margin))
    x, y = a.x + (a.width - w) / 2, a.y + (a.height - h) / 2
    return {"width": round(w / s), "height": round(h / s), "x": round(x / s), "y": round(y / s)}


class DisplayScaling:
    """Keeps the window the same size to the eye when it moves to a display scaled differently.

    Drawn per display, a window keeps its physical size when it crosses to another display: 2560
    pixels wide from a 200 % display covers a 1920-pixel 100 % one. Windows tells a window so with
    WM_DPICHANGED, but the WinForms that pywebview runs on raises no event for it unless the
    application's own config file asks, so the window's DPI is compared each time it has moved.
    """

    def __init__(self) -> None:
        self.handle: int | None = None
        self.dpi = 0

    def start(self, window) -> None:
        try:
            self.handle = window.native.Handle.ToInt32()
            self.dpi = self._display_dpi()
        except Exception:  # noqa: BLE001 - not on Windows: nothing to follow
            self.handle = None

    def _display_dpi(self) -> int:
        """The DPI of the display the window is on now. The window's own DPI changes only after its move has
        been reported, so asking the window would act one move late."""
        from ctypes import wintypes

        monitor = ctypes.windll.user32.MonitorFromWindow(self.handle, 2)  # MONITOR_DEFAULTTONEAREST
        x, y = wintypes.UINT(0), wintypes.UINT(0)
        try:
            ctypes.windll.shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(x), ctypes.byref(y))
        except (AttributeError, OSError):
            return 0
        return x.value

    def moved(self) -> None:
        if self.handle is None:
            return
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        dpi = self._display_dpi()
        if not dpi or not self.dpi or dpi == self.dpi or user32.IsZoomed(self.handle):
            self.dpi = dpi or self.dpi
            return
        r = wintypes.RECT()
        if user32.GetWindowRect(self.handle, ctypes.byref(r)):
            ratio = dpi / self.dpi
            w, h = round((r.right - r.left) * ratio), round((r.bottom - r.top) * ratio)
            user32.SetWindowPos(self.handle, None, 0, 0, w, h, 0x0002 | 0x0004 | 0x0010)  # size only, no z, no focus
        self.dpi = dpi


def _physical_rect(window) -> str | None:
    """The window's place and size in physical pixels, or None while it is minimized or maximized
    (then the place it had before is kept)."""
    try:
        from ctypes import wintypes

        handle = window.native.Handle.ToInt32()
        user32 = ctypes.windll.user32
        if user32.IsIconic(handle) or user32.IsZoomed(handle):
            return None
        r = wintypes.RECT()
        if not user32.GetWindowRect(handle, ctypes.byref(r)):
            return None
        return f"{r.left},{r.top},{r.right - r.left},{r.bottom - r.top}"
    except Exception:  # noqa: BLE001 - not on Windows, or the window is already gone
        return None


class Unavailable(RuntimeError):
    """This PC cannot show the window (no pywebview, or no WebView2 runtime)."""


class Bridge:
    """What the page can call. pywebview exposes public methods only, so everything else stays private."""

    def __init__(self, app: App, prefs_path: Path | None = None, notifier: Notifier | None = None):
        self._routes = api_routes(app)
        self._prefs_path = prefs_path
        self._notifier = notifier

    def call(self, path: str, body: dict | None = None) -> dict:
        """One API call, as the server would answer it: {"status": …, "data": …}."""
        method = "GET" if body is None else "POST"
        parts = urlsplit(str(path))
        run = self._routes[method].get(parts.path)
        if run is None:
            return {"status": 404, "data": {"error": "ありません"}}
        if method == "GET":
            status, data = answer(lambda: run({k: v[0] for k, v in parse_qs(parts.query).items()}))
        else:
            status, data = answer(lambda: run(body if isinstance(body, dict) else _not_object()))
        return {"status": int(status), "data": data}

    def set_pref(self, key: str, value: str) -> bool:
        """Remember one of the page's own settings (such as checking on its own while open)."""
        if self._prefs_path is None or not PREF_KEY.match(str(key)) or not isinstance(value, str):
            return False
        prefs = load_prefs(self._prefs_path)
        prefs[key] = value[:PREF_VALUE_MAX]
        try:
            self._prefs_path.write_text(json.dumps(prefs, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            return False
        return True

    def copy(self, text: str) -> bool:
        """Put text on the clipboard. The window's page has no clipboard API of its own."""
        return isinstance(text, str) and copy_text(text)

    def notify(self, title: str, body: str, name: str | None = None) -> bool:
        """Tell the person about new changes while the window is open: a Windows notification and a taskbar flash."""
        if self._notifier is None or not isinstance(title, str) or not isinstance(body, str):
            return False
        self._notifier.show(title[:NOTE_TITLE_MAX], body[:NOTE_BODY_MAX], name if isinstance(name, str) else None)
        return True


def copy_text(text: str) -> bool:
    import win32clipboard

    for _ in range(10):  # another program may hold the clipboard for a moment
        try:
            win32clipboard.OpenClipboard()
        except Exception:  # noqa: BLE001 - pywintypes.error: busy
            time.sleep(0.05)
            continue
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            return True
        finally:
            win32clipboard.CloseClipboard()
    return False


class Notifier:
    """One notification-area icon for the window's lifetime, used to show Windows notifications.

    Clicking a notification brings the window back and opens the target it was about.
    """

    def __init__(self, window):
        self._window = window
        self._icon = None
        self._name: str | None = None
        self._lock = threading.Lock()

    def _on_ui(self, fn) -> None:
        from System import Action

        self._window.native.Invoke(Action(fn))

    def show(self, title: str, body: str, name: str | None) -> None:
        with self._lock:
            self._name = name
            self._on_ui(lambda: self._balloon(title, body))
            self._flash()

    def _balloon(self, title: str, body: str) -> None:
        from System.Drawing import SystemIcons
        from System.Windows.Forms import NotifyIcon, ToolTipIcon

        if self._icon is None:
            icon = NotifyIcon()
            icon.Icon = self._window.native.Icon or SystemIcons.Information
            icon.Text = TITLE
            icon.BalloonTipClicked += lambda *_: self._bring_back()
            icon.Click += lambda *_: self._bring_back()
            self._icon = icon
        self._icon.Visible = True
        self._icon.ShowBalloonTip(8000, title, body, ToolTipIcon.Info)

    def _bring_back(self) -> None:
        self._window.restore()
        self._window.native.Activate()
        if self._name:
            self._window.evaluate_js(f"showTab('targets'); select({json.dumps(self._name)});")

    def _flash(self) -> None:
        if os.name != "nt":
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("dwFlags", ctypes.c_uint),
                ("uCount", ctypes.c_uint), ("dwTimeout", ctypes.c_uint),
            ]  # fmt: skip

        hwnd = int(str(self._window.native.Handle))
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))  # flashes until the window is in front

    def close(self) -> None:
        if self._icon is not None:
            icon, self._icon = self._icon, None
            try:
                icon.Visible = False  # otherwise a dead icon stays in the tray until the mouse passes over it
                icon.Dispose()
            except Exception:  # noqa: BLE001, S110 - the process is ending anyway
                pass


def load_prefs(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and PREF_KEY.match(k) and isinstance(v, str)}


def _not_object():
    raise ValueError("オブジェクトではない")


def _file_types() -> tuple[str, ...]:
    # pywebview wants "説明 (*.a;*.b)"
    return tuple(f"{label} ({';'.join(patterns.split())})" for label, patterns in FILE_TYPES)


def _picker(holder: dict):
    def pick(folder: bool, start: str | None) -> str | None:
        import webview

        window = holder["window"]
        directory = ""
        if start:
            p = Path(os.path.expanduser(start.strip().strip('"')))
            directory = str(p if p.is_dir() else p.parent) if p.parent.exists() else ""
        if folder:
            chosen = window.create_file_dialog(webview.FileDialog.FOLDER, directory=directory)
        else:
            chosen = window.create_file_dialog(webview.FileDialog.OPEN, directory=directory, file_types=_file_types())
        if not chosen:
            return None
        first = chosen[0] if isinstance(chosen, list | tuple) else chosen
        return os.path.normpath(first)

    return pick


def run(config_path: Path, debug: bool = False) -> None:
    """Open the window and return when it is closed. Raises Unavailable when it cannot be shown."""
    try:
        import webview
    except ImportError as e:
        raise Unavailable(f"pywebview がありません（{e}）") from e

    per_monitor_dpi()  # before anything asks Windows about displays or makes a window
    holder: dict = {}
    app = App(config_path, picker=_picker(holder))
    prefs_path = config_path.with_name("window.json")
    bridge = Bridge(app, prefs_path)
    prefs = load_prefs(prefs_path)
    window = webview.create_window(
        TITLE,
        html=page_html(app.token, desktop=True, prefs=prefs),
        js_api=bridge,
        **initial_geometry(prefs.get(GEOMETRY_PREF), _work_areas()),
        min_size=MIN_SIZE,
        text_select=True,
        zoomable=True,
    )
    holder["window"] = window

    def remember_geometry() -> None:
        rect = _physical_rect(window)  # read while the window is still there
        if rect:
            bridge.set_pref(GEOMETRY_PREF, rect)

    window.events.closing += remember_geometry

    scaling = DisplayScaling()
    window.events.shown += lambda: scaling.start(window)
    window.events.moved += lambda *_: scaling.moved()
    notifier = Notifier(window)
    bridge._notifier = notifier
    window.events.closed += notifier.close  # not on the UI thread: it is busy closing the window
    try:
        # private mode: WebView2 keeps no cookies, cache or storage after the window closes
        webview.start(gui="edgechromium", debug=debug, private_mode=True)
    except Exception as e:  # noqa: BLE001 - no WebView2 runtime, or it failed to start
        raise Unavailable(f"窓を開けませんでした: {type(e).__name__}: {e}") from e
