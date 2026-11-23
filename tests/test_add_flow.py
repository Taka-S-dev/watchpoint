"""The guided way to add a target: look inside the source, pick a path, save and start in one step."""

from __future__ import annotations

import codecs
from pathlib import Path

from openpyxl import Workbook

import watchpoint.gui as g
from watchpoint.config import TEMPLATE, load_config
from watchpoint.peek import guess_header, peek


def _book(path: Path, sheets: dict[str, list[list]]) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)


QUESTIONS = [
    ["〇〇案件 質問表"],
    [],
    ["No.", "質問", "回答者", "状態"],
    [1, "ログは？", "佐藤", "未回答"],
    [2, "保持は？", "鈴木", "回答済"],
]


# --------------------------------------------------------------------------- looking inside


def test_header_is_found_below_a_title_row() -> None:
    rows = [["〇〇案件 質問表", ""], ["", ""], ["No.", "質問"], ["1", "ログは？"]]
    assert guess_header(rows) == 3


def test_header_is_not_a_row_of_numbers_or_a_lone_title() -> None:
    assert guess_header([["2026", "11", "23"], ["1", "2", "3"]]) is None
    assert guess_header([["見出しだけ", "列2"]]) is None  # nothing below it


def test_peek_reads_every_sheet_of_a_copy(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    _book(src, {"表紙": [["承認", "済"]], "質問一覧": QUESTIONS})
    r = peek(str(src))
    assert r["kind"] == "file" and r["file"] == "q.xlsx" and r["size"] > 0
    sheet = r["sheets"][1]
    assert sheet["name"] == "質問一覧" and sheet["header_row"] == 3
    assert sheet["rows"][2] == ["No.", "質問", "回答者", "状態"]
    assert sheet["rows"][3][0] == "1"  # cell text, as the comparison will see it


def test_peek_csv(tmp_path: Path) -> None:
    src = tmp_path / "i.csv"
    src.write_text("id,件名\n10,ログ\n11,画面\n", encoding="cp932")
    (sheet,) = peek(str(src))["sheets"]
    assert sheet["header_row"] == 1 and sheet["rows"][1] == ["10", "ログ"]


def test_peek_other_places(tmp_path: Path) -> None:
    (tmp_path / "a.docx").write_bytes(b"x")
    (tmp_path / "b.txt").write_text("y", encoding="utf-8")
    folder = peek(str(tmp_path))
    assert folder["kind"] == "folder" and folder["files"] == 2
    doc = peek(str(tmp_path / "a.docx"))
    assert doc["file"] == "a.docx" and "sheets" not in doc
    assert "見つかりません" in peek(str(tmp_path / "none.xlsx"))["error"]
    assert peek("https://example.com/") == {"kind": "web"}


def test_peek_a_broken_book_asks_to_type(tmp_path: Path) -> None:
    src = tmp_path / "bad.xlsx"
    src.write_bytes(b"not a zip")
    r = peek(str(src))
    assert "手で入れて" in r["error"] and r["file"] == "bad.xlsx"


# --------------------------------------------------------------------------- the page


def test_page_peek_strips_the_quotes_explorer_adds(gui, tmp_path: Path) -> None:
    call, _, _ = gui
    src = tmp_path / "q.xlsx"
    _book(src, {"質問一覧": QUESTIONS})
    status, r = call("/api/peek", {"source": f'"{src}"'})
    assert status == 200 and r["sheets"][0]["header_row"] == 3 and r["vars"] == {}


def test_page_browse_returns_the_chosen_path(gui) -> None:
    call, _, app = gui
    seen = {}

    def fake(folder, start):
        seen.update(folder=folder, start=start)
        return r"C:\資料\q.xlsx"

    app.picker = fake  # the window or the browser page passes its own dialog
    status, r = call("/api/browse", {"folder": False, "start": r"C:\資料"})
    assert status == 200 and r["path"] == r"C:\資料\q.xlsx" and seen == {"folder": False, "start": r"C:\資料"}
    app.picker = None  # no dialog to show: asked to write the path instead
    assert call("/api/browse", {"folder": False})[0] == 409


def test_add_saves_and_the_first_check_takes_the_baseline(gui, tmp_path: Path) -> None:
    call, _, app = gui
    src = tmp_path / "q.xlsx"
    _book(src, {"質問一覧": QUESTIONS})
    item = {"name": "質問表", "source": str(src), "key": "No.", "header_row": "auto", "where": {"回答者": "佐藤"}}
    status, r = call("/api/targets/add", {"item": item})
    assert status == 200, r
    assert r["name"] == "質問表" and r["targets"] == ["仕様", "質問表"]
    assert (app.config_path.parent / "targets.yaml.bak").exists()
    job = call.check({"names": ["質問表"]})
    assert [x["status"] for x in job["results"]] == ["初回"]


def test_add_keeps_crlf_and_bom(gui, tmp_path: Path) -> None:
    call, src, app = gui
    text = app.config_path.read_text(encoding="utf-8")
    app.config_path.write_bytes(codecs.BOM_UTF8 + text.replace("\n", "\r\n").encode("utf-8"))
    status, _ = call("/api/targets/add", {"item": {"name": "二つ目", "source": str(src), "watch": "timestamp"}})
    assert status == 200
    data = app.config_path.read_bytes()
    assert data.startswith(codecs.BOM_UTF8)
    assert data.count(b"\n") == data.count(b"\r\n")  # no bare LF slipped in
    assert "  - name: 二つ目\r\n" in data.decode("utf-8-sig")


def test_add_refuses_when_the_rest_of_the_file_is_broken(gui) -> None:
    call, src, app = gui
    app.config_path.write_text("keep: 0\ntargets: []\n", encoding="utf-8")
    before = app.config_path.read_bytes()
    status, r = call("/api/targets/add", {"item": {"name": "x", "source": str(src)}})
    assert status == 422 and "ほかの所に誤り" in r["error"]
    assert app.config_path.read_bytes() == before


def test_add_refuses_a_name_taken(gui) -> None:
    call, src, _ = gui
    status, r = call("/api/targets/add", {"item": {"name": "仕様", "source": str(src)}})
    assert status == 422 and "もうあります" in r["error"]


# --------------------------------------------------------------------------- first run


def test_template_samples_are_all_off(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "t.yaml", TEMPLATE)
    assert cfg.targets and not any(t.enabled for t in cfg.targets)


def test_gui_creates_the_config_it_needs(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from watchpoint.cli import app

    cfg = tmp_path / "new" / "targets.yaml"
    monkeypatch.setattr(g, "make_server", lambda *a, **k: (_ for _ in ()).throw(OSError("stop here")))
    result = CliRunner().invoke(app, ["gui", "-c", str(cfg), "--no-browser"])
    assert cfg.exists() and "設定ファイルを作りました" in result.output
    assert result.exit_code == 2  # the fake server refused to start; the file was made before that


# --------------------------------------------------------------------------- an Excel file on SharePoint

SP_URL = "https://contoso.sharepoint.com/sites/pj/Shared%20Documents/%E8%B3%AA%E5%95%8F%E8%A1%A8.xlsx"


def test_sharepoint_excel_is_read_through_a_copy_excel_saves(gui, tmp_path: Path, monkeypatch) -> None:
    import shutil

    import watchpoint.fetchers.office as sources

    book = tmp_path / "q.xlsx"
    _book(book, {"表紙": [["x"]], "質問一覧": QUESTIONS})
    seen = {}

    def fake_copy(source, workdir, timeout=0):
        seen["source"] = source
        dest = Path(workdir) / "original.xlsx"
        shutil.copy(book, dest)
        return dest

    monkeypatch.setattr(sources, "office_copy", fake_copy)
    call, _, _ = gui
    status, r = call("/api/peek/office", {"source": SP_URL})
    assert status == 200 and seen["source"] == SP_URL
    assert r["file"] == "質問表.xlsx" and r["sheets"][1]["header_row"] == 3


def test_sharepoint_failure_says_why(gui, monkeypatch) -> None:
    import watchpoint.fetchers.office as sources

    def fail(*a, **k):
        raise ConnectionError("Excel で開けませんでした（サインインかアクセス権を確かめてください）")

    monkeypatch.setattr(sources, "office_copy", fail)
    call, _, _ = gui
    r = call("/api/peek/office", {"source": SP_URL})[1]
    assert "サインイン" in r["error"] and "sheets" not in r


def test_a_downloaded_copy_stands_in_for_the_table(gui, tmp_path: Path) -> None:
    call, _, _ = gui
    book = tmp_path / "質問表 (1).xlsx"
    _book(book, {"質問一覧": QUESTIONS})
    r = call("/api/peek/sample", {"path": f'"{book}"'})[1]
    assert r["sample_file"] == str(book) and r["sheets"][0]["header_row"] == 3
    r = call("/api/peek/sample", {"path": str(tmp_path / "x.docx")})[1]
    assert "Excel" in r["error"]
