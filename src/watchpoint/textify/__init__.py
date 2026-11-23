"""Turning a fetched copy into plain text that can be compared line by line.

Two text shapes are produced:

- plain lines, compared with a unified diff
- keyed records, one per line: ``[No.=15] 質問=… | 状態=未回答``. These are
  compared record by record (see :func:`record_diff`), so an inserted or
  re-sorted row is reported as what happened to that record, not as a shifted
  block of lines. A ``@列 …`` line lists the columns, so an added or removed
  column is reported once instead of on every row.

Lines starting with ``## `` (sections) and ``@列 `` (columns) are structure and
survive every filter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from .base import (
    CSV_SUFFIXES,
    EXCEL_SUFFIXES,
    HTML_SUFFIXES,
    TEXT_SUFFIXES,
    WORD_SUFFIXES,
    UnsupportedFormat,
    normalize_value,
    read_text_file,
    shape,
)
from .excel import CellRefError, cell_refs, excel_to_text
from .folder import file_stamp, folder_listing
from .html import html_to_text
from .project import PLAN_SUFFIXES, is_plan, plan_to_text
from .records import parse_records
from .structured import command_output_to_text, json_records, json_to_text, records_to_text
from .tables import cell_text, csv_to_text
from .word import docx_to_text

if TYPE_CHECKING:
    from ..config import Target

__all__ = [
    "CSV_SUFFIXES",
    "PLAN_SUFFIXES",
    "CellRefError",
    "EXCEL_SUFFIXES",
    "TEXT_SUFFIXES",
    "UnsupportedFormat",
    "WORD_SUFFIXES",
    "cell_refs",
    "cell_text",
    "command_output_to_text",
    "file_stamp",
    "file_to_text",
    "folder_listing",
    "html_to_text",
    "json_records",
    "json_to_text",
    "normalize_value",
    "parse_records",
    "read_text_file",
    "records_to_text",
    "shape",
]


def file_to_text(path: Path, target: Target) -> str:
    suffix = path.suffix.lower()
    if is_plan(path):  # .mpp / .mpt, or Project XML
        text = plan_to_text(path, target)
    elif suffix in EXCEL_SUFFIXES:
        text = excel_to_text(path, target)
    elif suffix in WORD_SUFFIXES:
        return shape(docx_to_text(path), target, tables=False)
    elif suffix in CSV_SUFFIXES and target.uses_header:
        text = csv_to_text(path, target)
    elif suffix in HTML_SUFFIXES:
        return shape(html_to_text(read_text_file(path), target), target, tables=False)
    elif suffix == ".json" and (target.records or target.uses_header):
        text = json_to_text(json.loads(read_text_file(path)), target)
    elif suffix in TEXT_SUFFIXES:
        return shape(read_text_file(path), target, tables=False)
    else:
        raise UnsupportedFormat(suffix or "(拡張子なし)")
    return shape(text, target)
