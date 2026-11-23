"""A first look at a source, for the form that adds a target.

It answers what the person needs to choose how to watch something: the sheets
and first rows of a table with a guess at its header row (the page works out
key columns and filter values from these rows), a file's size and modified
time, or what a folder holds. Like a check, a file is
copied first and only the copy is read.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import tempfile
from pathlib import Path

from .config import Target
from .fetchers.file import copy_source
from .textify import CSV_SUFFIXES, EXCEL_SUFFIXES, cell_text, folder_listing, read_text_file

MAX_ROWS = 300  # rows read per sheet; enough to find the header and see what the data looks like
MAX_COLS = 40
HEADER_SCAN = 30
_NUMBERISH = re.compile(r"^[\d\s.,:/\-+%¥$]+$")


def _stamp(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def guess_header(rows: list[list[str]]) -> int | None:
    """The first row that looks like column headings: two or more distinct words, with data below it.

    A title row above the table ("〇〇案件 質問表") has one word and is passed over.
    """
    for i, row in enumerate(rows[:HEADER_SCAN], start=1):
        filled = [c for c in row if c]
        words = [c for c in filled if not _NUMBERISH.match(c)]
        if len(words) < 2 or len(words) < 0.6 * len(filled) or len(set(words)) != len(words):
            continue
        if any(any(r) for r in rows[i : i + 5]):
            return i
    return None


def _sheet(name: str, rows: list[list[str]], max_row: int | None, max_col: int | None) -> dict:
    while rows and not any(rows[-1]):
        rows.pop()
    width = max((max((i + 1 for i, c in enumerate(r) if c), default=0) for r in rows), default=0)
    rows = [(r + [""] * width)[:width] for r in rows]
    header = guess_header(rows)
    return {
        "name": name,
        "rows": rows,
        "max_row": max_row or len(rows),
        "max_col": max_col or width,
        "header_row": header,
    }


def _excel(path: Path) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = []
        for ws in wb.worksheets:
            rows = [
                [cell_text(v) for v in r]
                for r in ws.iter_rows(min_row=1, max_row=MAX_ROWS, max_col=MAX_COLS, values_only=True)
            ]
            sheets.append(_sheet(ws.title, rows, ws.max_row, ws.max_column))
        return sheets
    finally:
        wb.close()


def _csv(path: Path) -> list[dict]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    reader = csv.reader(io.StringIO(read_text_file(path)), delimiter=delimiter)
    rows = []
    for i, r in enumerate(reader):
        if i >= MAX_ROWS:
            break
        rows.append([cell_text(c) for c in r[:MAX_COLS]])
    return [_sheet("CSV", rows, None, None)]


def peek(source: str) -> dict:
    """What the form shows about a source. Problems come back as `error`, not as exceptions."""
    try:
        target = Target(name="peek", source=source)
    except ValueError as e:
        return {"error": str(e).splitlines()[-1]}
    kind = target.kind
    if kind == "folder":
        try:
            lines = folder_listing(target).splitlines()[1:]
        except OSError as e:
            return {"kind": kind, "error": str(e)}
        names = [line[len("[ファイル=") :].split("]", 1)[0] for line in lines]
        return {"kind": kind, "files": len(names), "sample": names[:8]}
    if kind != "file":
        return {"kind": kind}

    src = Path(target.source)
    try:
        st = src.stat()
    except OSError:
        return {"kind": kind, "error": f"見つかりません: {target.source}"}
    if not src.is_file():
        return {"kind": kind, "error": f"ファイルではありません: {target.source}"}
    out: dict = {"kind": kind, "file": src.name, "size": st.st_size, "modified": _stamp(st.st_mtime)}
    suffix = src.suffix.lower()
    if suffix not in EXCEL_SUFFIXES | CSV_SUFFIXES:
        return out
    try:
        with tempfile.TemporaryDirectory(prefix="watchpoint-peek-") as tmp:
            copy = copy_source(target.source, Path(tmp))  # the original is never opened in place
            out["sheets"] = _excel(copy) if suffix in EXCEL_SUFFIXES else _csv(copy)
    except Exception as e:  # noqa: BLE001 - the form falls back to typing the names
        out["error"] = f"中身を読めませんでした（{type(e).__name__}: {e}）。列やセルは手で入れてください"
    return out


def peek_office(source: str) -> dict:
    """The sheets of an Excel file on SharePoint: a hidden Excel saves a copy (with the Office sign-in), then read it.

    Slow (Excel starts), so the form asks for it with a button instead of on every keystroke.
    """
    import urllib.parse

    from .fetchers.download import download
    from .fetchers.office import office_copy

    name = urllib.parse.unquote(Path(urllib.parse.urlsplit(source).path).name) or "Excel"
    try:
        kind = Target(name="peek", source=source).kind
    except ValueError as e:
        return {"error": str(e).splitlines()[-1]}
    suffix = Path(name).suffix.lower()
    tables = EXCEL_SUFFIXES | (CSV_SUFFIXES if kind == "download" else set())
    if suffix not in tables:
        return {
            "kind": kind,
            "error": "表を出せるのは Excel（.xlsx / .xlsm）" + ("か CSV" if kind == "download" else "") + "だけです",
        }
    with tempfile.TemporaryDirectory(prefix="watchpoint-peek-") as tmp:
        try:
            # a file on the web is downloaded as every check will; one on SharePoint is saved by a hidden Excel
            copy = (
                download(Target(name="peek", source=source), Path(tmp))
                if kind == "download"
                else office_copy(source, Path(tmp))
            )
        except Exception as e:  # noqa: BLE001 - the message says what to check (sign-in, access, the URL)
            return {"kind": kind, "file": name, "error": f"{e}"}
        try:
            sheets = _excel(copy) if suffix in EXCEL_SUFFIXES else _csv(copy)
            out = {"kind": kind, "file": name, "size": copy.stat().st_size, "sheets": sheets}
        except Exception as e:  # noqa: BLE001
            return {"kind": kind, "file": name, "error": f"中身を読めませんでした（{type(e).__name__}: {e}）"}
    return out


def peek_sample(path: str) -> dict:
    """A local Excel file the person downloaded, shown in place of one that cannot be read directly."""
    p = Path(path.strip().strip('"'))
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        return {"error": "Excel（.xlsx / .xlsm）のファイルを選んでください"}
    out = peek(str(p))
    out.pop("size", None)  # the downloaded copy's, not the file on SharePoint: shown, they would mislead
    out.pop("modified", None)
    out["sample_file"] = str(p)
    return out
