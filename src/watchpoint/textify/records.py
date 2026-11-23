"""Reading back the record lines ``[No.=15] 質問=… | 状態=…`` that tables.py writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from .base import _RECORD, COLUMNS_PREFIX, SECTION_PREFIX


@dataclass
class Records:
    order: list[tuple[str, str]] = field(default_factory=list)
    fields: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    raw: dict[tuple[str, str], str] = field(default_factory=dict)
    columns: dict[str, list[str]] = field(default_factory=dict)


def parse_records(text: str) -> Records:
    recs = Records()
    section = ""
    for line in text.splitlines():
        if line.startswith(SECTION_PREFIX):
            section = line[len(SECTION_PREFIX) :]
            continue
        if line.startswith(COLUMNS_PREFIX):
            body = line[len(COLUMNS_PREFIX) :]
            recs.columns[section] = body.split(" | ") if body else []
            continue
        m = _RECORD.match(line)
        if not m:
            continue
        key = (section, f"{m.group(1)}={m.group(2)}")
        body = m.group(3) or ""
        fields: dict[str, str] = {}
        for part in body.split(" | ") if body else []:
            name, _, value = part.partition("=")
            fields[name] = value
        recs.order.append(key)
        recs.fields[key] = fields
        recs.raw[key] = line
    return recs
