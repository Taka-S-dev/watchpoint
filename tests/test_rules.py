"""要注意: deadlines a source should have been updated by, and rules its values should follow."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from openpyxl import Workbook

from watchpoint import editing
from watchpoint.check import Status, check_target
from watchpoint.config import ConfigError, Target, load_config
from watchpoint.report import as_markdown, saved_results
from watchpoint.rules import deadline_alert, field_stats, rule_alerts
from watchpoint.store import State, TargetStore
from watchpoint.timewords import Deadline, parse_deadline

ROWS = """## シート: 質問一覧
@列 質問 | 状態 | 回答
[No.=1] 質問=ログは？ | 状態=回答済 | 回答=90 日
[No.=2] 質問=保持は？ | 状態=回答済
[No.=3] 質問=画面は？ | 状態=保留
[No.=4] 質問=権限は？ | 状態=未回答
"""


def _t(**kw) -> Target:
    return Target(name="q", source="q.xlsx", key="No.", **kw)


# --------------------------------------------------------------------------- deadlines


@pytest.mark.parametrize(
    ("text", "kind", "day", "minutes"),
    [("毎週金 17:00", "weekly", 4, 1020), ("毎週 金曜日 9:30", "weekly", 4, 570), ("平日 9:00", "weekdays", 0, 540),
     ("毎日 8:00", "daily", 0, 480), ("毎月25日 17:00", "monthly", 25, 1020)],
)  # fmt: skip
def test_deadlines_in_calendar_words(text, kind, day, minutes) -> None:
    d = parse_deadline(text)
    assert (d.kind, d.minutes) == (kind, minutes) and (d.day == day or kind in ("daily", "weekdays"))


@pytest.mark.parametrize("bad", ["毎週金", "金曜 17:00", "毎月31日 9:00", "毎日 25:00", "そのうち"])
def test_deadlines_that_cannot_be_read_say_how_to_write_them(bad) -> None:
    with pytest.raises(ValueError, match="毎週金 17:00|1〜28|0:00"):
        parse_deadline(bad)


def _state(changed: dt.datetime | None, started: dt.datetime) -> State:
    return State(
        snapshot="s", last_checked=started.isoformat(sep=" "), history=[started.strftime("%Y%m%d-%H%M%S")],
        last_changed=changed.isoformat(sep=" ") if changed else None,
    )  # fmt: skip


def test_a_weekly_deadline(tmp_path: Path) -> None:
    t = _t(expect_update="毎週金 17:00")
    store = TargetStore(tmp_path, "q")
    fri = dt.datetime(2026, 11, 27, 17, 0)  # a Friday
    started = dt.datetime(2026, 11, 1, 9, 0)
    # changed on Wednesday this week: in time
    assert (
        deadline_alert(t, _state(dt.datetime(2026, 11, 25, 10, 0), started), store, fri + dt.timedelta(hours=1)) is None
    )
    # last changed the week before: late, said with the deadline and the last change
    late = deadline_alert(t, _state(dt.datetime(2026, 11, 18, 10, 0), started), store, fri + dt.timedelta(hours=1))
    assert late and "11/27 17:00" in late and "11/18 10:00" in late
    # before Friday 17:00 nothing is due yet
    assert (
        deadline_alert(t, _state(dt.datetime(2026, 11, 18, 10, 0), started), store, fri - dt.timedelta(hours=1)) is None
    )
    # watching began inside the period: whether it was updated before is unknown, so no warning
    assert deadline_alert(t, _state(None, dt.datetime(2026, 11, 24, 9, 0)), store, fri + dt.timedelta(hours=1)) is None


def test_an_interval_deadline(tmp_path: Path) -> None:
    t = _t(expect_update="3d")
    now = dt.datetime(2026, 11, 27, 12, 0)
    store = TargetStore(tmp_path, "q")
    started = dt.datetime(2026, 11, 1)
    assert deadline_alert(t, _state(now - dt.timedelta(days=2), started), store, now) is None
    late = deadline_alert(t, _state(now - dt.timedelta(days=5), started), store, now)
    assert late and "3 日ごと" in late and "5 日" in late


def test_the_previous_deadline_skips_days_off() -> None:
    d: Deadline = parse_deadline("平日 9:00")
    monday = dt.datetime(2026, 11, 30, 10, 0)
    assert d.last_before(monday) == dt.datetime(2026, 11, 30, 9, 0)
    assert d.last_before(dt.datetime(2026, 11, 30, 8, 0)) == dt.datetime(2026, 11, 27, 9, 0)  # back to Friday


# --------------------------------------------------------------------------- rules on rows


def test_required_when_a_row_matches() -> None:
    t = _t(rules=[{"field": "回答", "required": True, "when": {"状態": "回答済"}}])
    assert rule_alerts(t, ROWS) == ["No. 2: 回答 が空です（状態 が 回答済）"]


def test_one_of_and_pattern_and_own_words() -> None:
    t = _t(rules=[
        {"field": "状態", "one_of": ["未回答", "回答済"]},
        {"field": "回答", "pattern": r"^\d+ 日$"},
        {"field": "回答", "required": True, "when": {"状態": "回答済"}, "message": "回答漏れ"},
    ])  # fmt: skip
    assert rule_alerts(t, ROWS) == [
        "No. 3: 状態 が「保留」です（未回答・回答済 のどれかのはず）",
        "No. 2: 回答漏れ",
    ]


def test_a_rule_on_a_column_that_is_not_there_says_so() -> None:
    t = _t(rules=[{"field": "承認", "required": True}])
    assert "「承認」という列・項目が見当たりません" in rule_alerts(t, ROWS)[0]


def test_rules_on_named_cells_including_an_empty_one(tmp_path: Path) -> None:
    t = Target(name="c", source="s.xlsx", cells={"合計": "集計!C6", "承認": "集計!C8"},
               rules=[{"field": "承認", "required": True, "message": "承認がまだです"}])  # fmt: skip
    text = "## シート: 集計\n[セル=C6] 合計=135\n[セル=C8]\n"
    assert rule_alerts(t, text) == ["C8: 承認がまだです"]
    assert rule_alerts(t, "## シート: 集計\n[セル=C6] 合計=135\n[セル=C8] 承認=山田\n") == []


def test_rules_only_where_rows_or_cells_are_compared(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="rules は、行ごとに見る見張り"):
        load_config(
            tmp_path / "t.yaml", "targets:\n  - name: x\n    source: a.md\n    rules: [{field: a, required: true}]\n"
        )
    with pytest.raises(ConfigError, match="required・one_of・pattern"):
        load_config(
            tmp_path / "t.yaml", "targets:\n  - name: x\n    source: a.xlsx\n    key: No.\n    rules: [{field: a}]\n"
        )


def test_field_stats_for_proposing_rules() -> None:
    stats = {f["field"]: f for f in field_stats(ROWS)}
    assert stats["回答"]["empty"] == 3 and stats["回答"]["empty_in"] == ["No. 2", "No. 3", "No. 4"]
    assert [v["value"] for v in stats["状態"]["values"]] == ["回答済", "保留", "未回答"]


# --------------------------------------------------------------------------- editing the config


CONFIG = """targets:
  - name: 質問表   # 自分の行
    source: q.xlsx
    key: No.
    points: |
      状態を見る
    notify_when: 新しい質問

  - name: 仕様
    source: spec.md
"""


def test_adding_and_removing_rules_keeps_the_rest(tmp_path: Path) -> None:
    out = editing.add_rule(CONFIG, "質問表", {"field": "回答", "required": True, "when": {"状態": "回答済"}})
    out = editing.add_rule(out, "質問表", {"field": "状態", "one_of": ["未回答", "回答済"]})
    out = editing.set_expect_update(out, "質問表", "毎週金 17:00")
    assert "  - name: 質問表   # 自分の行\n" in out and "  - name: 仕様\n    source: spec.md\n" in out
    assert "    notify_when: 新しい質問\n    expect_update: 毎週金 17:00\n    rules:\n      - field: 回答\n" in out
    cfg = load_config(tmp_path / "t.yaml", out)
    assert [r.field for r in cfg.targets[0].rules] == ["回答", "状態"] and cfg.targets[
        0
    ].expect_update == "毎週金 17:00"
    back = editing.set_expect_update(
        editing.remove_rule(editing.remove_rule(out, "質問表", 1), "質問表", 0), "質問表", None
    )
    assert back == CONFIG
    with pytest.raises(ValueError, match="同じルール"):
        editing.add_rule(
            editing.add_rule(CONFIG, "質問表", {"field": "回答", "required": True}),
            "質問表",
            {"field": "回答", "required": True},
        )


# --------------------------------------------------------------------------- through a check


def _book(path: Path, approval: str | None) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "集計"
    ws["C6"] = 120
    if approval:
        ws["C8"] = approval
    wb.save(path)


def test_a_check_finds_alerts_notifies_once_and_clears_them(tmp_path: Path) -> None:
    from watchpoint.check import send_notifications

    book = tmp_path / "s.xlsx"
    _book(book, None)
    drop = tmp_path / "drop"
    text = (
        f"notify: {{drop_folder: '{drop}'}}\ntargets:\n  - name: 集計\n    source: '{book}'\n"
        "    cells: {合計: 集計!C6, 承認: 集計!C8}\n    rules: [{field: 承認, required: true, message: 承認がまだです}]\n"
    )
    cfg = load_config(tmp_path / "t.yaml", text)
    data = tmp_path / "data"
    r = check_target(cfg.targets[0], cfg, data)
    assert r.status is Status.FIRST and r.alerts == ["C8: 承認がまだです"] and r.new_alerts == r.alerts
    assert send_notifications([r], cfg, data) == 1
    r = check_target(cfg.targets[0], cfg, data)
    assert r.alerts and not r.new_alerts and send_notifications([r], cfg, data) == 0  # not repeated
    md = as_markdown(saved_results(cfg.targets, cfg, data))
    assert "要注意 1 件" in md and "### 要注意" in md and "- C8: 承認がまだです" in md
    _book(book, "山田")
    r = check_target(cfg.targets[0], cfg, data)
    assert r.alerts == [] and saved_results(cfg.targets, cfg, data)[0].alerts == []


def test_page_and_cli_add_rules_after_showing_what_they_would_flag(gui, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from watchpoint.cli import app

    call, _, a = gui
    book = tmp_path / "s.xlsx"
    _book(book, None)
    a.config_path.write_text(
        f"targets:\n  - name: 集計\n    source: '{book}'\n    cells: {{合計: 集計!C6, 承認: 集計!C8}}\n",
        encoding="utf-8",
    )
    call.check({"names": ["集計"]})
    fields = call("/api/fields?name=%E9%9B%86%E8%A8%88")[1]["fields"]
    assert [f["field"] for f in fields] == ["合計"]  # an empty cell has no value to show
    rule = {"field": "承認", "required": True}
    assert call("/api/rules/try", {"name": "集計", "rule": rule})[1]["alerts"] == ["C8: 承認 が空です"]
    assert call("/api/rules/add", {"name": "集計", "rule": rule})[0] == 200
    detail = call("/api/target?name=%E9%9B%86%E8%A8%88")[1]
    assert detail["rules"] == [rule] and detail["alerts"] == ["C8: 承認 が空です"] and detail["can_have_rules"]
    assert call("/api/rules/expect", {"name": "集計", "when": "毎週"})[0] == 422
    r = CliRunner().invoke(app, ["expect", "集計", "平日 9:00", "-c", str(a.config_path)])
    assert r.exit_code == 0 and load_config(a.config_path).targets[0].expect_update == "平日 9:00"
    r = CliRunner().invoke(app, ["rule", "remove", "集計", "1", "-c", str(a.config_path)])
    assert r.exit_code == 0 and load_config(a.config_path).targets[0].rules == []
