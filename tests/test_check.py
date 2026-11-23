from pathlib import Path

from openpyxl import Workbook

from watchpoint.check import Status, acknowledge, check_target
from watchpoint.config import Config, Target


def _book(path: Path, rows: list[list]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "質問一覧"
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_excel_new_row_is_reported_as_one_added_line(tmp_path: Path) -> None:
    src = tmp_path / "share" / "質問表.xlsx"
    src.parent.mkdir()
    header = ["No.", "質問", "回答者", "状態"]
    _book(src, [header, [1, "ログの保存先は？", "佐藤", "回答済み"]])
    before = src.read_bytes()

    t = Target(name="質問表", source=str(src), sheets=["質問一覧"])
    cfg = Config(targets=[t])
    data = tmp_path / "data"

    assert check_target(t, cfg, data).status is Status.FIRST
    assert check_target(t, cfg, data).status is Status.UNCHANGED
    assert src.read_bytes() == before  # the master is never written

    _book(src, [header, [1, "ログの保存先は？", "佐藤", "回答済み"], [2, "タイムアウトは何秒？", "佐藤", "未回答"]])
    r = check_target(t, cfg, data)
    assert r.status is Status.CHANGED
    assert (r.added, r.removed) == (1, 0)
    assert "+R3 | 2 | タイムアウトは何秒？ | 佐藤 | 未回答" in r.diff

    # The change is reported again until it is acknowledged.
    assert check_target(t, cfg, data).status is Status.CHANGED
    assert acknowledge(t, cfg, data)
    assert check_target(t, cfg, data).status is Status.UNCHANGED
    assert not acknowledge(t, cfg, data)


def test_text_file_change_and_missing_source(tmp_path: Path) -> None:
    src = tmp_path / "spec.md"
    src.write_text("# 仕様\n- A\n", encoding="utf-8")
    t = Target(name="仕様", source=str(src))
    cfg = Config(targets=[t])
    data = tmp_path / "data"

    check_target(t, cfg, data)
    src.write_text("# 仕様\n- A\n- B\n", encoding="utf-8")
    r = check_target(t, cfg, data)
    assert r.status is Status.CHANGED and "+- B" in r.diff

    src.unlink()
    r = check_target(t, cfg, data)
    assert r.status is Status.FAILED and "見つかりません" in r.message


def test_generations_are_pruned(tmp_path: Path, monkeypatch) -> None:
    import datetime as dt

    import watchpoint.check as chk

    src = tmp_path / "a.txt"
    t = Target(name="a", source=str(src))
    cfg = Config(keep=2, targets=[t])
    data = tmp_path / "data"
    base = dt.datetime(2026, 1, 1)
    for i in range(4):
        src.write_text(f"v{i}\n", encoding="utf-8")
        monkeypatch.setattr(chk, "_now", lambda i=i: base + dt.timedelta(minutes=i))
        check_target(t, cfg, data)
    snaps = sorted(p.name for p in (data / "a" / "snapshots").iterdir())
    # The unacknowledged baseline (v0) survives pruning, so the diff still starts from it.
    assert snaps == ["20260101-000000", "20260101-000300"]
    r = check_target(t, cfg, data)
    assert "-v0" in r.diff and "+v3" in r.diff


def test_same_second_checks_do_not_overwrite_the_baseline(tmp_path: Path, monkeypatch) -> None:
    import datetime as dt

    import watchpoint.check as chk

    monkeypatch.setattr(chk, "_now", lambda: dt.datetime(2026, 1, 1))
    src = tmp_path / "a.txt"
    t = Target(name="a", source=str(src))
    cfg = Config(targets=[t])
    src.write_text("old\n", encoding="utf-8")
    check_target(t, cfg, tmp_path / "data")
    src.write_text("new\n", encoding="utf-8")
    r = check_target(t, cfg, tmp_path / "data")
    assert r.status is Status.CHANGED and "-old" in r.diff
