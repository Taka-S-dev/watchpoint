"""The page in its own window: the bridge that stands in for the server, and how `gui` chooses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import watchpoint.desktop as desktop
import watchpoint.gui as g
from watchpoint.app import App
from watchpoint.cli import app as cli
from watchpoint.desktop import Bridge, load_prefs
from watchpoint.gui import page_html


@pytest.fixture
def bridge(gui) -> tuple[Bridge, App]:
    _, _, app = gui  # the same config the server tests use
    return Bridge(app, app.config_path.with_name("window.json")), app


def test_bridge_answers_like_the_server(bridge) -> None:
    b, _ = bridge
    r = b.call("/api/overview")
    assert r["status"] == 200 and [t["name"] for t in r["data"]["targets"]] == ["仕様"]
    r = b.call("/api/target?name=%E4%BB%95%E6%A7%98")  # query strings work as in a URL
    assert r["status"] == 200 and r["data"]["name"] == "仕様"


def test_bridge_errors_come_back_as_status_and_message(bridge) -> None:
    b, _ = bridge
    assert b.call("/api/nothing") == {"status": 404, "data": {"error": "ありません"}}
    assert b.call("/api/overview", {}) == {"status": 404, "data": {"error": "ありません"}}  # GET only
    r = b.call("/api/config/validate", {"text": 3})
    assert r["status"] == 400 and "text は文字列" in r["data"]["error"]
    r = b.call("/api/targets/add", {"item": {"name": "仕様", "source": "C:\\x.txt"}})
    assert r["status"] == 422 and "もうあります" in r["data"]["error"]


def test_bridge_shows_only_what_the_page_needs() -> None:
    public = {n for n in dir(Bridge) if not n.startswith("_")}
    assert public == {"call", "set_pref", "copy", "notify"}  # pywebview exposes every public method


def test_prefs_are_kept_beside_the_config(bridge) -> None:
    b, app = bridge
    path = app.config_path.with_name("window.json")
    assert b.set_pref("wp-auto", "1") and load_prefs(path) == {"wp-auto": "1"}
    assert not b.set_pref("../evil", "1") and not b.set_pref("wp-auto", 1)  # type: ignore[arg-type]
    path.write_text(json.dumps({"wp-auto": "0", "other": "x", "wp-n": 5}), encoding="utf-8")
    assert load_prefs(path) == {"wp-auto": "0"}
    path.write_text("not json", encoding="utf-8")
    assert load_prefs(path) == {}


def test_notify_without_a_window_does_nothing(bridge) -> None:
    b, _ = bridge
    assert b.notify("t", "b") is False


def test_window_page_has_no_network_and_carries_its_prefs() -> None:
    html = page_html("tok", desktop=True, prefs={"wp-auto": "1</script><script>alert(1)"})
    assert 'http-equiv="Content-Security-Policy"' in html and "connect-src 'none'" in html
    assert 'const DESKTOP = "1" === "1"' in html
    assert "</script><script>alert" not in html  # a value cannot close the script
    served = page_html("tok")
    assert "http-equiv" not in served and 'const DESKTOP = "0" === "1"' in served
    assert "const PREFS = /*prefs*/null/*prefs*/;" in served


# --------------------------------------------------------------------------- choosing window or browser


def _no_server(monkeypatch) -> None:
    monkeypatch.setattr(g, "make_server", lambda *a, **k: (_ for _ in ()).throw(OSError("stop here")))


def test_gui_opens_the_window_by_default(tmp_path: Path, monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(desktop, "run", lambda path, debug=False: seen.update(path=path))
    _no_server(monkeypatch)
    cfg = tmp_path / "targets.yaml"
    result = CliRunner().invoke(cli, ["gui", "-c", str(cfg)])
    assert result.exit_code == 0 and seen == {"path": cfg}


def test_gui_falls_back_to_the_browser_when_there_is_no_window(tmp_path: Path, monkeypatch) -> None:
    def unavailable(path, debug=False):
        raise desktop.Unavailable("窓を開けませんでした: test")

    monkeypatch.setattr(desktop, "run", unavailable)
    _no_server(monkeypatch)
    result = CliRunner().invoke(cli, ["gui", "-c", str(tmp_path / "targets.yaml")])
    assert "かわりにブラウザで開きます" in result.output and result.exit_code == 2  # reached the server


def test_gui_browser_skips_the_window(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(desktop, "run", lambda *a, **k: pytest.fail("the window was opened"))
    _no_server(monkeypatch)
    result = CliRunner().invoke(cli, ["gui", "--browser", "-c", str(tmp_path / "targets.yaml")])
    assert result.exit_code == 2


def test_the_window_opens_inside_the_screen_above_the_taskbar() -> None:
    from watchpoint.desktop import Area, initial_geometry

    laptop = [Area(0, 0, 2880, 1824, 2.0)]  # 2880 x 1920 at 200 %, less the taskbar
    g = initial_geometry(None, laptop)  # in the main display's logical pixels, as pywebview takes them
    assert g["height"] * 2 <= 1824 and g["y"] >= 0 and (g["y"] + g["height"]) * 2 <= 1824
    assert g["width"] * 2 <= 2880 and g["x"] >= 0
    big = [Area(0, 0, 2560, 1400, 1.0)]
    assert initial_geometry(None, big) == {"width": 1280, "height": 860, "x": 640, "y": 270}


def test_the_window_opens_where_it_was_closed_on_a_display_scaled_differently() -> None:
    from watchpoint.desktop import Area, initial_geometry

    main = Area(0, 0, 2880, 1824, 2.0)
    side = Area(958, -1080, 1920, 1032, 1.0)  # a 100 % display above, as on the PC that showed broken text
    g = initial_geometry("1000,-1000,1600,900", [main, side])  # physical pixels, as remembered
    assert {k: v * 2 for k, v in g.items()} == {"x": 1000, "y": -1000, "width": 1600, "height": 900}
    assert initial_geometry("1000,-1000,1600,900", [main])["y"] >= 0  # that display is gone: back on the main one
    assert initial_geometry("-32000,-32000,160,28", [main, side])["width"] >= 420
    assert initial_geometry("not,a,size", [main])["x"] is not None


def test_the_process_draws_at_each_displays_own_scaling(monkeypatch) -> None:
    import ctypes
    import os

    import pytest

    from watchpoint import desktop

    if os.name != "nt":
        pytest.skip("Windows only")
    seen = []
    monkeypatch.setattr(ctypes.windll.user32, "SetProcessDpiAwarenessContext", lambda ctx: seen.append(ctx.value) or 1)
    desktop.per_monitor_dpi()
    assert seen == [
        ctypes.c_void_p(-4).value
    ]  # PER_MONITOR_AWARE_V2 (-4 as a pointer), set before pywebview's own system-wide call


def test_the_window_keeps_its_size_to_the_eye_across_displays(monkeypatch) -> None:
    """Drawn per display, a window kept its physical size: from a 200 % display onto a 100 % one it was
    twice as large to the eye and ran off the screen."""
    import ctypes
    import os

    import pytest

    from watchpoint import desktop

    if os.name != "nt":
        pytest.skip("Windows only")
    sizes = []

    class User32:
        rect = (0, 0, 2560, 1720)

        def IsZoomed(self, h):  # noqa: N802 - Windows' names
            return False

        def GetWindowRect(self, h, ref):  # noqa: N802
            r = ref._obj
            r.left, r.top, r.right, r.bottom = self.rect
            return True

        def SetWindowPos(self, h, after, x, y, w, hh, flags):  # noqa: N802
            sizes.append((w, hh))
            self.rect = (0, 0, w, hh)
            return True

    monkeypatch.setattr(ctypes.windll, "user32", User32())
    s = desktop.DisplayScaling()
    s.handle, s.dpi = 1, 192
    dpis = iter([96, 96, 192])
    monkeypatch.setattr(s, "_display_dpi", lambda: next(dpis))
    s.moved()  # onto the 100 % display
    s.moved()  # moving about on it changes nothing
    s.moved()  # back onto 200 %
    assert sizes == [(1280, 860), (2560, 1720)]
