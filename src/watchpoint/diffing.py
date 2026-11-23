"""Comparing two texts: hunks labelled with their heading, changes within a line, and record by record."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .textify.base import SECTION_PREFIX
from .textify.records import parse_records

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def annotate_hunks(diff_lines: list[str], before: str) -> list[str]:
    """Add the nearest heading above each hunk, like `git diff` does for functions."""
    base = before.splitlines()
    out = []
    for line in diff_lines:
        m = _HUNK.match(line)
        if m:
            start = int(m.group(1))
            heading = next((base[i] for i in range(min(start, len(base)) - 1, -1, -1) if base[i].startswith("#")), None)
            if heading:
                line = f"{line} {heading.lstrip('#').strip()}"
        out.append(line)
    return out


_TOKEN = re.compile(r"\d+(?:[.,:/]\d+)*|[A-Za-z_][A-Za-z0-9_]*|\s+|.", re.S)


def inline_changes(diff_lines: list[str], min_ratio: float = 0.4) -> list[str]:
    """For lines replaced one for one, mark what changed inside the line: 保持は [30→60] 日.

    Lines are compared as tokens (a number, a word, or one character), so a
    number is never split in the middle.
    """
    out: list[str] = []
    minus: list[str] = []
    plus: list[str] = []

    def pair() -> None:
        if minus and len(minus) == len(plus):
            for a, b in zip(minus, plus, strict=False):
                ta, tb = _TOKEN.findall(a), _TOKEN.findall(b)
                sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
                if sm.ratio() < min_ratio:
                    continue
                parts = []
                for op, i1, i2, j1, j2 in sm.get_opcodes():
                    if op == "equal":
                        parts.append("".join(ta[i1:i2]))
                    else:
                        parts.append(f"[{''.join(ta[i1:i2])}→{''.join(tb[j1:j2])}]")
                out.append("".join(parts))
        minus.clear()
        plus.clear()

    for line in diff_lines:
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            if plus:
                pair()
            minus.append(line[1:])
        elif line.startswith("+"):
            plus.append(line[1:])
        else:
            pair()
    pair()
    return out


@dataclass
class RecordDiff:
    lines: list[str]
    added: int = 0
    removed: int = 0
    changed: int = 0
    columns: int = 0
    # The same changes as data, for a page to lay out as a table: lines are made for reading and an AI,
    # and a value holding " / " or " → " cannot be split back out of them.
    #   {kind: added|removed, section, key, values: [[field, value], ...]}
    #   {kind: changed, section, key, cells: [[field, before, after], ...], row: [[field, value now], ...]}
    #   {kind: columns, section, added: [...], removed: [...]}
    entries: list[dict] = field(default_factory=list)


def record_diff(before: str, after: str, report: Iterable[str] = ("added", "changed", "removed")) -> RecordDiff:
    """Per-record differences. Kinds not in `report` are neither counted nor listed."""
    report = set(report)
    a, b = parse_records(before), parse_records(after)
    by_section: dict[str, list[tuple[str, dict]]] = {}
    sections: list[str] = []
    result = RecordDiff([])

    def emit(section: str, line: str, entry: dict) -> None:
        if section not in by_section:
            by_section[section] = []
            sections.append(section)
        by_section[section].append((line, {**entry, "section": section}))

    skip_fields: dict[str, set[str]] = {}
    for section in dict.fromkeys(list(a.columns) + list(b.columns)):
        old_cols, new_cols = a.columns.get(section), b.columns.get(section)
        if old_cols is None or new_cols is None or old_cols == new_cols:
            continue
        added = [c for c in new_cols if c not in old_cols]
        gone = [c for c in old_cols if c not in new_cols]
        skip_fields[section] = set(added) | set(gone)
        if added or gone:
            result.columns += 1
            parts = ([f"追加 {', '.join(added)}"] if added else []) + ([f"削除 {', '.join(gone)}"] if gone else [])
            emit(section, "列　 " + " / ".join(parts), {"kind": "columns", "added": added, "removed": gone})

    for key in b.order:
        section, k = key
        if key not in a.fields:
            if "added" in report:
                result.added += 1
                emit(
                    section,
                    f"追加 {b.raw[key]}",
                    {"kind": "added", "key": k, "values": [[f, v] for f, v in b.fields[key].items()]},
                )
            continue
        if "changed" not in report:
            continue
        old, new = a.fields[key], b.fields[key]
        skip = skip_fields.get(section, set())
        changes = []
        cells = []
        for name in dict.fromkeys(list(old) + list(new)):
            if name in skip:
                continue
            ov, nv = old.get(name, ""), new.get(name, "")
            if ov != nv:
                changes.append(f"{name}: {ov or '(空)'} → {nv or '(空)'}")
                cells.append([name, ov, nv])
        if changes:
            result.changed += 1
            # the whole row as it is now, every column in the sheet's order, so a page can show the cells
            # around the change (which question got this answer) as the sheet itself would
            # (cells under a blank header are not in the column line; they follow it)
            names = list(dict.fromkeys(b.columns.get(section, []) + list(new) + [n for n in old if n not in skip]))
            row = [[name, new.get(name, "")] for name in names]
            emit(
                section,
                f"変更 [{k}] " + " / ".join(changes),
                {"kind": "changed", "key": k, "cells": cells, "row": row},
            )
    if "removed" in report:
        for key in a.order:
            if key not in b.fields:
                result.removed += 1
                emit(
                    key[0],
                    f"削除 {a.raw[key]}",
                    {"kind": "removed", "key": key[1], "values": [[f, v] for f, v in a.fields[key].items()]},
                )

    for s in sections:
        if s:
            result.lines.append(f"{SECTION_PREFIX}{s}")
        result.lines += [line for line, _ in by_section[s]]
        result.entries += [entry for _, entry in by_section[s]]
    return result
