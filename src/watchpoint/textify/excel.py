"""Excel workbooks as lines to compare: whole sheets as rows, or the cells named in `cells`, from what
officetext reads (formulas never calculated, struck text, comments, shapes, hidden sheets, header)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from officetext import Cell, Sheet, Workbook, read_workbook

from .base import MAX_CELLS, SECTION_PREFIX
from .tables import cell_text, one_line, table_to_lines

if TYPE_CHECKING:
    from ..config import Target


class CellRefError(ValueError):
    """A cell reference in `cells` cannot be read."""


_CELL = r"\$?[A-Za-z]{1,3}\$?\d+"
_CELL_REF = re.compile(rf"^(?:(?P<sheet>'(?:[^']|'')+'|[^!]+)!)?(?P<range>{_CELL}(?::{_CELL})?)$")


@dataclass(frozen=True)
class CellRef:
    sheet: str | None  # None: the first of `sheets`, or the first sheet of the book
    min_col: int
    min_row: int
    max_col: int
    max_row: int
    label: str  # the field name shown in a change: 値, or the name given in the config


def cell_refs(target: Target) -> list[CellRef]:
    """Parse `cells` (["集計!F20", "B3:D5"] or {合計: 集計!F20}) into ranges."""
    from openpyxl.utils.cell import range_boundaries

    named = isinstance(target.cells, dict)
    items = list(target.cells.items()) if named else [("値", c) for c in target.cells or []]
    out: list[CellRef] = []
    total = 0
    for label, ref in items:
        if named and re.search(r"[|=\[\]]", str(label)):  # these separate the fields of a record line
            raise CellRefError(f"cells の名前に | = [ ] は使えません「{label}」")
        m = _CELL_REF.match(str(ref).strip())
        if not m:
            raise CellRefError(f"セルの書き方が違います「{ref}」（例: B3、集計!F20、B3:D5、'月次 集計'!A1）")
        sheet = m.group("sheet")
        if sheet and sheet.startswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
        min_col, min_row, max_col, max_row = range_boundaries(m.group("range").replace("$", "").upper())
        total += (max_col - min_col + 1) * (max_row - min_row + 1)
        out.append(CellRef(sheet, min_col, min_row, max_col, max_row, str(label).strip() or "値"))
    if total > MAX_CELLS:
        raise CellRefError(too_many_cells(total))
    return out


def too_many_cells(total: int) -> str:
    """Why a long range of cells is the wrong tool, and what to write instead.

    Read by a person and by an AI editing the config alike: splitting the cells over several targets
    gets past the limit but keeps the real problem, so the message says not to.
    """
    return (
        f"cells が多すぎます（{total} 個。{MAX_CELLS} 個まで）。"
        "列を何百行も見るなら、セルではなく行ごとに見てください。cells は番地で比べるので、"
        "途中に行が挿入されると、その下のセルが全部「変わった」と出ます。"
        "見張りを分けて上限を避けても、この問題は残ります（分けないでください）。\n"
        "書き換えの例:\n"
        "  key: No.                     # 行を見分ける列（番号や ID）\n"
        "  columns: [質問, ステータス]  # 変わったら知らせる列（見出しの名前で）\n"
        "  show: [回答者, 期限]         # 変わった行と一緒に見るだけの列（任意）\n"
        "行を見分ける列が無い表なら、cells のまま見たい範囲を絞ってください"
    )


def _value(cell: Cell, formulas: bool):
    """What a cell contributes to the text: the value as stored (so the lines read as they always
    have), its formula when `formulas` is set, a formula never calculated as itself, and struck text
    marked ~~ as officetext marks it."""
    if formulas and cell.formula:
        return cell.formula
    if cell.formula and not cell.cached:
        return f"{cell.formula}（未計算）"  # openpyxl alone reads this as an empty cell
    if cell.struck:
        return f"~~{cell_text(cell.value)}~~"
    if isinstance(cell.value, str) and cell.shown != cell.value:
        return cell.shown  # some words struck
    return cell.value


MAX_GRID = 5_000_000  # cells laid out from A1, for the whole workbook; 20,000 rows of 50 columns are 1,000,000


def _laid_out(sheet: Sheet) -> int:
    """How many cells _grid lays out for a sheet: every row's cells up to its last, and the rows."""
    widths: dict[int, int] = {}
    for c in sheet.cells:
        widths[c.row] = max(widths.get(c.row, 0), c.col)
    return sum(widths.values()) + max(widths, default=0)


def _check_size(sheets: list[Sheet]) -> None:
    """Refuse, with the reason, a workbook whose sheets would lay out more than MAX_GRID cells in all:
    rows that each hold one cell far to the right lay out every cell before it, and many sheets just
    under a limit of their own would add up."""
    total = 0
    for sheet in sheets:
        total += _laid_out(sheet)
        if total > MAX_GRID:
            raise ValueError(
                f"A1 から並べると、シート「{sheet.name}」までで {total:,} 個のセルになるので比べません"
                f"（ブック全体で {MAX_GRID:,} 個まで）。離れた場所に値があれば、sheets や cells で見る範囲を絞ってください"
            )


def _grid(sheet: Sheet, formulas: bool) -> list[list]:
    """The sheet as rows of values from A1, each only as long as its last cell: one value far off in
    XFD1048576 must not make a rectangle of every cell up to it (table_to_lines reads short rows)."""
    if not sheet.cells:
        return []
    _check_size([sheet])
    rows: list[list] = [[] for _ in range(max(c.row for c in sheet.cells))]
    for c in sheet.cells:
        row = rows[c.row - 1]
        if len(row) < c.col:
            row.extend([None] * (c.col - len(row)))
        row[c.col - 1] = _value(c, formulas)
    return rows


def _state(sheet: Sheet) -> list[str]:
    """A line under the sheet's heading when it is hidden: hidden sheets often hold the real inputs."""
    if sheet.state == "visible":
        return []
    return ["（非表示のシート）" if sheet.state == "hidden" else "（完全に非表示のシート）"]


def _extras(sheet: Sheet) -> list[str]:
    """What sits beside the cells: the printed header and footer, comments, and the text of shapes
    and text boxes."""
    out = []
    if sheet.header:
        out.append("[ヘッダー] " + " | ".join(one_line(p) for p in sheet.header))
    if sheet.footer:
        out.append("[フッター] " + " | ".join(one_line(p) for p in sheet.footer))
    out += [f"[コメント {c.address}] {one_line(c.comment)}" for c in sheet.cells if c.comment]
    out += [
        f"[{'テキストボックス' if s.kind == 'textbox' else '図形'} {s.anchor}] {one_line(s.text)}".replace(" ]", "]")
        for s in sheet.shapes
    ]
    return out


def cells_to_text(book: Workbook, target: Target) -> str:
    """One record per cell: ``[セル=F20] 合計=120``, grouped by sheet."""
    from openpyxl.utils.cell import get_column_letter

    names = [s.name for s in book.sheets]
    default = (target.sheets or names)[0]
    by_name = {s.name: {(c.row, c.col): c for c in s.cells} for s in book.sheets}
    by_sheet: dict[str, dict[str, str]] = {}
    for ref in cell_refs(target):
        name = ref.sheet or default
        if name not in by_name:
            raise ValueError(f"シートが見つかりません: {name}（あるシート: {', '.join(names)}）")
        lines = by_sheet.setdefault(name, {})
        for r in range(ref.min_row, ref.max_row + 1):
            for c in range(ref.min_col, ref.max_col + 1):
                addr = f"{get_column_letter(c)}{r}"
                cell = by_name[name].get((r, c))
                value = cell_text(_value(cell, target.formulas)) if cell else ""
                lines.setdefault(addr, f"[セル={addr}] {ref.label}={value}" if value else f"[セル={addr}]")
    out: list[str] = []
    for name, lines in by_sheet.items():
        out += [f"{SECTION_PREFIX}シート: {name}", *lines.values(), ""]
    return "\n".join(out)


def excel_to_text(path: Path, target: Target) -> str:
    book = read_workbook(path)
    if target.cells:
        return cells_to_text(book, target)
    names = [s.name for s in book.sheets]
    wanted = names
    if target.sheets:
        missing = [s for s in target.sheets if s not in names]
        if missing:
            raise ValueError(f"シートが見つかりません: {', '.join(missing)}（あるシート: {', '.join(names)}）")
        wanted = [s for s in names if s in target.sheets]
    out: list[str] = []
    chosen = [s for s in book.sheets if s.name in wanted]
    _check_size(chosen)  # for the whole workbook, before any sheet is laid out
    for sheet in chosen:
        out.append(f"{SECTION_PREFIX}シート: {sheet.name}")
        out += _state(sheet)
        rows = _grid(sheet, target.formulas)
        if target.uses_header:
            out += table_to_lines(rows, target, f"シート「{sheet.name}」")
        else:
            for row_no, row in enumerate(rows, start=1):
                cells = [cell_text(v) for v in row]
                while cells and cells[-1] == "":
                    cells.pop()
                if cells:
                    out.append(f"R{row_no} | " + " | ".join(cells))
        out += _extras(sheet)
        out.append("")
    return "\n".join(out)
