"""officetext reads what a plain python-docx walk loses, in the notation its docstring promises."""

from __future__ import annotations

import zipfile
from pathlib import Path

from docx import Document as new_document
from docx.oxml import parse_xml

from officetext import read_document

NSDECL = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:v="urn:schemas-microsoft-com:vml"'
)


def _p(inner: str) -> str:
    return f"<w:p {NSDECL}>{inner}</w:p>"


def _run(text: str, rpr: str = "") -> str:
    return f'<w:r>{f"<w:rPr>{rpr}</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{text}</w:t></w:r>'


def _docx(path: Path, build=None, parts: dict[str, str] | None = None) -> Path:
    doc = new_document()
    if build:
        build(doc)
    doc.save(path)
    if parts:  # parts python-docx cannot write (footnotes, comments), read by officetext from the package
        with zipfile.ZipFile(path, "a") as z:
            for name, xml in parts.items():
                z.writestr(name, xml)
    return path


def _append(doc, xml: str) -> None:
    body = doc.element.body
    body.insert(len(body) - 1, parse_xml(xml))  # before the final sectPr


def test_list_numbers_come_from_numbering_not_from_the_text(tmp_path) -> None:
    def build(doc):
        doc.add_heading("概要", level=1)
        doc.add_paragraph("一つ目", style="List Number")
        doc.add_paragraph("二つ目", style="List Number")
        doc.add_paragraph("箇条", style="List Bullet")

    d = read_document(_docx(tmp_path / "a.docx", build))
    heading, first, second, bullet = d.blocks
    assert (heading.kind, heading.level, heading.text) == ("heading", 1, "概要")
    assert (first.kind, first.label, first.text) == ("list", "1.", "一つ目")
    assert (second.label, bullet.label) == ("2.", "•")


def test_header_and_footer_lines(tmp_path) -> None:
    def build(doc):
        doc.sections[0].header.paragraphs[0].text = "社外秘　版数 6.0"
        doc.sections[0].footer.paragraphs[0].text = "文書番号 SPEC-001"
        doc.add_paragraph("本文")

    d = read_document(_docx(tmp_path / "h.docx", build))
    assert d.header == ("社外秘　版数 6.0",) and d.footer == ("文書番号 SPEC-001",)
    assert [b.text for b in d.blocks] == ["本文"]


def test_content_controls_text_boxes_and_the_table_of_contents(tmp_path) -> None:
    def build(doc):
        _append(doc, f"<w:sdt {NSDECL}><w:sdtContent>{_p(_run('契約者: 山田'))}</w:sdtContent></w:sdt>")
        box = (
            "<w:pict><v:shape><v:textbox><w:txbxContent>"
            + _p(_run("レビュー: 要確認"))
            + "</w:txbxContent></v:textbox></v:shape></w:pict>"
        )
        _append(doc, _p(_run("本文") + f"<w:r>{box}</w:r>"))
        toc = '<w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/></w:docPartObj></w:sdtPr>'
        _append(doc, f'<w:sdt {NSDECL}>{toc}<w:sdtContent>{_p(_run("1 概要 .... 3"))}{_p(_run("2 範囲 .... 5"))}'
                     "</w:sdtContent></w:sdt>")  # fmt: skip

    d = read_document(_docx(tmp_path / "c.docx", build))
    assert [(b.kind, b.text) for b in d.blocks] == [
        ("paragraph", "契約者: 山田"),  # python-docx skips whatever a content control wraps
        ("paragraph", "本文"),
        ("textbox", "レビュー: 要確認"),  # apart from the paragraph it floats over
        ("toc", "2"),  # one block, not two lines of stale page numbers
    ]


def test_tracked_changes_strikes_and_comments_are_marked_where_they_are(tmp_path) -> None:
    def build(doc):
        change = (
            _run("保存期間は ")
            + '<w:del w:id="1" w:author="山田" w:date="2026-11-01T09:00:00Z"><w:r><w:delText>90</w:delText></w:r></w:del>'
            + '<w:ins w:id="2" w:author="山田" w:date="2026-11-01T09:00:00Z">' + _run("18") + _run("0") + "</w:ins>"
            + _run(" 日")
        )  # fmt: skip
        _append(doc, _p(change))
        _append(doc, _p(_run("旧仕様", "<w:strike/>") + _run("は廃止") + '<w:r><w:commentReference w:id="0"/></w:r>'))

    comments = (
        f'<w:comments {NSDECL}><w:comment w:id="0" w:author="佐藤"><w:p><w:r><w:t>いつから？</w:t></w:r></w:p>'
        "</w:comment></w:comments>"
    )
    d = read_document(_docx(tmp_path / "t.docx", build, {"word/comments.xml": comments}))
    assert [b.text for b in d.blocks] == ["保存期間は {--90--}{++180++} 日", "~~旧仕様~~は廃止{>>佐藤: いつから？<<}"]
    assert [(c.kind, c.text, c.author, c.date) for c in d.changes] == [
        ("deleted", "90", "山田", "2026-11-01"),
        ("inserted", "180", "山田", "2026-11-01"),
    ]


def test_footnotes_are_numbered_as_printed(tmp_path) -> None:
    def build(doc):
        _append(doc, _p(_run("保存期間") + '<w:r><w:footnoteReference w:id="7"/></w:r>'))

    notes = (
        f'<w:footnotes {NSDECL}><w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>'
        '<w:footnote w:id="7"><w:p><w:r><w:t>法令の改正に合わせる</w:t></w:r></w:p></w:footnote></w:footnotes>'
    )
    d = read_document(_docx(tmp_path / "f.docx", build, {"word/footnotes.xml": notes}))
    assert d.blocks[0].text == "保存期間[^1]" and d.notes == {"1": "法令の改正に合わせる"}


def test_tables_and_a_form_cell_wrapped_in_a_content_control(tmp_path) -> None:
    def build(doc):
        t = doc.add_table(rows=2, cols=2)
        t.cell(0, 0).text, t.cell(0, 1).text = "項目", "値"
        t.cell(1, 0).text = "氏名"
        tc = t.cell(1, 1)._tc
        tr = tc.getparent()
        sdt = parse_xml(f"<w:sdt {NSDECL}><w:sdtContent><w:tc><w:p>{_run('山田')}</w:p></w:tc></w:sdtContent></w:sdt>")
        tr.replace(tc, sdt)  # the answer cell of a form, inside a content control

    d = read_document(_docx(tmp_path / "tbl.docx", build))
    assert d.blocks[0].kind == "table" and d.blocks[0].rows == (("項目", "値"), ("氏名", "山田"))


def test_reading_never_writes_the_file(tmp_path) -> None:
    path = _docx(tmp_path / "r.docx", lambda doc: doc.add_paragraph("本文"))
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    read_document(path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
