"""What counts as a change, and how it is shown."""

import datetime as dt
from pathlib import Path

from openpyxl import Workbook

import watchpoint.check as chk
from watchpoint.check import Status, acknowledge, check_target
from watchpoint.config import Config, Target


def _run(t: Target, data: Path, **cfg):
    return check_target(t, Config(targets=[t], **cfg), data)


def _md(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_report_added_only_ignores_removals_and_moves_the_baseline(tmp_path: Path) -> None:
    d = tmp_path / "議事録"
    d.mkdir()
    (d / "a.docx").write_bytes(b"1")
    (d / "b.docx").write_bytes(b"1")
    t = Target(name="f", source=str(d / "*.docx"), report=["added"])
    data = tmp_path / "data"
    _run(t, data)
    (d / "a.docx").unlink()
    r = _run(t, data)
    assert r.status is Status.FILTERED
    (d / "c.docx").write_bytes(b"1")
    r = _run(t, data)
    assert r.status is Status.CHANGED and r.added == 1 and "a.docx" not in r.diff


def test_match_counts_only_changes_that_mention_something(tmp_path: Path) -> None:
    src = tmp_path / "spec.md"
    t = Target(name="s", source=str(src), match=["ログ"])
    data = tmp_path / "data"
    _md(src, ["# 仕様", "画面は青"])
    _run(t, data)
    _md(src, ["# 仕様", "画面は赤"])
    assert _run(t, data).status is Status.FILTERED
    _md(src, ["# 仕様", "画面は赤", "ログは30日"])
    r = _run(t, data)
    assert r.status is Status.CHANGED and "+ログは30日" in r.diff and "画面は青" not in r.diff


def test_sections_include_normalize_and_sort(tmp_path: Path) -> None:
    src = tmp_path / "spec.md"
    data = tmp_path / "data"
    t = Target(name="s", source=str(src), sections=["ログ"], normalize=["whitespace", "nfkc"], sort_lines=True)
    _md(src, ["# 画面", "色は青", "# ログ", "保持  ３０日", "保存先 D:", "# 画面2", "x"])
    _run(t, data)
    # Outside the section, full/half width and spacing, and order: none of these count.
    _md(src, ["# 画面", "色は赤", "# ログ", "保存先 D:", "保持 30日", "# 画面2", "y"])
    assert _run(t, data).status is Status.UNCHANGED
    _md(src, ["# 画面", "色は赤", "# ログ", "保存先 E:", "保持 30日", "# 画面2", "y"])
    r = _run(t, data)
    assert r.status is Status.CHANGED and "+保存先 E:" in r.diff

    t2 = Target(name="s2", source=str(src), include=[r"^保"])
    _run(t2, data)
    _md(src, ["# 画面", "色は緑", "# ログ", "保存先 E:", "保持 30日", "# 画面2", "z"])
    assert _run(t2, data).status is Status.UNCHANGED


def test_inline_changes_mark_what_changed_inside_a_line(tmp_path: Path) -> None:
    src = tmp_path / "spec.md"
    t = Target(name="s", source=str(src))
    data = tmp_path / "data"
    _md(src, ["# ログ", "保持は 30 日、保存先は D:"])
    _run(t, data)
    _md(src, ["# ログ", "保持は 60 日、保存先は D:"])
    r = _run(t, data)
    assert r.inline == ["保持は [30→60] 日、保存先は D:"]


def _book(path: Path, header: list, rows: list[list], title_rows: int = 0) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "質問一覧"
    for i in range(title_rows):
        ws.append([f"タイトル行{i + 1}"])
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_header_row_auto_and_column_changes_reported_once(tmp_path: Path) -> None:
    src = tmp_path / "q.xlsx"
    _book(src, ["No.", "質問", "状態"], [[1, "a", "未回答"], [2, "b", "未回答"]], title_rows=2)
    t = Target(name="q", source=str(src), key="No.", header_row="auto")
    data = tmp_path / "data"
    assert _run(t, data).status is Status.FIRST
    _book(
        src,
        ["No.", "質問", "状態", "回答期限"],
        [[1, "a", "未回答", "12/1"], [2, "b", "回答済み", "12/1"]],
        title_rows=3,
    )
    r = _run(t, data)
    assert "列　 追加 回答期限" in r.diff
    assert "変更 [No.=2] 状態: 未回答 → 回答済み" in r.diff
    assert r.changed == 1  # the new column did not turn every row into a change


def test_formulas_option(tmp_path: Path) -> None:
    src = tmp_path / "f.xlsx"
    wb = Workbook()
    wb.active.append(["=1+1"])
    wb.save(src)
    t = Target(name="f", source=str(src), formulas=True)
    r = _run(t, tmp_path / "data")
    assert "=1+1" in (r.snapshot / "text.txt").read_text(encoding="utf-8")


def test_due_only_checks_targets_whose_interval_passed(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "a.md"
    _md(src, ["x"])
    t = Target(name="a", source=str(src), every="30m")
    data = tmp_path / "data"
    base = dt.datetime(2026, 1, 1, 9, 0)
    monkeypatch.setattr(chk, "_now", lambda: base)
    check_target(t, Config(targets=[t]), data, due_only=True)
    monkeypatch.setattr(chk, "_now", lambda: base + dt.timedelta(minutes=10))
    assert check_target(t, Config(targets=[t]), data, due_only=True).status is Status.NOT_DUE
    monkeypatch.setattr(chk, "_now", lambda: base + dt.timedelta(minutes=31))
    assert check_target(t, Config(targets=[t]), data, due_only=True).status is Status.UNCHANGED


def test_fail_after_waits_for_repeated_failures(tmp_path: Path) -> None:
    t = Target(name="a", source=str(tmp_path / "none.md"), fail_after=3)
    data = tmp_path / "data"
    assert _run(t, data).status is Status.RETRYING
    assert _run(t, data).status is Status.RETRYING
    r = _run(t, data)
    assert r.status is Status.FAILED and "3 回続けて" in r.message and r.new_since_notified
    assert _run(t, data).new_since_notified  # still owed: nothing has written a notice yet


def test_id_keeps_history_across_a_rename(tmp_path: Path) -> None:
    src = tmp_path / "a.md"
    _md(src, ["x"])
    data = tmp_path / "data"
    _run(Target(name="古い名前", id="spec", source=str(src)), data)
    _md(src, ["y"])
    assert _run(Target(name="新しい名前", id="spec", source=str(src)), data).status is Status.CHANGED


def test_max_size(tmp_path: Path) -> None:
    src = tmp_path / "big.txt"
    src.write_bytes(b"x" * 2 * 1024 * 1024)
    r = _run(Target(name="b", source=str(src), max_size_mb=1), tmp_path / "data")
    assert r.status is Status.FAILED and "大きすぎます" in r.message


def test_ack_note_and_compare_any_two_generations(tmp_path: Path) -> None:
    from watchpoint.store import TargetStore

    src = tmp_path / "a.md"
    t = Target(name="a", source=str(src))
    cfg = Config(targets=[t])
    data = tmp_path / "data"
    for i, body in enumerate(["v1", "v2", "v3"]):
        _md(src, [body])
        check_target(t, cfg, data)
        acknowledge(t, cfg, data, note=f"見た{i}")
    store = TargetStore(data, "a")
    state = store.load()
    assert state.notes[state.snapshot] == "見た2"
    r = chk.compare(t, cfg, store, state, base_id=store.generation(state, 3), new_id=store.generation(state, 1))
    assert "-v1" in r.diff and "+v3" in r.diff
