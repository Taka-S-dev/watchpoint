"""The text shape every format is turned into, and the filters applied to it (sections, normalize, sort)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Target


TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".log", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".ini"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
WORD_SUFFIXES = {".docx"}
CSV_SUFFIXES = {".csv", ".tsv"}
HTML_SUFFIXES = {".html", ".htm"}
SECTION_PREFIX = "## "
COLUMNS_PREFIX = "@列 "
AUTO_HEADER_SCAN = 30
MAX_CELLS = 2000  # cells watched in one target; a whole sheet belongs to the row-by-row comparison

_RECORD = re.compile(r"^\[([^=\]]+)=(.*?)\](?: (.*))?$")


def _record_body(line: str) -> str | None:
    m = _RECORD.match(line)
    return (m.group(3) or "") if m else None


class UnsupportedFormat(Exception):
    """The file can be compared only by its bytes, not as text."""


def _is_structural(line: str, tables: bool = True) -> bool:
    """Lines watchpoint itself writes. In documents (text, Word, web pages) a "## " line is the
    author's heading, so ignore / include / normalize must apply to it like any other line."""
    return line.startswith((SECTION_PREFIX, COLUMNS_PREFIX) if tables else COLUMNS_PREFIX)


def shape(text: str, target: Target, tables: bool = True) -> str:
    """Apply sections → include → ignore → normalize → sort, in that order.

    tables: the text was generated from sheets / records, so "## " lines mark sections.
    """

    def _structural(line: str) -> bool:
        return _is_structural(line, tables)

    lines = text.splitlines()
    if target.sections:
        lines = _keep_sections(lines, target.sections)
    if target.include:
        regs = [re.compile(p) for p in target.include]
        lines = [
            line for line in lines if _structural(line) or line.startswith("#") or any(r.search(line) for r in regs)
        ]
    if target.ignore:
        regs = [re.compile(p) for p in target.ignore]
        lines = [
            line
            for line in lines
            if _structural(line) or not any(r.search(line) or r.search(_record_body(line) or "\0") for r in regs)
        ]
    if target.normalize:
        how = target.normalize
        lines = [
            line if _structural(line) else _normalize_record(line, how) if tables else _normalize(line, how)
            for line in lines
        ]
        if "whitespace" in target.normalize:
            lines = [line for line in lines if line.strip()]
    if target.sort_lines:
        lines = _sort_within_sections(lines)
    return "\n".join(lines) + ("\n" if lines else "")


def normalize_value(value: str, how: Iterable[str]) -> str:
    """One value as `normalize` makes it, with the field separator kept out of it.

    NFKC turns the full-width ｜ a cell's | is written as back into |, which would split the cell into
    a field that is not there; so the value is escaped again after normalizing.
    """
    return _normalize(value, how).replace("|", "｜")


def _normalize_record(line: str, how: Iterable[str]) -> str:
    """A record line with only its values normalized: the key's name and the field names stay as they are,
    so columns, rules and `key` still name them, and the separators stay separators."""
    m = _RECORD.match(line)
    if not m:
        return _normalize(line, how)
    parts = []
    for part in (m.group(3) or "").split(" | ") if m.group(3) else []:
        name, sep, value = part.partition("=")
        parts.append(f"{name}={normalize_value(value, how)}" if sep else normalize_value(part, how))
    return f"[{m.group(1)}={normalize_value(m.group(2), how)}]" + (" " + " | ".join(parts) if parts else "")


def _normalize(line: str, how: Iterable[str]) -> str:
    if "nfkc" in how:
        line = unicodedata.normalize("NFKC", line)
    if "whitespace" in how:
        line = re.sub(r"[ \t　]+", " ", line).strip()
    if "case" in how:
        line = line.casefold()
    return line


def _heading_level(line: str) -> int | None:
    m = re.match(r"^(#{1,6}) ", line)
    return len(m.group(1)) if m else None


def _keep_sections(lines: list[str], names: list[str]) -> list[str]:
    """Keep headings whose title contains one of names, with everything below them."""
    out: list[str] = []
    keep_level: int | None = None
    for line in lines:
        level = _heading_level(line)
        if level is not None and not line.startswith(SECTION_PREFIX + "シート:"):
            if keep_level is not None and level <= keep_level:
                keep_level = None
            if keep_level is None and any(n in line[level + 1 :] for n in names):
                keep_level = level
        if keep_level is not None or line.startswith((SECTION_PREFIX + "シート:", COLUMNS_PREFIX)):
            out.append(line)
    return out


def _sort_within_sections(lines: list[str]) -> list[str]:
    out: list[str] = []
    block: list[str] = []
    for line in lines:
        if _is_structural(line) or _heading_level(line):
            out += sorted(block)
            block = []
            out.append(line)
        else:
            block.append(line)
    return out + sorted(block)


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
