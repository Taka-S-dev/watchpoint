"""Rows of cells as text: a header row found or given, and keyed rows as one record per line."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING

from openpyxl.utils import get_column_letter

from .base import AUTO_HEADER_SCAN, COLUMNS_PREFIX, SECTION_PREFIX, read_text_file

if TYPE_CHECKING:
    from ..config import Target


def cell_text(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, dt.datetime):
        if v.second:
            return v.strftime("%Y-%m-%d %H:%M:%S")
        return v.strftime("%Y-%m-%d %H:%M") if (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, dt.date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return one_line(str(v))


def one_line(s: str) -> str:
    """Keep a cell on one line and the field separator unambiguous.

    Every separator str.splitlines() splits on is replaced (a lone CR, U+2028, form feed, ...):
    a record split over two lines would no longer be compared as one record.
    """
    s = s.replace("\r\n", "\n")
    return "⏎".join(s.splitlines()).replace("|", "｜").strip()


def _find_header_row(rows: list[list], target: Target, where: str) -> int:
    if target.header_row != "auto":
        return int(target.header_row)
    wanted = [target.key] if target.key else list(target.columns or [])
    for i, row in enumerate(rows[:AUTO_HEADER_SCAN], start=1):
        cells = {cell_text(v) for v in row}
        if all(w in cells for w in wanted):
            return i
    raise ValueError(f"{where}の先頭 {AUTO_HEADER_SCAN} 行に、見出し「{'、'.join(wanted)}」のある行が見つかりません")


def table_to_lines(rows: list[list], target: Target, where: str) -> list[str]:
    """Rows (first the header) → record lines, honouring key / columns / where."""
    hr = _find_header_row(rows, target, where)
    if len(rows) < hr:
        raise ValueError(f"{where}に {hr} 行目（見出しの行）がありません")
    width = max((len(r) for r in rows[hr - 1 :]), default=0)
    head_row = list(rows[hr - 1]) + [None] * (width - len(rows[hr - 1]))
    header: list[str] = []
    auto: set[str] = set()  # names made up for blank header cells: the column's letter, as the sheet shows it
    for i, v in enumerate(head_row):
        h = cell_text(v) or f"{get_column_letter(i + 1)}列"
        h = h if h not in header else f"{h}({i + 1})"
        if not cell_text(v):
            auto.add(h)
        header.append(h)

    wanted = ([target.key] if target.key else []) + (target.columns or []) + list((target.where or {}).keys())
    missing = [c for c in dict.fromkeys(wanted) if c not in header]
    if missing:
        shown = [h for h in header if h not in auto]
        hint = ""
        found = next(
            (
                i
                for i, row in enumerate(rows[:AUTO_HEADER_SCAN], start=1)
                if set(missing) <= {cell_text(v) for v in row}
            ),
            None,
        )
        if found and found != hr:  # a title row above the table is common in shared sheets
            hint = f"。見出しは {found} 行目にあります: header_row: {found} か header_row: auto にしてください"
        raise ValueError(
            f"{where}の {hr} 行目に見出しが見つかりません: {', '.join(missing)}（ある見出し: {', '.join(shown)}）{hint}"
        )

    cols = list(target.columns or [h for h in header if h not in auto])
    if target.key and target.key not in cols:
        cols = [target.key] + cols
    # A value typed where the header is blank is still a change to report, not something to skip. Those
    # columns stay out of the column line: a first value in one would otherwise read as a column added,
    # and its value would not be listed.
    unheaded = [] if target.columns else [h for h in header if h in auto]
    where_any = {k: ([v] if isinstance(v, str) else v) for k, v in (target.where or {}).items()}

    out = [COLUMNS_PREFIX + " | ".join(c for c in cols if c != target.key)]
    seen: dict[str, int] = {}
    key_col = header.index(target.key) if target.key else -1
    taken = {cell_text(r[key_col]) for r in rows[hr:] if 0 <= key_col < len(r)} if target.key else set()
    for row_no, row in enumerate(rows[hr:], start=hr + 1):
        values = {h: cell_text(row[i]) if i < len(row) else "" for i, h in enumerate(header)}
        if not any(values.values()):
            continue
        if any(not any(w in values.get(col, "") for w in ws) for col, ws in where_any.items()):
            continue
        body = " | ".join(
            f"{c}={values.get(c, '')}" for c in cols + unheaded if c != target.key and values.get(c, "") != ""
        )
        if target.key:
            k = values.get(target.key, "") or f"R{row_no}"
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > 1:
                n = seen[k]
                while f"{k}#{n}" in taken:
                    n += 1
                seen[k] = n
                k = f"{k}#{n}"
                taken.add(k)
            out.append(f"[{target.key}={k}] {body}".rstrip())
        else:
            out.append(f"R{row_no} | {body}".rstrip(" |"))
    return out


def csv_to_text(path: Path, target: Target) -> str:
    import csv
    import io

    text = read_text_file(path)
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows = [[c if c != "" else None for c in r] for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return "\n".join([f"{SECTION_PREFIX}CSV"] + table_to_lines(rows, target, "CSV ")) + "\n"
