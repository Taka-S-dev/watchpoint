"""The original (the master) is never opened in an app, and nothing shown to the person can be saved over it."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from openpyxl import Workbook

import watchpoint.viewing as viewing


def _book(path: Path) -> None:
    wb = Workbook()
    wb.active.append(["No.", "件名"])
    wb.active.append([1, "ログ"])
    wb.save(path)


def test_office_apps_open_a_copy_of_a_local_file(tmp_path: Path, monkeypatch) -> None:
    import shutil

    import watchpoint.fetchers.office as sources

    master = tmp_path / "share" / "質問表.xlsx"
    master.parent.mkdir()
    _book(master)
    before = (master.read_bytes(), master.stat().st_mtime_ns)
    given = {}

    def fake_excel(app, source, dest):  # what Excel is asked to open
        given["source"] = source
        shutil.copy(source, dest)

    monkeypatch.setattr(sources, "_excel_copy", fake_excel)
    pytest.importorskip("win32com.client")
    work = tmp_path / "work"
    work.mkdir()
    try:
        out = sources.office_copy(str(master), work, timeout=60)
    except ConnectionError as e:  # no Excel on this PC
        pytest.skip(str(e))
    assert Path(given["source"]).resolve() != master.resolve() and Path(given["source"]).parent.parent == work
    assert out.exists() and (master.read_bytes(), master.stat().st_mtime_ns) == before


class _Doc:
    def __init__(self, read_only: bool, log: list) -> None:
        self.ReadOnly, self.log = read_only, log

    def SaveCopyAs(self, path):  # noqa: N802 - Office's names
        self.log.append("save-copy")

    def SaveAs2(self, **kw):  # noqa: N802
        self.log.append("save-copy")

    def Close(self, SaveChanges):  # noqa: N802, N803
        self.log.append(f"close(save={SaveChanges})")


class _Office:
    def __init__(self, read_only: bool) -> None:
        self.log: list[str] = []
        doc = _Doc(read_only, self.log)
        self.Workbooks = self.Documents = type("Opener", (), {"Open": lambda _s, *a, **k: doc})()

    def Quit(self, **kw):  # noqa: N802
        self.log.append("quit")


@pytest.mark.parametrize("copy", ["_excel_copy", "_word_copy"])
def test_an_office_file_that_did_not_open_read_only_is_closed_unsaved(copy, tmp_path: Path) -> None:
    """If Office ignores ReadOnly=True (AutoSave, co-authoring), nothing is saved anywhere."""
    import watchpoint.fetchers.office as sources

    app = _Office(read_only=False)
    with pytest.raises(sources.NotReadOnly, match="読み取り専用で開けなかった"):
        getattr(sources, copy)(app, "https://example.sharepoint.com/s/x/a.xlsx", tmp_path / "original.xlsx")
    assert "save-copy" not in app.log and app.log[0] in ("close(save=False)", "close(save=0)")
    ok = _Office(read_only=True)
    getattr(sources, copy)(ok, "https://example.sharepoint.com/s/x/a.xlsx", tmp_path / "original.xlsx")
    assert ok.log[0] == "save-copy"


def test_what_is_shown_is_a_read_only_third_copy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(viewing, "view_dir", lambda: tmp_path / "view")
    kept = tmp_path / "history" / "original.xlsx"
    kept.parent.mkdir()
    _book(kept)
    first = viewing.viewing_copy(kept, "質問表", "20261123-140335")
    assert first.parent == tmp_path / "view" and "読み取り専用" in first.name and first.suffix == ".xlsx"
    assert not os.access(first, os.W_OK) and os.access(kept, os.W_OK)  # the kept copy is left as it was
    again = viewing.viewing_copy(kept, "質問表", "20261123-140335")  # a second look replaces the first
    assert again == first and not os.access(again, os.W_OK)
    os.chmod(again, stat.S_IWRITE)


def test_runnable_formats_are_only_pointed_at(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(viewing, "view_dir", lambda: tmp_path / "view")
    started, shown = [], []
    monkeypatch.setattr(os, "startfile", lambda p: started.append(p), raising=False)
    monkeypatch.setattr(viewing.subprocess, "Popen", lambda args: shown.append(args))
    for name in ("original.bat", "original.xlsm", "original.xlsx"):
        kept = tmp_path / name
        kept.write_bytes(b"x")
        viewing.show(kept, "x", "s")
    assert [Path(p).suffix for p in started] == [".xlsx"]
    assert [Path(a[1].split(",", 1)[1]).suffix for a in shown] == [".bat", ".xlsm"]


def test_the_page_opens_the_viewing_copy(gui, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(viewing, "view_dir", lambda: tmp_path / "view")
    monkeypatch.setattr(os, "startfile", lambda p: None, raising=False)
    call, _, _ = gui
    call.check({"names": ["仕様"]})
    status, r = call("/api/open", {"name": "仕様"})
    assert status == 200 and Path(r["opened"]).parent == tmp_path / "view" and "読み取り専用" in r["opened"]
