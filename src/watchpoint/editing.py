"""Adding a target to the text of a config file, for the form on the page.

The form never saves. It returns the config text with the new target written into
the `targets:` list, and the page puts that into its editor, so validation, trying
and saving go through the same path as a hand edit. Comments and the layout of the
rest of the file are kept: the new entry is inserted as text, not by dumping the
whole file again.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from . import sharepoint
from .config import Rule, Target, _translate
from .report import kind_label
from .textify import EXCEL_SUFFIXES, TEXT_SUFFIXES, WORD_SUFFIXES
from .timewords import check_every, parse_deadline

# The order a person reads a target in: what and where, how, then what to tell.
ORDER = (
    "name", "source", "fetch", "watch", "tags", "every", "link",
    "attachments", "sheets", "header_row", "key", "columns", "where", "cells", "selector",
    "report", "points", "notify_when", "expect_update", "rules",
)  # fmt: skip

_TARGETS = re.compile(r"^targets\s*:(.*)$")


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data.strip("\n") else None  # points and notify_when stay readable
    if style:
        data = data.strip("\n") + "\n"
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


class _Block(dict):
    """The target itself: always one key per line, however simple its values are."""


def _block(dumper: yaml.SafeDumper, data: dict):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=False)


def _leaf(dumper: yaml.SafeDumper, data):
    # tags: [a, b] and where: {回答者: 佐藤} on one line, as people write them
    items = data.values() if isinstance(data, dict) else data
    flow = all(not isinstance(v, dict | list) for v in items)
    if isinstance(data, dict):
        return dumper.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=flow)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(str, _str)
_Dumper.add_representer(_Block, _block)
_Dumper.add_representer(dict, _leaf)
_Dumper.add_representer(list, _leaf)


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def clean_item(item: dict) -> dict:
    """Drop empty fields, refuse unknown ones, and put the rest in reading order."""
    if not isinstance(item, dict):
        raise ValueError("item はオブジェクト")
    unknown = sorted(set(item) - set(Target.model_fields))
    if unknown:
        raise ValueError(f"知らない項目です: {', '.join(unknown)}")
    kept = {k: v.strip() if isinstance(v, str) else v for k, v in item.items()}
    kept = {k: v for k, v in kept.items() if not _empty(v)}
    rank = {k: i for i, k in enumerate(ORDER)}
    return dict(sorted(kept.items(), key=lambda kv: rank.get(kv[0], len(ORDER))))


def validation_message(e: Exception) -> str:
    """A pydantic error in the same words as the config file's; anything else as it is."""
    if isinstance(e, ValidationError):
        lines = []
        for err in e.errors():
            loc = " > ".join(str(p) for p in err["loc"] if not str(p).startswith("function-"))
            lines.append(f"{loc}: {_translate(err)}" if loc else _translate(err))
        return "\n".join(lines)
    return str(e)


def check_item(item: dict) -> Target:
    """Validate one target on its own, with messages in the same words as the config file's."""
    try:
        return Target.model_validate(item)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            loc = " > ".join(str(p) for p in err["loc"] if not str(p).startswith("function-"))
            lines.append(f"{loc}: {_translate(err)}" if loc else _translate(err))
        raise ValueError("\n".join(lines)) from e


def target_yaml(item: dict) -> list[str]:
    """The item as a YAML list entry (`- name: …`), one string per line, without indentation."""
    text = yaml.dump([_Block(item)], Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=10_000)
    return text.rstrip("\n").split("\n")


def existing_names(text: str) -> set[str]:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return set()
    targets = raw.get("targets") if isinstance(raw, dict) else None
    return {str(t.get("name", "")).strip() for t in targets or [] if isinstance(t, dict)}


def add_target(text: str, item: dict) -> str:
    """Return text with item appended to the `targets:` list (created when there is none)."""
    lines = text.split("\n")
    at = next((i for i, line in enumerate(lines) if _TARGETS.match(line)), None)
    if at is None:
        body = text.rstrip("\n")
        return (body + "\n\n" if body else "") + "targets:\n" + "\n".join("  " + s for s in target_yaml(item)) + "\n"

    rest = _TARGETS.match(lines[at]).group(1).split("#", 1)[0].strip()  # type: ignore[union-attr]
    if rest == "[]":
        lines[at] = "targets:"
    elif rest:
        raise ValueError("targets が [ … ] の形で書かれているので、画面からは足せません。- で始まる形に直してください")

    end = len(lines)
    for j in range(at + 1, len(lines)):
        line = lines[j]
        if line and not line[0].isspace() and not line.startswith(("#", "- ")):
            end = j  # the next top-level key
            break
    while end > at + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1  # blank lines and comments in front of the next key belong to it

    first = next((re.match(r"^(\s*)- ", line) for line in lines[at + 1 : end] if re.match(r"^\s*- ", line)), None)
    indent = first.group(1) if first else "  "
    entry = [indent + s for s in target_yaml(item)]
    if end > at + 1:
        entry = ["", *entry]  # keep entries apart, as the template does
    lines[end:end] = entry
    out = "\n".join(lines)
    return out if out.endswith("\n") else out + "\n"


_BEFORE_EVERY = ("name", "source", "fetch", "watch", "tags")  # every goes after these (ORDER)


def set_every(text: str, name: str, every: str | None) -> str:
    """Return text with one target's every set (None: removed, so the config's every applies).

    Only that value (or that one line) changes; comments and the rest of the file stay as they are.
    """
    if every is not None:
        every = check_every(every.strip())
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as e:
        raise ValueError(f"設定ファイルを読めません: {e}") from e
    targets = None
    if isinstance(root, yaml.MappingNode):
        targets = next((v for k, v in root.value if k.value == "targets"), None)
    item = None
    if isinstance(targets, yaml.SequenceNode):
        for node in targets.value:
            if isinstance(node, yaml.MappingNode) and any(
                k.value == "name" and isinstance(v, yaml.ScalarNode) and v.value.strip() == name for k, v in node.value
            ):
                item = node
                break
    if item is None:
        raise ValueError(
            f"「{name}」が設定ファイルの targets にありません（include で取り込んだファイルにあるなら、そちらで直してください）"
        )
    if item.flow_style:
        raise ValueError(
            f"「{name}」は {{ … }} の形で書かれているので、画面からは変えられません。「設定」で直してください"
        )

    lines = text.split("\n")
    pairs = {k.value: (k, v) for k, v in item.value}
    if "every" in pairs:
        k, v = pairs["every"]
        if not isinstance(v, yaml.ScalarNode) or v.start_mark.line != v.end_mark.line:
            raise ValueError("every が1行で書かれていないので、画面からは変えられません。「設定」で直してください")
        line = lines[v.start_mark.line]
        if every is not None:
            lines[v.start_mark.line] = line[: v.start_mark.column] + every + line[v.end_mark.column :]
        else:
            if line[: k.start_mark.column].strip():  # `- every: …` starts the entry: the line cannot go
                raise ValueError("every が項目の先頭にあるので、画面からは消せません。「設定」で直してください")
            del lines[k.start_mark.line]
        return "\n".join(lines)
    if every is None:
        return text  # already follows the config's every
    after = max(pairs[key][1].end_mark.line for key in _BEFORE_EVERY if key in pairs)
    indent = pairs["name"][0].start_mark.column
    lines.insert(after + 1, " " * indent + f"every: {every}")
    return "\n".join(lines)


def describe_source(source: str) -> dict:
    """What the form needs to know about a source to offer the right ways of watching it."""
    source = source.strip()
    if not source:
        return {"kind": None}
    address = sharepoint.classify(source) if source.startswith(("http://", "https://")) else None
    if address is not None and address.form != "file":  # a link for a browser: say which file it is, if it can
        return {
            "kind": None,
            "link": {"label": address.label, "file_url": address.file_url, "advice": address.advice(source)},
        }
    try:
        t = Target(name="x", source=source)
    except ValidationError:
        return {"kind": None}
    suffix = Path(source.split("?")[0]).suffix.lower()
    kind = t.kind
    label = kind_label(t)
    readable = kind != "file" or suffix in TEXT_SUFFIXES | EXCEL_SUFFIXES | WORD_SUFFIXES
    if not readable:  # "中身が変わったかだけ" says less than the format to someone picking a PDF
        label = f"{suffix[1:].upper()} ファイル" if suffix else "ファイル"
    return {
        "kind": kind,
        "label": label,
        "text": readable,
        "excel": suffix in (".xlsx", ".xlsm") and kind in ("file", "office", "download"),
        "table": suffix in (".xlsx", ".xlsm", ".csv", ".tsv") and kind in ("file", "office", "download"),
        "timestamp": kind == "file",
    }


# --------------------------------------------------------------------------- rules and deadlines


class _IndentedDumper(_Dumper):
    """Lists under a key indented under it (`rules:` then `  - field: …`), as the template writes them."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _find_item(text: str, name: str):
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as e:
        raise ValueError(f"設定ファイルを読めません: {e}") from e
    targets = (
        next((v for k, v in root.value if k.value == "targets"), None) if isinstance(root, yaml.MappingNode) else None
    )
    if isinstance(targets, yaml.SequenceNode):
        for node in targets.value:
            if isinstance(node, yaml.MappingNode) and any(
                k.value == "name" and isinstance(v, yaml.ScalarNode) and v.value.strip() == name for k, v in node.value
            ):
                if node.flow_style:
                    raise ValueError(
                        f"「{name}」は {{ … }} の形で書かれているので、画面からは変えられません。「設定」で直してください"
                    )
                return node
    raise ValueError(
        f"「{name}」が設定ファイルの targets にありません（include で取り込んだファイルにあるなら、そちらで直してください）"
    )


def _last_line(lines: list[str], node) -> int:
    """The last line that belongs to a value (a block value's end mark sits on the line after it)."""
    end = node.end_mark.line
    if end > node.start_mark.line and not lines[end][: node.end_mark.column].strip():
        end -= 1  # the mark sits on the first token after the value (the next key, or the next target)
    while end > node.start_mark.line and (not lines[end].strip() or lines[end].lstrip().startswith("#")):
        end -= 1
    return end


def set_value(text: str, name: str, key: str, value) -> str:
    """Return text with one setting of one target replaced, added (in reading order) or removed (None).

    Only those lines change; comments and the rest of the file stay as they are.
    """
    item = _find_item(text, name)
    lines = text.split("\n")
    pairs = {k.value: (k, v) for k, v in item.value}
    indent = pairs["name"][0].start_mark.column
    new = [] if value is None else [
        " " * indent + line for line in yaml.dump(_Block({key: value}), Dumper=_IndentedDumper, allow_unicode=True, sort_keys=False, width=10_000).rstrip("\n").split("\n")
    ]  # fmt: skip
    if key in pairs:
        k, v = pairs[key]
        if lines[k.start_mark.line][: k.start_mark.column].strip():  # `- key: …` starts the entry
            raise ValueError(f"{key} が項目の先頭にあるので、画面からは変えられません。「設定」で直してください")
        lines[k.start_mark.line : _last_line(lines, v) + 1] = new
        return "\n".join(lines)
    if value is None:
        return text
    rank = {k: i for i, k in enumerate(ORDER)}
    before = [kk for kk in pairs if rank.get(kk, len(ORDER)) < rank.get(key, len(ORDER))] or ["name"]
    after = max(_last_line(lines, pairs[kk][1]) for kk in before)
    lines[after + 1 : after + 1] = new
    return "\n".join(lines)


def raw_target(text: str, name: str) -> dict:
    """The target as written (variables not filled in), to change one setting and write it back."""
    raw = yaml.safe_load(text) or {}
    for t in raw.get("targets") or []:
        if isinstance(t, dict) and str(t.get("name", "")).strip() == name:
            return t
    raise ValueError(f"「{name}」が設定ファイルの targets にありません")


def add_rule(text: str, name: str, rule: dict) -> str:
    rule = {k: v for k, v in rule.items() if not _empty(v) and v is not False}
    Rule.model_validate(rule)  # says what is wrong before anything is written
    rules = list(raw_target(text, name).get("rules") or [])
    if rule in rules:
        raise ValueError("同じルールがもうあります")
    return set_value(text, name, "rules", [_Block(r) for r in [*rules, rule]])  # one rule per few lines


def remove_rule(text: str, name: str, index: int) -> str:
    rules = list(raw_target(text, name).get("rules") or [])
    if not 0 <= index < len(rules):
        raise ValueError(f"ルールの {index + 1} 番はありません（{len(rules)} 件）")
    del rules[index]
    return set_value(text, name, "rules", [_Block(r) for r in rules] or None)


def set_expect_update(text: str, name: str, when: str | None) -> str:
    if when is not None and when.strip():
        when = parse_deadline(when).text
    else:
        when = None
    return set_value(text, name, "expect_update", when)


def remove_target(text: str, name: str) -> str:
    """Return text without one target's entry and the comment lines right above it (they describe it).

    Everything else, other comments included, stays as it is.
    """
    item = _find_item(text, name)
    lines = text.split("\n")
    first = item.start_mark.line
    if not re.match(r"^\s*- ", lines[first]):
        raise ValueError(
            f"「{name}」の書き方が - で始まる形ではないので、画面からは消せません。「設定」で直してください"
        )
    last = _last_line(lines, item)
    while first > 0 and lines[first - 1].lstrip().startswith("#"):
        first -= 1
    del lines[first : last + 1]
    # the blank line that kept this entry apart from the next goes with it
    prev = lines[first - 1] if first > 0 else ""
    if first < len(lines) and not lines[first].strip() and (not prev.strip() or _TARGETS.match(prev)):
        del lines[first]
    elif prev and not prev.strip() and (first == len(lines) or not lines[first].startswith(" ")):
        del lines[first - 1]  # it was the last entry: the blank line before it goes
    out = "\n".join(lines)
    raw = yaml.safe_load(out) or {}
    if isinstance(raw, dict) and "targets" in raw and raw["targets"] is None:  # the last one went
        out = re.sub(r"^targets\s*:[ \t]*(#.*)?$", "targets: []", out, count=1, flags=re.M)
    return out
