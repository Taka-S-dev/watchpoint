"""Watching a Microsoft Project plan: each task compared as a record told apart by UID."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import watchpoint.check as chk
from watchpoint.check import Status, check_target
from watchpoint.config import Config, Target, load_config

HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Project xmlns="http://schemas.microsoft.com/project">'
    "<Title>次期システム開発</Title><StatusDate>{status}</StatusDate><Tasks>"
    "<Task><UID>0</UID><ID>0</ID><Name>次期システム開発</Name><OutlineLevel>0</OutlineLevel>"
    "<PercentComplete>35</PercentComplete></Task>"
)


def _task(uid: int, row: int, name: str, finish: str, percent: int = 0, after: int | None = None) -> str:
    link = f"<PredecessorLink><PredecessorUID>{after}</PredecessorUID><Type>1</Type></PredecessorLink>" if after else ""
    return (
        f"<Task><UID>{uid}</UID><ID>{row}</ID><Name>{name}</Name><OutlineNumber>{row}</OutlineNumber>"
        f"<OutlineLevel>1</OutlineLevel><Start>2026-11-02T08:00:00</Start><Finish>{finish}</Finish>"
        f"<PercentComplete>{percent}</PercentComplete>{link}</Task>"
    )


def _plan(path: Path, tasks: list[str], status: str = "2026-11-20T17:00:00") -> None:
    path.write_text(HEAD.format(status=status) + "".join(tasks) + "</Tasks></Project>", encoding="utf-8")


@pytest.fixture
def clock(monkeypatch):
    now = [dt.datetime(2026, 11, 23, 9, 0)]

    def tick() -> None:
        now[0] += dt.timedelta(minutes=40)

    monkeypatch.setattr(chk, "_now", lambda: now[0])
    return tick


def test_a_plan_is_compared_task_by_task(tmp_path: Path, clock) -> None:
    src = tmp_path / "計画.xml"  # Project XML: read without Project
    _plan(src, [_task(7, 1, "画面設計", "2026-12-10T17:00:00"), _task(9, 2, "DB 設計", "2026-12-20T17:00:00")])
    t = Target(name="計画", source=str(src), key="UID")
    data = tmp_path / "data"
    clock()
    assert check_target(t, Config(targets=[t]), data).status is Status.FIRST
    _plan(src, [_task(7, 1, "画面設計", "2026-12-18T17:00:00", 40), _task(9, 2, "DB 設計", "2026-12-20T17:00:00")],
          status="2026-11-27T17:00:00")  # fmt: skip
    clock()
    r = check_target(t, Config(targets=[t]), data)
    assert r.status is Status.CHANGED and (r.changed, r.added, r.removed) == (2, 0, 0)
    assert "[UID=7]" in r.diff and "2026-12-10 17:00 → 2026-12-18 17:00" in r.diff and "0% → 40%" in r.diff
    assert "[UID=0]" in r.diff and "2026-11-20 → 2026-11-27" in r.diff  # the status date, on the plan's own row


def test_a_task_inserted_above_others_is_only_an_addition(tmp_path: Path, clock) -> None:
    """ID is the row number and shifts for every task below an inserted row; UID does not."""
    src = tmp_path / "計画.xml"
    _plan(src, [_task(7, 1, "画面設計", "2026-12-10T17:00:00"), _task(9, 2, "DB 設計", "2026-12-20T17:00:00", after=7)])
    t = Target(name="計画", source=str(src), key="UID")
    data = tmp_path / "data"
    clock()
    check_target(t, Config(targets=[t]), data)
    _plan(
        src,
        [
            _task(15, 1, "要件の見直し", "2026-12-05T17:00:00"),
            _task(7, 2, "画面設計", "2026-12-10T17:00:00"),
            _task(9, 3, "DB 設計", "2026-12-20T17:00:00", after=7),
        ],
    )  # fmt: skip  (its predecessor's ID went 1 -> 2)
    clock()
    r = check_target(t, Config(targets=[t]), data)
    assert (r.added, r.changed, r.removed) == (1, 0, 0) and "要件の見直し" in r.diff


def test_a_mpp_is_told_apart_by_uid_without_saying_so(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "t.yaml", "targets:\n  - name: 計画\n    source: C:/share/計画.mpp\n")
    assert cfg.targets[0].key == "UID" and cfg.targets[0].is_keyed
    cfg = load_config(tmp_path / "t.yaml", "targets:\n  - name: 計画\n    source: C:/share/計画.mpp\n    key: 名前\n")
    assert cfg.targets[0].key == "名前"  # what the config says wins


def test_without_project_a_mpp_is_still_watched_by_its_bytes(tmp_path: Path, clock, monkeypatch) -> None:
    import officetext.project as project

    monkeypatch.setattr(project, "project_available", lambda: False)
    src = tmp_path / "計画.mpp"
    src.write_bytes(b"\xd0\xcf\x11\xe0 version 1")
    t = Target(name="計画", source=str(src))
    data = tmp_path / "data"
    clock()
    assert check_target(t, Config(targets=[t]), data).status is Status.FIRST
    src.write_bytes(b"\xd0\xcf\x11\xe0 version 2")
    clock()
    r = check_target(t, Config(targets=[t]), data)
    assert r.status is Status.CHANGED and "比べられない形式" in r.message
    text = (r.snapshot / "text.txt").read_text(encoding="utf-8")
    assert "Microsoft Project が要ります" in text
