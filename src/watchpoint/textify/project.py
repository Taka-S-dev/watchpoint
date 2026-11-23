"""Microsoft Project plans as lines to compare: one record per task, told apart by UID, from what
officetext.project reads. The plan as a whole is the record UID=0, with its status date (状況報告日).

Neither the row number (ID), the outline number (1.2.3) nor predecessors by ID are compared: both shift for every task
below a row inserted above it. A task's place is its parent summary tasks by name, which a move
under another summary changes and an inserted row does not."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .base import SECTION_PREFIX, UnsupportedFormat
from .tables import table_to_lines

if TYPE_CHECKING:
    from ..config import Target

PLAN_SUFFIXES = {".mpp", ".mpt"}
COLUMNS = ["UID", "名前", "親", "種類", "開始", "終了", "期間", "進捗", "先行", "担当",
           "基準開始", "基準終了", "期限", "メモ", "状況報告日"]  # fmt: skip


def is_plan(path: Path) -> bool:
    if path.suffix.lower() in PLAN_SUFFIXES:
        return True
    if path.suffix.lower() != ".xml":
        return False
    try:
        from officetext.project import is_project_xml
    except ImportError:
        return False
    return is_project_xml(path)


def _when(iso: str) -> str:
    """2026-12-10T17:00:00 as 2026-12-10 17:00."""
    return iso[:16].replace("T", " ") if iso else ""


def plan_to_text(path: Path, target: Target) -> str:
    try:
        from officetext.project import ProjectUnavailable, read_project
    except ImportError:  # officetext without its optional Project part
        raise UnsupportedFormat(path.suffix.lower()) from None
    try:
        plan = read_project(path)
    except ProjectUnavailable as e:
        # compared by its bytes, as before, saying why its tasks cannot be compared
        raise UnsupportedFormat(f"{path.suffix.lower()}。{e}") from None
    custom = list(dict.fromkeys(k for t in plan.tasks for k in t.custom))
    header = COLUMNS + [c for c in custom if c not in COLUMNS]
    rows: list[list] = [header]
    rows.append(
        ["0", f"（計画全体）{plan.name}".rstrip(), "", "", "", "", "", f"{plan.percent}%", "", "", "", "", "", "",
         plan.status_date[:10]] + [""] * (len(header) - len(COLUMNS))
    )  # fmt: skip
    for t in plan.tasks:
        kind = "マイルストーン" if t.milestone else "まとめ" if t.summary else ""
        if not t.active:
            kind = (kind + "・" if kind else "") + "無効"
        rows.append(
            [t.uid, t.name, " > ".join(t.path), kind, _when(t.start), _when(t.finish),
             f"{t.duration_days} 日" if t.duration_days else "", f"{t.percent}%", t.predecessor_names,
             "、".join(t.resources), _when(t.baseline_start), _when(t.baseline_finish), _when(t.deadline),
             t.notes, ""] + [t.custom.get(c, "") for c in header[len(COLUMNS):]]
        )  # fmt: skip
    keyed = target if target.key else target.model_copy(update={"key": "UID"})
    lines = table_to_lines(rows, keyed, "計画")
    title = plan.name or path.stem
    return "\n".join([f"{SECTION_PREFIX}計画: {title}", *lines]) + "\n"
