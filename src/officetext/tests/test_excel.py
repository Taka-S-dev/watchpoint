"""officetext reads what a plain openpyxl read loses from a workbook."""

from __future__ import annotations

import datetime as dt
import zipfile

from openpyxl import Workbook as new_workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.comments import Comment
from openpyxl.styles import Font

from officetext import read_workbook
from officetext.excel import display


def _cells(sheet) -> dict:
    return {c.address: c for c in sheet.cells}


def test_a_formula_never_calculated_keeps_its_formula_and_says_so(tmp_path) -> None:
    wb = new_workbook()
    wb.active["A1"] = 100
    wb.active["B1"] = "=A1*2"  # saved by a tool: no result is stored
    wb.save(tmp_path / "c.xlsx")
    book = read_workbook(tmp_path / "c.xlsx")
    b1 = _cells(book.sheets[0])["B1"]
    assert (b1.formula, b1.cached, b1.value) == ("=A1*2", False, None)
    assert book.warnings and "B1" in book.warnings[0]


def test_strikes_comments_hidden_sheets_and_the_printed_header(tmp_path) -> None:
    wb = new_workbook()
    ws = wb.active
    ws["A1"] = "旧仕様"
    ws["A1"].font = Font(strike=True)
    ws["A2"] = CellRichText([TextBlock(InlineFont(strike=True), "90"), " → 180 日"])
    ws["A3"] = "保存期間"
    ws["A3"].comment = Comment("根拠は？", "佐藤")
    ws.oddHeader.center.text = "社外秘 &P/&N"
    ws.row_dimensions[5].hidden = True
    ws["A5"] = "旧版のメモ"
    ws.merge_cells("B1:D1")
    secret = wb.create_sheet("内部")
    secret.sheet_state = "hidden"
    secret["A1"] = "原価"
    wb.save(tmp_path / "s.xlsx")
    main, hidden = read_workbook(tmp_path / "s.xlsx").sheets
    cells = _cells(main)
    assert cells["A1"].struck and cells["A1"].shown == "~~旧仕様~~"
    assert cells["A2"].shown == "~~90~~ → 180 日" and not cells["A2"].struck
    assert cells["A3"].comment and "根拠は？" in cells["A3"].comment
    assert main.header == ("社外秘 [page]/[pages]",)
    assert cells["A5"].hidden and 5 in main.hidden_rows and main.merged == ("B1:D1",)
    assert (hidden.name, hidden.state) == ("内部", "hidden")


SHAPE_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" '
    'Target="../drawings/drawing1.xml"/></Relationships>'
)
DRAWING = (
    '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    "<xdr:twoCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>2</xdr:row>"
    "<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>4</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>5</xdr:row>"
    '<xdr:rowOff>0</xdr:rowOff></xdr:to><xdr:sp><xdr:nvSpPr><xdr:cNvPr id="2" name="TextBox 1"/>'
    '<xdr:cNvSpPr txBox="1"/></xdr:nvSpPr><xdr:spPr/><xdr:txBody><a:bodyPr/><a:p><a:r><a:t>レビュー: '
    "単価の根拠を確認</a:t></a:r></a:p></xdr:txBody></xdr:sp><xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>"
)


def test_text_in_shapes_and_text_boxes_with_the_cell_they_sit_on(tmp_path) -> None:
    wb = new_workbook()
    wb.active["A1"] = "単価"
    path = tmp_path / "d.xlsx"
    wb.save(path)
    with zipfile.ZipFile(path, "a") as z:  # openpyxl cannot draw: the drawing part is added as Excel writes it
        z.writestr("xl/worksheets/_rels/sheet1.xml.rels", SHAPE_RELS)
        z.writestr("xl/drawings/drawing1.xml", DRAWING)
    shapes = read_workbook(path).sheets[0].shapes
    assert [(s.text, s.anchor, s.kind) for s in shapes] == [("レビュー: 単価の根拠を確認", "B3", "textbox")]


def test_values_as_excel_shows_them() -> None:
    assert display(0.15, "0%") == "15%"
    assert display(1234.5, "#,##0.0") == "1,234.5"
    assert display(-1200, "#,##0;(#,##0)") == "(1,200)"
    assert display(5, '0"件"') == "5件"
    assert display(dt.datetime(2026, 10, 1), "yyyy/mm/dd") == "2026/10/01"
    assert display(dt.datetime(2026, 10, 1, 9, 5), "h:mm") == "9:05"
    assert display(120.0, "General") == "120"


def test_reading_never_writes_the_file(tmp_path) -> None:
    wb = new_workbook()
    wb.active["A1"] = "x"
    path = tmp_path / "r.xlsx"
    wb.save(path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    read_workbook(path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_a_package_that_unpacks_to_too_much_is_not_read(tmp_path, monkeypatch) -> None:
    import pytest

    import officetext.package as package
    from officetext import TooLarge, read_document

    wb = new_workbook()
    wb.active["A1"] = "x"
    wb.save(tmp_path / "b.xlsx")
    monkeypatch.setattr(package, "MAX_UNPACKED_BYTES", 1000)  # as if it declared gigabytes of XML
    with pytest.raises(TooLarge, match="読みません"):
        read_workbook(tmp_path / "b.xlsx")
    from docx import Document as new_doc

    new_doc().save(tmp_path / "b.docx")
    with pytest.raises(TooLarge):
        read_document(tmp_path / "b.docx")
