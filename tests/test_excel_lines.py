"""watchpoint turns what officetext reads from a workbook into lines to compare, within its limits."""

from __future__ import annotations

from openpyxl import Workbook as new_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font

from officetext import read_workbook


def _cells(sheet) -> dict:
    return {c.address: c for c in sheet.cells}


def test_watchpoint_shows_what_officetext_reads_beside_the_cells(tmp_path) -> None:
    from watchpoint.config import Target
    from watchpoint.textify import file_to_text

    wb = new_workbook()
    ws = wb.active
    ws["A1"], ws["B1"] = 100, "=A1*2"
    ws["A2"] = "旧仕様"
    ws["A2"].font = Font(strike=True)
    ws["A3"] = "保存期間"
    ws["A3"].comment = Comment("根拠は？", "佐藤")
    ws.oddHeader.center.text = "版数 6.0"
    hidden = wb.create_sheet("内部")
    hidden.sheet_state = "hidden"
    hidden["A1"] = "原価"
    wb.save(tmp_path / "w.xlsx")
    text = file_to_text(tmp_path / "w.xlsx", Target(name="w", source=str(tmp_path / "w.xlsx")))
    assert "R1 | 100 | =A1*2（未計算）" in text and "R2 | ~~旧仕様~~" in text
    assert "[ヘッダー] 版数 6.0" in text and "[コメント A3] " in text and "根拠は？" in text
    assert "## シート: 内部\n（非表示のシート）\nR1 | 原価" in text


def test_one_far_off_cell_does_not_make_a_rectangle_up_to_it(tmp_path) -> None:
    from watchpoint.textify.excel import _grid

    wb = new_workbook()
    wb.active["A1"] = "見出し"
    wb.active["XFD20"] = "遠く"
    wb.save(tmp_path / "far.xlsx")
    rows = _grid(read_workbook(tmp_path / "far.xlsx").sheets[0], formulas=False)
    assert len(rows) == 20 and len(rows[0]) == 1 and sum(map(len, rows)) == 1 + 16384  # not 20 x 16384


def test_a_header_with_a_line_break_stays_one_line(tmp_path) -> None:
    from watchpoint.config import Target
    from watchpoint.textify import file_to_text

    wb = new_workbook()
    wb.active["A1"] = "x"
    wb.active.oddHeader.center.text = "版数 6.0\n[R1] 偽の行"
    wb.save(tmp_path / "h.xlsx")
    text = file_to_text(tmp_path / "h.xlsx", Target(name="h", source=str(tmp_path / "h.xlsx")))
    assert "[ヘッダー] 版数 6.0⏎[R1] 偽の行" in text and "\n[R1]" not in text


def test_many_rows_each_far_off_are_refused_with_the_reason(tmp_path, monkeypatch) -> None:
    import pytest

    import watchpoint.textify.excel as wx

    wb = new_workbook()
    for r in range(1, 6):
        wb.active.cell(row=r, column=16384, value="遠く")  # each row would lay out 16,384 cells
    wb.save(tmp_path / "many.xlsx")
    monkeypatch.setattr(wx, "MAX_GRID", 50_000)
    with pytest.raises(ValueError, match="比べません"):
        wx._grid(read_workbook(tmp_path / "many.xlsx").sheets[0], formulas=False)


def test_the_limit_is_for_the_whole_workbook_not_each_sheet(tmp_path, monkeypatch) -> None:
    import pytest

    import watchpoint.textify.excel as wx
    from watchpoint.config import Target

    wb = new_workbook()
    for i in range(4):
        ws = wb.active if i == 0 else wb.create_sheet(f"s{i}")
        ws.cell(row=1, column=300, value="遠く")  # each sheet lays out 301 cells
    wb.save(tmp_path / "sheets.xlsx")
    monkeypatch.setattr(wx, "MAX_GRID", 1000)  # every sheet is under it, the four are not
    assert all(wx._laid_out(s) < 1000 for s in read_workbook(tmp_path / "sheets.xlsx").sheets)
    with pytest.raises(ValueError, match="ブック全体で"):
        wx.excel_to_text(tmp_path / "sheets.xlsx", Target(name="s", source=str(tmp_path / "sheets.xlsx")))
