"""How often targets are checked: the default interval, the floor, the check hours, and changing it from the page."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from watchpoint.check import Status, check_target
from watchpoint.config import CheckHours, ConfigError, load_config
from watchpoint.editing import set_every
from watchpoint.report import next_due
from watchpoint.store import State

CONFIG = """keep: 5
targets:
  - name: 質問表   # 自分の行
    source: 'C:\\data\\q.xlsx'
    tags: [a]
    key: No.

  - name: 仕様
    source: spec.md
    every: 2h   # 打ち合わせの前後
    points: |
      保持の日数
"""


def _load(tmp_path: Path, text: str):
    return load_config(tmp_path / "t.yaml", text)


# --------------------------------------------------------------------------- the default and the floor


def test_a_target_without_every_gets_the_default(tmp_path: Path) -> None:
    cfg = _load(tmp_path, CONFIG)
    q, spec = cfg.targets
    assert cfg.every == "30m"
    assert (q.every, q.every_is_default) == ("30m", True)
    assert (spec.every, spec.every_is_default) == ("2h", False)


def test_the_default_can_be_changed_for_all(tmp_path: Path) -> None:
    cfg = _load(tmp_path, "every: 1h\n" + CONFIG.replace("keep: 5\n", ""))
    assert [t.every for t in cfg.targets] == ["1h", "2h"]


@pytest.mark.parametrize("bad", ["1m", "4m", "0m"])
def test_too_short_is_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="5m 以上"):
        _load(tmp_path, CONFIG.replace("every: 2h", f"every: {bad}"))
    with pytest.raises(ConfigError, match="5m 以上"):
        _load(tmp_path, f"every: {bad}\n" + CONFIG)


# --------------------------------------------------------------------------- check hours


def test_check_hours() -> None:
    h = CheckHours(days="平日", time="9:00-19:00")
    mon_8 = dt.datetime(2026, 11, 23, 8, 59)  # a Monday
    assert not h.allows(mon_8) and h.allows(mon_8.replace(hour=9, minute=0))
    assert not h.allows(mon_8.replace(hour=19, minute=0))  # the end is not included
    assert h.next_start(mon_8) == dt.datetime(2026, 11, 23, 9, 0)
    fri_20 = dt.datetime(2026, 11, 27, 20, 0)
    assert h.next_start(fri_20) == dt.datetime(2026, 11, 30, 9, 0)  # over the weekend
    assert h.label == "平日 9:00〜19:00"
    assert CheckHours(days=["月", "水曜"], time="0:00-24:00").weekdays == {0, 2}


@pytest.mark.parametrize(
    ("days", "time", "msg"),
    [("祝日", "9:00-19:00", "分かりません"), ("平日", "19:00-9:00", "前に"), ("平日", "9-19", "のように")],
)
def test_check_hours_mistakes(days, time, msg) -> None:
    with pytest.raises(ValueError, match=msg):
        CheckHours(days=days, time=time)


def test_unquoted_time_is_explained(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="引用符"):
        _load(tmp_path, "check_hours: {time: 9:00}\n" + CONFIG)  # YAML reads 9:00 as a number


def test_outside_the_hours_checks_by_interval_wait(tmp_path: Path, monkeypatch) -> None:
    import watchpoint.check as check

    src = tmp_path / "m.txt"
    src.write_text("x\n", encoding="utf-8")
    text = f"check_hours: {{days: 平日, time: '9:00-19:00'}}\ntargets:\n  - name: m\n    source: '{src}'\n"
    cfg = _load(tmp_path, text)
    monkeypatch.setattr(check, "_now", lambda: dt.datetime(2026, 11, 28, 10, 0))  # a Saturday
    r = check_target(cfg.targets[0], cfg, tmp_path / "data", due_only=True)
    assert r.status == Status.NOT_DUE and "時間外（平日 9:00〜19:00）" in r.message and "11/30 09:00" in r.message
    r = check_target(cfg.targets[0], cfg, tmp_path / "data")  # asked for by hand: runs
    assert r.status == Status.FIRST


def test_next_due_moves_into_the_hours(tmp_path: Path) -> None:
    cfg = _load(tmp_path, CONFIG)
    state = State(last_checked=dt.datetime.now().replace(microsecond=0).isoformat(sep=" "))
    every_day_tonight = CheckHours(days="毎日", time="23:58-24:00")
    shown = next_due(cfg.targets[0], state, every_day_tonight)
    assert shown is not None and shown.endswith(("23:58", "23:59"))


# --------------------------------------------------------------------------- YAML slips, explained


@pytest.mark.parametrize(
    ("line", "hint"),
    [
        ("    where: {回答者: ${me}}", "引用符で囲んで"),
        ("\tkey: No.", "タブは使えません"),
        ("    source: http://x/a: b", "「: 」を含むとき"),
    ],
)
def test_yaml_mistakes_say_where_and_how(tmp_path: Path, line: str, hint: str) -> None:
    text = "targets:\n  - name: x\n    source: a.txt\n" + line + "\n"
    with pytest.raises(ConfigError) as e:
        _load(tmp_path, text)
    msg = str(e.value)
    assert "4 行目" in msg and hint in msg


# --------------------------------------------------------------------------- changing it from the page


def test_set_every_changes_only_that_value() -> None:
    out = set_every(CONFIG, "仕様", "15m")
    assert "    every: 15m   # 打ち合わせの前後\n" in out  # the comment stays
    assert out.replace("every: 15m", "every: 2h") == CONFIG


def test_set_every_adds_a_line_after_tags() -> None:
    out = set_every(CONFIG, "質問表", "1h")
    assert "    tags: [a]\n    every: 1h\n    key: No.\n" in out
    assert "  - name: 質問表   # 自分の行\n" in out


def test_set_every_none_goes_back_to_the_default(tmp_path: Path) -> None:
    out = set_every(CONFIG, "仕様", None)
    assert "every" not in out and "    source: spec.md\n    points: |\n" in out
    assert set_every(CONFIG, "質問表", None) == CONFIG  # already the default
    assert _load(tmp_path, out).targets[1].every_is_default


def test_set_every_refuses_what_it_cannot_do() -> None:
    with pytest.raises(ValueError, match="targets にありません"):
        set_every(CONFIG, "無い", "1h")
    with pytest.raises(ValueError, match="5m 以上"):
        set_every(CONFIG, "仕様", "1m")
    with pytest.raises(ValueError, match="画面からは変えられません"):
        set_every("targets:\n  - {name: x, source: y}\n", "x", "1h")


def test_page_sets_every_and_keeps_the_rest(gui) -> None:
    call, _, app = gui
    before = app.config_path.read_text(encoding="utf-8")
    status, r = call("/api/targets/every", {"name": "仕様", "every": "2h"})
    assert status == 200, r
    after = app.config_path.read_text(encoding="utf-8")
    assert after == before.replace("    tags: [案件A]\n", "    tags: [案件A]\n    every: 2h\n")
    detail = call("/api/target?name=%E4%BB%95%E6%A7%98")[1]
    assert (detail["every"], detail["every_is_default"]) == ("2h", False)
    status, r = call("/api/targets/every", {"name": "仕様", "every": None})
    assert status == 200 and app.config_path.read_text(encoding="utf-8") == before
    status, r = call("/api/targets/every", {"name": "仕様", "every": "1m"})
    assert status == 422 and "5m 以上" in r["error"]
