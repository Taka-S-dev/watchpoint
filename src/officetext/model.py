"""What the readers return. Plain, frozen data: how to show it is up to the tool that reads it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- Word


@dataclass(frozen=True)
class Block:
    """One piece of a document's body, in document order.

    kind: "heading", "paragraph", "list" (a numbered or bulleted item), "table", "textbox"
    (the text of a box floating near the paragraph before it) or "toc" (a table of contents,
    left as one block: its page numbers go stale and say nothing the headings do not).
    """

    kind: str
    text: str = ""  # with the inline marks described in the package docstring; lines joined by \n
    level: int = 0  # heading depth from 1; list depth from 0
    label: str = ""  # the automatic number as printed ("1.2", "(a)", "第3条"), "•" for a bullet
    rows: tuple[tuple[str, ...], ...] = ()  # a table's cells; a cell merged across columns appears once


@dataclass(frozen=True)
class Change:
    """A tracked change, as listed at the end of a review."""

    kind: str  # "inserted" or "deleted"
    text: str
    author: str = ""
    date: str = ""  # YYYY-MM-DD


@dataclass(frozen=True)
class Document:
    blocks: tuple[Block, ...]
    header: tuple[str, ...] = ()  # every line of every section's headers, once each
    footer: tuple[str, ...] = ()
    notes: dict[str, str] = field(default_factory=dict)  # "1" -> footnote text, "e1" -> endnote, as referenced
    changes: tuple[Change, ...] = ()
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- Excel


@dataclass(frozen=True)
class Cell:
    address: str  # "C4"
    row: int
    col: int
    value: Any  # as stored: a number, text, a date, a bool; for a formula, its last calculated result
    shown: str  # as Excel displays it with the cell's number format (15%, 1,234, 2026/10/01)
    formula: str | None = None  # "=A1*2" when the cell holds a formula
    cached: bool = True  # False: a formula never calculated (saved by a tool), so value is None
    struck: bool = False  # the whole cell is struck through; struck words inside it are in shown as ~~
    comment: str | None = None
    hidden: bool = False  # its row or column is hidden


@dataclass(frozen=True)
class Shape:
    """Text in a shape or text box drawn on a sheet, with the cell its top-left corner is on."""

    text: str
    anchor: str = ""  # "B3"
    kind: str = "shape"  # "shape" or "textbox"


@dataclass(frozen=True)
class Sheet:
    name: str
    state: str = "visible"  # "visible", "hidden" or "veryHidden"
    cells: tuple[Cell, ...] = ()  # the cells that hold something, row by row
    shapes: tuple[Shape, ...] = ()
    merged: tuple[str, ...] = ()  # "B3:E3"
    header: tuple[str, ...] = ()  # the printed header and footer
    footer: tuple[str, ...] = ()
    hidden_rows: frozenset[int] = frozenset()
    hidden_cols: frozenset[str] = frozenset()  # column letters


@dataclass(frozen=True)
class Workbook:
    sheets: tuple[Sheet, ...]
    warnings: tuple[str, ...] = ()
