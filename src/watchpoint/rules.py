"""Warnings a person sets up for themselves: a source that should have been updated by a deadline,
and values that break a rule (an empty 承認, a 状態 outside the expected ones).

These are decided by watchpoint on every check, the same way every time, from the latest copy it
keeps; no AI is involved and nothing leaves the PC. They are shown as "要注意" next to the changes,
notified once when new, and they disappear when the source is put right.
"""

from __future__ import annotations

import datetime as dt
import re

from .config import Rule, Target
from .store import State, TargetStore
from .textify import cell_refs, normalize_value, parse_records
from .timewords import parse_deadline

MAX_ALERTS = 30  # per target; the rest is counted


def _when(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s) if s else None


def deadline_alert(target: Target, state: State, store: TargetStore, now: dt.datetime) -> str | None:
    """ "Not updated" when no change was found in the period that ended at the latest deadline."""
    if not target.expect_update:
        return None
    d = parse_deadline(target.expect_update)
    started = _first_seen(state, store)
    if started is None:
        return None  # nothing fetched yet: nothing is known
    last = _when(state.last_changed) or started
    if d.kind == "interval":
        assert d.interval is not None
        if now - last > d.interval:
            return f"{d.label}に更新されるはずが、{_ago(now - last)}更新されていません（最後の変更 {last:%m/%d %H:%M}）"
        return None
    due = d.last_before(now)
    if due is None:
        return None
    before = d.last_before(due - dt.timedelta(minutes=1))
    if before is None or started > before:
        return None  # watching began inside this period: whether it was updated before then is unknown
    if last <= before:
        return f"{due:%m/%d %H:%M}（{d.text}）までに更新されていません（最後の変更 {last:%m/%d %H:%M}）"
    return None


def _first_seen(state: State, store: TargetStore) -> dt.datetime | None:
    if state.history:
        try:
            return dt.datetime.strptime(state.history[0][:15], "%Y%m%d-%H%M%S")
        except ValueError:
            pass
    return _when(state.last_checked) if state.snapshot else None


def _ago(td: dt.timedelta) -> str:
    days = td.days
    return f"{days} 日" if days >= 1 else f"{td.seconds // 3600} 時間"


def _label(key: tuple[str, str]) -> str:
    name, _, value = key[1].partition("=")
    return value if name == "セル" else f"{name} {value}"


def _matches(rule: Rule, values: dict[str, str], how: list[str]) -> bool:
    for col, wanted in (rule.when or {}).items():
        options = [normalize_value(w, how) for w in ([wanted] if isinstance(wanted, str) else wanted)]
        if not any(w in values.get(col, "") for w in options):
            return False
    return True


def _problem(rule: Rule, value: str, how: list[str]) -> str | None:
    if not value:
        return "空です" if rule.required else None
    if rule.one_of and value not in [normalize_value(v, how) for v in rule.one_of]:
        return f"「{value}」です（{'・'.join(rule.one_of)} のどれかのはず）"
    if rule.pattern and not re.search(rule.pattern, value):
        return f"「{value}」が決めた形に合いません"
    return None


def _cell_names(target: Target) -> dict[str, str]:
    """Cell address -> the name given to it in `cells` (a range gives every cell in it that name)."""
    from openpyxl.utils.cell import get_column_letter

    out: dict[str, str] = {}
    for ref in cell_refs(target):
        for r in range(ref.min_row, ref.max_row + 1):
            for c in range(ref.min_col, ref.max_col + 1):
                out.setdefault(f"{get_column_letter(c)}{r}", ref.label)
    return out


def rule_alerts(target: Target, text: str) -> list[str]:
    """Every row (or named cell) that breaks a rule, in the latest text."""
    if not target.rules:
        return []
    recs = parse_records(text)
    cell_names = _cell_names(target) if target.cells else {}
    how = list(target.normalize)  # the values were normalized; compare what the rule expects the same way
    known = (
        {f for fields in recs.fields.values() for f in fields}
        | {c for cols in recs.columns.values() for c in cols}
        | set(cell_names.values())
    )
    out: list[str] = []
    for rule in target.rules:
        if rule.field not in known and recs.order:
            out.append(f"ルールの「{rule.field}」という列・項目が見当たりません（列の名前が変わった？）")
            continue
        for key in recs.order:
            values = recs.fields[key]
            if cell_names:
                # named cells: each holds one field, and an empty cell is written without its name
                if cell_names.get(key[1].partition("=")[2]) != rule.field:
                    continue
            elif not recs.columns.get(key[0]) and rule.field not in values:
                continue
            if not _matches(rule, values, how):
                continue
            problem = _problem(rule, values.get(rule.field, ""), how)
            if problem:
                cond = "".join(f"・{c} が {values.get(c, '')}" for c in (rule.when or {}))
                what = rule.message or f"{rule.field} が{problem}"
                out.append(f"{_label(key)}: {what}" + (f"（{cond.lstrip('・')}）" if cond and not rule.message else ""))
    if len(out) > MAX_ALERTS:
        out = [*out[:MAX_ALERTS], f"ほか {len(out) - MAX_ALERTS} 件"]
    return out


def alerts_for(target: Target, store: TargetStore, state: State, now: dt.datetime | None = None) -> list[str]:
    """All warnings for a target from what is kept: the deadline, then the rules on the latest copy."""
    now = now or dt.datetime.now()
    out = []
    late = deadline_alert(target, state, store, now)
    if late:
        out.append(late)
    if target.rules and state.snapshot:
        text = store.read_text(state.snapshot)
        if text is not None:
            out += rule_alerts(target, text)
    return out


def field_stats(text: str, sample: int = 8) -> list[dict]:
    """For proposing rules: how filled each field is and which values it takes, from the latest text."""
    recs = parse_records(text)
    names: list[str] = []
    for cols in recs.columns.values():
        names += [c for c in cols if c not in names]
    for fields in recs.fields.values():
        names += [f for f in fields if f not in names]
    rows = len(recs.order)
    out = []
    for name in names:
        values = [recs.fields[k].get(name, "") for k in recs.order]
        empty_keys = [_label(k) for k, v in zip(recs.order, values, strict=True) if not v]
        counts: dict[str, int] = {}
        for v in values:
            if v:
                counts[v] = counts.get(v, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:sample]
        out.append({
            "field": name, "rows": rows, "empty": len(empty_keys), "empty_in": empty_keys[:sample],
            "distinct": len(counts), "values": [{"value": v, "count": c} for v, c in top],
        })  # fmt: skip
    return out
