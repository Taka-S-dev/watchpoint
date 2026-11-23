"""JSON, XML and delimited command output turned into records (one per line) or indented text."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .base import SECTION_PREFIX, shape
from .tables import cell_text, one_line, table_to_lines

if TYPE_CHECKING:
    from ..config import Target


def _is_name_value_list(value: list) -> bool:
    """[{name: 辛さ, value: 中辛}, …]: extra fields an issue tracker keeps as a list (Redmine custom_fields)."""
    return bool(value) and all(isinstance(v, dict) and "name" in v and "value" in v for v in value)


def _flatten(value, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    elif isinstance(value, list):
        if _is_name_value_list(value):
            # Each becomes its own field, so a change of 辛さ shows as 辛さ and rules can name it.
            # Joining only the names (as for other lists of objects) lost every value.
            for item in value:
                v = item["value"]
                text = ", ".join(cell_text(x) for x in v) if isinstance(v, list) else cell_text(v)
                out[f"{prefix}{item['name']}"] = text
        elif all(not isinstance(v, dict | list) for v in value):
            out[prefix[:-1]] = ", ".join(cell_text(v) for v in value)
        else:
            names = [v.get("name") for v in value if isinstance(v, dict) and "name" in v]
            out[prefix[:-1]] = (
                ", ".join(map(str, names)) if names else json.dumps(value, ensure_ascii=False, sort_keys=True)
            )
    else:
        out[prefix[:-1]] = cell_text(value)
    return out


def dig(data, path: str | None):
    if not path:
        return data
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise ValueError(f"JSON に「{path}」がありません（{part} で止まりました）")
    return data


def json_records(data, target: Target) -> list[dict]:
    path = target.records
    if path is None and isinstance(data, dict):
        lists = [k for k, v in data.items() if isinstance(v, list)]
        if len(lists) == 1:  # {issues: [...], total_count: 154, …}: the one list is the records
            path = lists[0]
        elif lists:
            raise ValueError(f"一覧が複数あります。records にどれかを書いてください（{'・'.join(lists)}）")
    items = dig(data, path)
    if not isinstance(items, list):
        raise ValueError(
            f"records「{path}」の先が一覧（配列）ではありません" if path else "JSON の中に一覧（配列）が見つかりません"
        )
    records = []
    for i in items:
        if not isinstance(i, dict):
            records.append({"value": cell_text(i)})
            continue
        flat = _flatten({k: v for k, v in i.items() if not (isinstance(v, list) and _is_name_value_list(v))})
        for k, v in i.items():  # extra fields by their own name (辛さ), unless that clashes with a field
            if isinstance(v, list) and _is_name_value_list(v):
                for name, text in _flatten(v, f"{k}.").items():
                    short = name.removeprefix(f"{k}.")
                    flat[short if short not in flat else name] = text
        records.append(flat)
    return records


def records_to_text(records: list[dict], target: Target, title: str = "API") -> str:
    header: list[str] = []
    for r in records:
        for k in r:
            if k not in header:
                header.append(k)
    rows = [header] + [[r.get(h) for h in header] for r in records]
    shaped = target.model_copy(update={"header_row": 1})
    if not records:
        return f"{SECTION_PREFIX}{title}\n(0 件)\n"
    return "\n".join([f"{SECTION_PREFIX}{title}"] + table_to_lines(rows, shaped, "JSON ")) + "\n"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]  # drop an XML namespace


def _element_fields(el, prefix: str = "") -> dict[str, str]:
    """An element as fields: its attributes, and the text of each child (nested ones as a.b)."""
    out: dict[str, str] = {f"{prefix}{_local(k)}": v for k, v in el.attrib.items()}
    seen: dict[str, list[str]] = {}
    for child in el:
        name = f"{prefix}{_local(child.tag)}"
        if len(child) or child.attrib:
            for k, v in _element_fields(child, name + ".").items():
                seen.setdefault(k, []).append(v)
        text = (child.text or "").strip()
        if text or not (len(child) or child.attrib):
            seen.setdefault(name, []).append(one_line(text))
    out.update({k: ", ".join(v) for k, v in seen.items()})
    return out


def xml_records(text: str, target: Target) -> list[dict]:
    """Each element named `records` (anywhere in the document) is one record."""
    from xml.etree import ElementTree

    if "<!DOCTYPE" in text[:2000] or "<!ENTITY" in text:
        raise ValueError("DOCTYPE や ENTITY のある XML は読みません")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as e:
        raise ValueError(f"XML として読めませんでした: {e}") from None
    found = [el for el in root.iter() if _local(el.tag) == target.records]
    if not found and _local(root.tag) != target.records:
        tags = sorted({_local(el.tag) for el in root.iter()})[:15]
        raise ValueError(f"records「{target.records}」の要素がありません（ある要素: {', '.join(tags)}）")
    return [_element_fields(el) for el in found]


def delimited_rows(text: str, target: Target) -> list[list]:
    import csv
    import io

    delimiter = "\t" if target.format == "tsv" else ","
    rows = [[c if c != "" else None for c in r] for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return [list(target.fields), *rows] if target.fields else rows


def command_output_to_text(out: str, target: Target) -> str:
    """The output of a command, read as its format says, as the text to compare."""
    fmt = target.format
    if fmt == "json":
        try:
            data = json.loads(out)
        except ValueError as e:
            raise ValueError(f"コマンドの出力を JSON として読めませんでした: {e}") from None
        return shape(json_to_text(data, target), target)
    if fmt == "xml":
        return shape(records_to_text(xml_records(out, target), target, title="コマンド"), target)
    if fmt in ("csv", "tsv"):
        rows = delimited_rows(out, target)
        if target.uses_header or target.fields:
            shaped = target if target.uses_header else target.model_copy(update={"header_row": 1})
            return shape(
                "\n".join([f"{SECTION_PREFIX}コマンド"] + table_to_lines(rows, shaped, "出力 ")) + "\n", target
            )
        return shape(out, target, tables=False)
    return shape(out, target, tables=False)


def json_to_text(data, target: Target) -> str:
    if target.records or target.uses_header:
        return records_to_text(json_records(data, target), target)
    return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
