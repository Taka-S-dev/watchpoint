"""Changes that used to be missed or misreported: each case was reproduced before it was fixed."""

import datetime as dt
import os
from pathlib import Path

from watchpoint.check import Status, check_target, is_due
from watchpoint.config import Config, Target
from watchpoint.store import State, TargetStore


def _run(t: Target, data: Path, **cfg):
    return check_target(t, Config(targets=[t], **cfg), data)


def _xlsx(path: Path, rows: list[list]) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    wb.save(path)


def test_content_lines_starting_with_dashes_are_counted(tmp_path: Path) -> None:
    src = tmp_path / "memo.md"
    src.write_text("# 予定\n-- 期限: 11/20\n", encoding="utf-8")
    t = Target(name="m", source=str(src))
    _run(t, tmp_path / "d")
    src.write_text("# 予定\n-- 期限: 11/27\n", encoding="utf-8")
    r = _run(t, tmp_path / "d")
    assert r.status is Status.CHANGED and (r.added, r.removed) == (1, 1)


def test_a_line_separator_inside_a_cell_keeps_the_record_whole(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    _xlsx(src, [["No.", "質問"], [1, "質問A 期限は11/20"]])
    t = Target(name="q", source=str(src), key="No.")
    _run(t, tmp_path / "d")
    _xlsx(src, [["No.", "質問"], [1, "質問A 期限は12/01"]])
    r = _run(t, tmp_path / "d")
    assert r.status is Status.CHANGED and r.diff.count("変更") == 1


def test_equal_text_in_neighbouring_word_cells_is_two_cells(tmp_path: Path) -> None:
    from docx import Document

    src = tmp_path / "a.docx"

    def save(cells: list[str]) -> None:
        d = Document()
        row = d.add_table(rows=1, cols=3).rows[0]
        for c, v in zip(row.cells, cells, strict=True):
            c.text = v
        d.save(src)

    save(["○", "○", "×"])
    t = Target(name="w", source=str(src))
    _run(t, tmp_path / "d")
    save(["○", "×", "×"])
    assert _run(t, tmp_path / "d").status is Status.CHANGED


def test_a_repeated_key_never_takes_the_name_of_a_real_one(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    _xlsx(src, [["No.", "状態"], ["15", "未"], ["15", "未"], ["15#2", "未"]])
    t = Target(name="q", source=str(src), key="No.")
    _run(t, tmp_path / "d")
    _xlsx(src, [["No.", "状態"], ["15", "未"], ["15", "済"], ["15#2", "未"]])
    r = _run(t, tmp_path / "d")
    assert r.status is Status.CHANGED and "済" in r.diff


def test_a_real_header_starting_with_retsu_is_compared(tmp_path: Path) -> None:
    src = tmp_path / "t.xlsx"
    _xlsx(src, [["No.", "列車番号"], [1, "101"]])
    t = Target(name="t", source=str(src), key="No.")
    _run(t, tmp_path / "d")
    _xlsx(src, [["No.", "列車番号"], [1, "102"]])
    assert _run(t, tmp_path / "d").status is Status.CHANGED


def test_a_value_under_a_blank_header_is_a_change_named_by_its_column_letter(tmp_path: Path) -> None:
    """A value typed to the right of the table, where the header row is empty, was read past unseen."""
    src = tmp_path / "q.xlsx"
    _xlsx(src, [["No.", "状態"], [1, "未"], [2, "未"]])
    t = Target(name="q", source=str(src), key="No.")
    _run(t, tmp_path / "d")
    _xlsx(src, [["No.", "状態"], [1, "未"], [2, "未", None, "メモ"]])
    r = _run(t, tmp_path / "d")
    assert r.status is Status.CHANGED and r.entries == [
        {
            "kind": "changed",
            "key": "No.=2",
            "cells": [["D列", "", "メモ"]],
            "row": [["状態", "未"], ["D列", "メモ"]],
            "section": "シート: Sheet",
        }
    ]
    # an empty column with no header adds nothing, and the column line does not count it as a column added
    assert "列　" not in r.diff


def test_seconds_in_a_time_are_kept(tmp_path: Path) -> None:
    src = tmp_path / "t.xlsx"
    _xlsx(src, [["No.", "時刻"], [1, dt.datetime(2026, 11, 23, 0, 0, 0)]])
    t = Target(name="t", source=str(src), key="No.")
    _run(t, tmp_path / "d")
    _xlsx(src, [["No.", "時刻"], [1, dt.datetime(2026, 11, 23, 0, 0, 30)]])
    assert _run(t, tmp_path / "d").status is Status.CHANGED


def test_utf16_text_files_are_read_as_text(tmp_path: Path) -> None:
    src = tmp_path / "u.txt"
    src.write_bytes("予定: 11/20\n".encode("utf-16"))
    r = _run(Target(name="u", source=str(src)), tmp_path / "d")
    assert (r.snapshot / "text.txt").read_text(encoding="utf-8") == "予定: 11/20\n"


def test_ignore_applies_to_level_two_headings_in_documents(tmp_path: Path) -> None:
    src = tmp_path / "m.md"
    src.write_text("## 最終更新 11/20\n本文\n", encoding="utf-8")
    t = Target(name="m", source=str(src), ignore=["最終更新"])
    _run(t, tmp_path / "d")
    src.write_text("## 最終更新 11/21\n本文\n", encoding="utf-8")
    assert _run(t, tmp_path / "d").status is Status.UNCHANGED


def test_a_check_time_in_the_future_does_not_stop_the_target() -> None:
    t = Target(name="x", source="x", every="1h")
    future = (dt.datetime.now() + dt.timedelta(days=1)).isoformat(sep=" ")
    assert is_due(t, State(last_checked=future))[0]


def test_saving_the_same_text_again_is_not_a_new_change(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    t = Target(name="q", source=str(src), key="No.")
    data = tmp_path / "d"
    _xlsx(src, [["No.", "状態"], [1, "未"]])
    _run(t, data)
    _xlsx(src, [["No.", "状態"], [1, "済"]])
    assert _run(t, data).status is Status.CHANGED
    before = TargetStore(data, t.store_key).load().history
    _xlsx(src, [["No.", "状態"], [1, "済"]])  # saved again: new bytes, same content
    os.utime(src, (src.stat().st_atime, src.stat().st_mtime + 5))
    assert _run(t, data).status is Status.CHANGED
    # No new generation: the same unacknowledged change is not notified a second time.
    assert TargetStore(data, t.store_key).load().history == before


def test_changing_settings_with_an_unseen_change_says_so(tmp_path: Path) -> None:
    from watchpoint.check import send_notifications

    src = tmp_path / "m.txt"
    src.write_text("a\n", encoding="utf-8")
    drop = tmp_path / "drop"
    data = tmp_path / "d"
    t = Target(name="m", source=str(src))
    check_target(t, Config(targets=[t]), data)
    src.write_text("a\n重要な追記\n", encoding="utf-8")
    check_target(t, Config(targets=[t]), data)  # an unacknowledged change
    t2 = Target(name="m", source=str(src), ignore=["^無関係$"])  # the settings change
    cfg = Config(targets=[t2], notify={"drop_folder": str(drop)})
    r = check_target(t2, cfg, data)
    assert r.status is Status.FIRST and r.warning and r.needs_attention
    assert "その前の未確認の変更" in r.message and "確認済みにするまで残して" in r.message
    assert send_notifications([r], cfg, data) == 1


def test_a_failing_target_still_shows_its_unseen_change(tmp_path: Path) -> None:
    from watchpoint.report import saved_results

    src = tmp_path / "m.txt"
    src.write_text("a\n", encoding="utf-8")
    t = Target(name="m", source=str(src), fail_after=1)
    cfg = Config(targets=[t])
    data = tmp_path / "d"
    check_target(t, cfg, data)
    src.write_text("a\nb\n", encoding="utf-8")
    check_target(t, cfg, data)
    src.unlink()
    r = check_target(t, cfg, data)
    assert r.status is Status.FAILED and "未確認の変更が残っています" in r.message
    (saved,) = saved_results([t], cfg, data)
    assert saved.status is Status.CHANGED and "最新の取得は失敗" in saved.message


def test_cli_ack_with_seen_leaves_a_newer_change_alone(tmp_path: Path) -> None:
    import json

    from typer.testing import CliRunner

    from watchpoint.cli import app

    runner = CliRunner()
    src = tmp_path / "m.txt"
    src.write_text("a\n", encoding="utf-8")
    cfg = tmp_path / "t.yaml"
    cfg.write_text(f"targets:\n  - name: m\n    source: '{src}'\n", encoding="utf-8")
    c = ["-c", str(cfg)]
    runner.invoke(app, ["check", *c])
    src.write_text("a\nb\n", encoding="utf-8")
    shown = json.loads(runner.invoke(app, ["check", *c, "-f", "json"]).output)[0]["generation"]
    src.write_text("a\nb\nc 重要\n", encoding="utf-8")
    runner.invoke(app, ["check", *c])  # a scheduled run brings a newer copy
    refused = runner.invoke(app, ["ack", "m", "--seen", shown, *c])
    assert refused.exit_code == 1
    md = runner.invoke(app, ["check", *c, "-f", "md"]).output
    assert "c 重要" in md and "--seen" in md
    newest = json.loads(runner.invoke(app, ["check", *c, "-f", "json"]).output)[0]["generation"]
    assert runner.invoke(app, ["ack", "m", "--seen", newest, *c]).exit_code == 0


def test_a_title_row_above_the_table_gets_an_actionable_message(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    _xlsx(src, [["〇〇案件 質問表"], [], ["No.", "質問"], [1, "ログは？"]])
    r = _run(Target(name="q", source=str(src), key="No.", fail_after=1), tmp_path / "d")
    assert r.status is Status.FAILED and "見出しは 3 行目にあります" in r.message and "header_row: auto" in r.message
    assert _run(Target(name="q", source=str(src), key="No.", header_row="auto"), tmp_path / "d2").status is Status.FIRST
