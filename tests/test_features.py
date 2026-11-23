import os
from pathlib import Path

from openpyxl import Workbook

from watchpoint.check import Status, check_target
from watchpoint.config import Config, Target

HEADER = ["No.", "質問", "回答者", "状態", "更新日"]


def _book(path: Path, rows: list[list]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "質問一覧"
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    wb.save(path)


def _run(t: Target, data: Path, **cfg):
    return check_target(t, Config(targets=[t], **cfg), data)


def test_keyed_excel_reports_record_changes_even_when_rows_move(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    _book(src, [[1, "保存先は？", "佐藤", "未回答", "11/20"], [2, "色は？", "鈴木", "未回答", "11/20"]])
    t = Target(name="q", source=str(src), key="No.", columns=["質問", "回答者", "状態"])
    data = tmp_path / "data"
    _run(t, data)

    # A row is inserted at the top, No.1 is answered, No.2 is deleted, the date column changes everywhere.
    _book(src, [[3, "タイムアウトは？", "佐藤", "未回答", "11/22"], [1, "保存先は？", "佐藤", "回答済み", "11/22"]])
    r = _run(t, data)
    assert r.status is Status.CHANGED
    assert (r.added, r.changed, r.removed) == (1, 1, 1)
    assert "変更 [No.=1] 状態: 未回答 → 回答済み" in r.diff
    assert "追加 [No.=3] 質問=タイムアウトは？ | 回答者=佐藤 | 状態=未回答" in r.diff
    assert "更新日" not in r.diff  # not in columns


def test_where_keeps_only_matching_rows(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    _book(src, [[1, "保存先は？", "佐藤", "未回答", ""]])
    t = Target(name="q", source=str(src), key="No.", where={"回答者": "佐藤"})
    data = tmp_path / "data"
    _run(t, data)
    _book(src, [[1, "保存先は？", "佐藤", "未回答", ""], [2, "色は？", "鈴木", "未回答", ""]])
    assert _run(t, data).status is Status.UNCHANGED  # someone else's question
    _book(
        src,
        [
            [1, "保存先は？", "佐藤", "未回答", ""],
            [2, "色は？", "鈴木", "未回答", ""],
            [3, "期限は？", "佐藤", "未回答", ""],
        ],
    )
    r = _run(t, data)
    assert r.status is Status.CHANGED and r.added == 1 and "期限は？" in r.diff


def test_missing_header_is_explained(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    _book(src, [[1, "保存先は？", "佐藤", "未回答", ""]])
    r = _run(Target(name="q", source=str(src), key="番号"), tmp_path / "data")
    assert r.status is Status.FAILED and "番号" in r.message and "No." in r.message


def test_ignore_lines(tmp_path: Path) -> None:
    src = tmp_path / "spec.md"
    t = Target(name="s", source=str(src), ignore=[r"^最終更新"])
    data = tmp_path / "data"
    src.write_text("最終更新: 11/20\n本文\n", encoding="utf-8")
    _run(t, data)
    src.write_text("最終更新: 11/22\n本文\n", encoding="utf-8")
    assert _run(t, data).status is Status.UNCHANGED


def test_folder_reports_added_and_updated_files(tmp_path: Path) -> None:
    d = tmp_path / "議事録"
    d.mkdir()
    (d / "a.docx").write_bytes(b"1")
    (d / "memo.txt").write_bytes(b"x")
    t = Target(name="f", source=str(d / "*.docx"))
    data = tmp_path / "data"
    assert _run(t, data).status is Status.FIRST

    (d / "b.docx").write_bytes(b"2")
    (d / "~$a.docx").write_bytes(b"lock")  # Office lock file is ignored
    (d / "a.docx").write_bytes(b"11")
    r = _run(t, data)
    assert (r.added, r.changed, r.removed) == (1, 1, 0)
    assert "追加 [ファイル=b.docx]" in r.diff and "変更 [ファイル=a.docx] サイズ: 1 → 2" in r.diff
    assert "memo.txt" not in r.diff


def test_docx_text(tmp_path: Path) -> None:
    from docx import Document

    src = tmp_path / "spec.docx"
    doc = Document()
    doc.add_heading("ログ", level=1)
    doc.add_paragraph("保存先は D:\\log")
    tbl = doc.add_table(rows=1, cols=2)
    tbl.rows[0].cells[0].text = "保持"
    tbl.rows[0].cells[1].text = "30日"
    doc.save(src)
    t = Target(name="d", source=str(src))
    data = tmp_path / "data"
    _run(t, data)
    doc.paragraphs[1].text = "保存先は E:\\log"
    doc.save(src)
    r = _run(t, data)
    assert "-保存先は D:\\log" in r.diff and "+保存先は E:\\log" in r.diff


def test_unchanged_size_and_mtime_skip_the_copy(tmp_path: Path, monkeypatch) -> None:
    import watchpoint.fetchers.file as chk

    src = tmp_path / "a.txt"
    src.write_text("v1\n", encoding="utf-8")
    t = Target(name="a", source=str(src))
    data = tmp_path / "data"
    _run(t, data)
    calls = []
    real = chk.copy_source
    monkeypatch.setattr(chk, "copy_source", lambda *a: calls.append(a) or real(*a))
    assert _run(t, data).status is Status.UNCHANGED
    assert calls == []

    src.write_text("v2\n", encoding="utf-8")
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert _run(t, data).status is Status.CHANGED
    assert len(calls) == 1


def test_changing_settings_resets_the_baseline(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    _book(src, [[1, "保存先は？", "佐藤", "未回答", ""]])
    data = tmp_path / "data"
    _run(Target(name="q", source=str(src)), data)
    r = _run(Target(name="q", source=str(src), key="No."), data)
    assert r.status is Status.FIRST and "設定" in r.message
