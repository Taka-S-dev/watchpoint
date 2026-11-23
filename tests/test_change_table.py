"""Changes per record also come as data, so the page lays them out as a table without parsing text."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import watchpoint.check as chk
from watchpoint.check import acknowledge, check_target
from watchpoint.config import Config, Target
from watchpoint.diffing import record_diff
from watchpoint.report import latest_changes
from watchpoint.store import TargetStore

BEFORE = "## シート: 質問\n@列 質問 | 回答\n[No.=1] 質問=x | 回答=a / b\n[No.=2] 質問=y\n"
AFTER = "## シート: 質問\n@列 質問 | 回答 | 担当\n[No.=1] 質問=x | 回答=a → c\n[No.=3] 質問=z\n"


def test_entries_say_the_same_as_the_lines_and_keep_values_whole() -> None:
    r = record_diff(BEFORE, AFTER)
    assert r.lines == [
        "## シート: 質問",
        "列　 追加 担当",
        "変更 [No.=1] 回答: a / b → a → c",  # in a line, which " / " and " → " separate cannot be told
        "追加 [No.=3] 質問=z",
        "削除 [No.=2] 質問=y",
    ]
    assert r.entries == [
        {"kind": "columns", "added": ["担当"], "removed": [], "section": "シート: 質問"},
        {
            "kind": "changed",
            "key": "No.=1",
            "cells": [["回答", "a / b", "a → c"]],
            "row": [["質問", "x"], ["回答", "a → c"], ["担当", ""]],
            "section": "シート: 質問",
        },
        {"kind": "added", "key": "No.=3", "values": [["質問", "z"]], "section": "シート: 質問"},
        {"kind": "removed", "key": "No.=2", "values": [["質問", "y"]], "section": "シート: 質問"},
    ]


def test_a_changed_row_comes_whole_in_the_sheets_column_order() -> None:
    """The page shows the cells around a change (the question an answer belongs to) as the sheet has them."""
    before = "## S\n@列 質問 | 状態 | 回答\n[No.=1] 質問=保存期間は？ | 状態=未回答\n"
    after = "## S\n@列 質問 | 状態 | 回答\n[No.=1] 質問=保存期間は？ | 状態=回答済 | 回答=90 日\n"
    (entry,) = record_diff(before, after).entries
    assert entry["row"] == [["質問", "保存期間は？"], ["状態", "回答済"], ["回答", "90 日"]]
    # records without a column line (an API, a plan) keep the order their fields come in
    (entry,) = record_diff("[id=1] a=1 | b=2\n", "[id=1] a=1 | b=3\n").entries
    assert entry["row"] == [["a", "1"], ["b", "3"]]


@pytest.fixture
def clock(monkeypatch):
    now = [dt.datetime(2026, 11, 23, 9, 0)]

    def tick() -> None:
        now[0] += dt.timedelta(minutes=40)

    monkeypatch.setattr(chk, "_now", lambda: now[0])
    return tick


def test_the_page_gets_the_entries_before_and_after_acknowledging(tmp_path: Path, clock) -> None:
    src = tmp_path / "q.csv"
    src.write_text("No.,状態\n1,未回答\n2,未回答\n", encoding="utf-8")
    t = Target(name="q", source=str(src), key="No.")
    cfg = Config(targets=[t])
    data = tmp_path / "data"
    clock()
    check_target(t, cfg, data)
    src.write_text("No.,状態\n1,回答済\n2,未回答\n", encoding="utf-8")
    clock()
    check_target(t, cfg, data)
    store = TargetStore(data, "q")
    pending = latest_changes(t, cfg, store, store.load())
    want = [
        {
            "kind": "changed",
            "key": "No.=1",
            "cells": [["状態", "未回答", "回答済"]],
            "row": [["状態", "回答済"]],
            "section": "CSV",
        }
    ]
    assert pending.pending and pending.entries == want and pending.entries_total == 1
    acknowledge(t, cfg, data)
    seen = latest_changes(t, cfg, store, store.load())  # read back from what the check kept with the copy
    assert not seen.pending and seen.entries == want


def test_only_so_many_entries_go_to_the_page(tmp_path: Path, clock, monkeypatch) -> None:
    import watchpoint.report as report

    monkeypatch.setattr(report, "MAX_ENTRIES", 10)
    src = tmp_path / "q.csv"
    src.write_text("No.,状態\n" + "".join(f"{i},a\n" for i in range(30)), encoding="utf-8")
    t = Target(name="q", source=str(src), key="No.")
    cfg = Config(targets=[t])
    data = tmp_path / "data"
    clock()
    check_target(t, cfg, data)
    src.write_text("No.,状態\n" + "".join(f"{i},b\n" for i in range(30)), encoding="utf-8")
    clock()
    check_target(t, cfg, data)
    store = TargetStore(data, "q")
    ch = latest_changes(t, cfg, store, store.load())
    assert len(ch.entries) == 10 and ch.entries_total == 30
