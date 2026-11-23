"""Jumping to where a target is: its folder (the file selected, never opened), a page, or a link set by hand."""

from __future__ import annotations

from pathlib import Path

import pytest

import watchpoint.viewing as viewing
from watchpoint.config import ConfigError, Target, load_config
from watchpoint.viewing import place_of

SP = "https://contoso.sharepoint.com/sites/pj/Shared%20Documents/q.xlsx"


def test_defaults(tmp_path: Path) -> None:
    f = tmp_path / "q.xlsx"
    f.write_bytes(b"x")
    assert place_of(Target(name="a", source=str(f))) == ("select", str(f))
    assert place_of(Target(name="b", source=str(tmp_path / "*.docx"))) == ("folder", str(tmp_path))
    assert place_of(Target(name="c", source="https://example.com/news")) == ("browser", "https://example.com/news")
    assert place_of(Target(name="d", source=SP)) is None  # would open the master in Excel on the web
    assert place_of(Target(name="e", source="https://x.example.com/issues.json")) is None


def test_a_link_set_by_hand_wins(tmp_path: Path) -> None:
    sp_folder = "https://contoso.sharepoint.com/sites/pj/Shared%20Documents/Forms/AllItems.aspx"
    assert place_of(Target(name="a", source=SP, link=sp_folder)) == ("browser", sp_folder)
    assert place_of(Target(name="b", source=SP, link=str(tmp_path))) == ("folder", str(tmp_path))
    f = tmp_path / "x.xlsx"
    assert place_of(Target(name="c", source=SP, link=str(f))) == ("select", str(f))


@pytest.mark.parametrize("bad", ["file:///C:/x.bat", "javascript:alert(1)", "ftp://x/y", "data:text/html,x"])
def test_only_web_links_and_paths(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="link は"):
        load_config(tmp_path / "t.yaml", f"targets:\n  - name: x\n    source: a.txt\n    link: '{bad}'\n")


def test_a_file_is_selected_never_started(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(viewing.subprocess, "Popen", lambda args: calls.append(args))
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ")
    viewing.open_place("select", str(exe))
    viewing.open_place("folder", str(tmp_path))
    viewing.open_place("select", str(tmp_path / "gone.xlsx"))  # renamed or deleted: its folder
    assert calls == [["explorer", f"/select,{exe}"], ["explorer", str(tmp_path)], ["explorer", str(tmp_path)]]
    with pytest.raises(FileNotFoundError):
        viewing.open_place("folder", str(exe))  # a file passed as a folder would be started by Explorer


def test_the_page_opens_the_place(gui, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(viewing.subprocess, "Popen", lambda args: calls.append(args))
    call, src, app = gui
    detail = call("/api/target?name=%E4%BB%95%E6%A7%98")[1]
    assert detail["place"] == {"label": "フォルダで開く", "where": str(src)}
    status, r = call("/api/place", {"name": "仕様"})
    assert status == 200 and calls == [["explorer", f"/select,{src}"]]
    app.config_path.write_text(f"targets:\n  - name: sp\n    source: '{SP}'\n", encoding="utf-8")
    status, r = call("/api/place", {"name": "sp"})
    assert status == 404 and "link:" in r["error"]
