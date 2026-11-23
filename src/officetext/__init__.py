"""Reading Office files the way a person sees them, as plain data for any tool to show its own way.

What a plain read with python-docx or openpyxl silently loses, and this keeps: a Word document's
automatic heading numbers, its header and footer, footnotes, text boxes, content controls,
tracked changes and comments; a workbook's formulas Excel never calculated, text in shapes,
comments, struck-through cells and hidden sheets.

This package only reads. It never writes to the file it is given, nor anywhere else, and it
depends on nothing of the tools that use it, so any of them can take it as it is.

The interface (the dataclasses in model.py and the read_* functions) changes by adding only:
renaming or removing a field raises the major version in __version__.

Text inside a paragraph or a cell carries these marks, one notation for every reader:
``~~struck~~``, ``{++inserted++}`` and ``{--deleted--}`` for tracked changes,
``{>>author: comment<<}`` where a comment is anchored, ``[^1]`` for a footnote reference
(``[^e1]`` for an endnote) and ``｜base《reading》`` for furigana.
"""

from .excel import read_workbook
from .model import Block, Cell, Change, Document, Shape, Sheet, Workbook
from .package import TooLarge
from .word import read_document

__version__ = "1.0.0"

__all__ = [
    "Block",
    "Cell",
    "Change",
    "Document",
    "Shape",
    "Sheet",
    "TooLarge",
    "Workbook",
    "__version__",
    "read_document",
    "read_workbook",
]
