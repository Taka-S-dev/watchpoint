"""Choosing what to watch: whole content, chosen cells, or a file's timestamp; and adding a target from a form."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from watchpoint import editing
from watchpoint.check import Status, check_target
from watchpoint.config import TEMPLATE, Config, Target, load_config
from watchpoint.store import TargetStore


def _book(path: Path, cells: dict[str, object], sheet: str = "集計") -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for ref, v in cells.items():
        ws[ref] = v
    wb.save(path)


def _run(t: Target, data: Path):
    return check_target(t, Config(targets=[t]), data)


# --------------------------------------------------------------------------- cells


def test_only_the_chosen_cells_count(tmp_path: Path) -> None:
    src = tmp_path / "b.xlsx"
    _book(src, {"F20": 100, "A1": "表題", "B3": "未"})
    t = Target(name="b", source=str(src), cells={"合計": "集計!F20", "状態": "B3"})
    data = tmp_path / "d"
    assert _run(t, data).status is Status.FIRST

    _book(src, {"F20": 100, "A1": "表題を直した", "B3": "未"})  # a cell nobody watches
    assert _run(t, data).status is Status.UNCHANGED

    _book(src, {"F20": 120, "A1": "表題を直した", "B3": "済"})
    r = _run(t, data)
    assert r.status is Status.CHANGED
    assert "変更 [セル=F20] 合計: 100 → 120" in r.diff
    assert "変更 [セル=B3] 状態: 未 → 済" in r.diff


def test_a_cell_that_becomes_empty_is_a_change(tmp_path: Path) -> None:
    src = tmp_path / "b.xlsx"
    _book(src, {"C2": "x"})
    t = Target(name="b", source=str(src), cells=["C2:C3"])
    data = tmp_path / "d"
    _run(t, data)
    _book(src, {"C3": "y"})
    r = _run(t, data)
    assert "値: x → (空)" in r.diff and "値: (空) → y" in r.diff


def test_cells_on_a_sheet_with_a_space_in_its_name(tmp_path: Path) -> None:
    src = tmp_path / "b.xlsx"
    _book(src, {"A1": 5}, sheet="月次 集計")
    t = Target(name="b", source=str(src), cells=["'月次 集計'!A1"])
    r = _run(t, tmp_path / "d")
    assert r.status is Status.FIRST
    text = TargetStore(tmp_path / "d", "b").read_text(TargetStore(tmp_path / "d", "b").load().snapshot)
    assert "## シート: 月次 集計\n[セル=A1] 値=5" in text


@pytest.mark.parametrize(
    ("cells", "extra", "message"),
    [
        (["ZZ"], {}, "セルの書き方が違います"),
        (["A1:Z1000"], {}, "多すぎます"),
        ([], {}, "cells が空です"),
        (["A1"], {"key": "No."}, "一緒に使えません"),
        ({"a|b": "A1"}, {}, "使えません"),
    ],
)
def test_cells_mistakes_are_explained(tmp_path: Path, cells, extra, message) -> None:
    with pytest.raises(ValidationError, match=message):
        Target(name="b", source=str(tmp_path / "b.xlsx"), cells=cells, **extra)


def test_cells_only_for_excel(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Excel"):
        Target(name="b", source=str(tmp_path / "b.docx"), cells=["A1"])


# --------------------------------------------------------------------------- timestamp


def test_timestamp_reports_a_save_without_copying(tmp_path: Path) -> None:
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 10)
    t = Target(name="t", source=str(src), watch="timestamp", max_size_mb=0.000001)  # the size limit is moot
    data = tmp_path / "d"
    assert _run(t, data).status is Status.FIRST
    assert _run(t, data).status is Status.UNCHANGED

    src.write_bytes(b"y" * 12)
    st = src.stat()
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    r = _run(t, data)
    assert r.status is Status.CHANGED
    assert "サイズ: 10 → 12" in r.diff and "更新:" in r.diff
    store = TargetStore(data, "t")
    assert not list(store.snapshot_dir(store.load().snapshot).glob("original*"))


def test_timestamp_is_for_one_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="1つのファイル"):
        Target(name="t", source=str(tmp_path), watch="timestamp")
    with pytest.raises(ValidationError, match="1つのファイル"):
        Target(name="t", source="https://example.com/", watch="timestamp")


def test_changing_the_way_of_watching_takes_a_new_baseline(tmp_path: Path) -> None:
    src = tmp_path / "a.txt"
    src.write_text("a\n", encoding="utf-8")
    data = tmp_path / "d"
    _run(Target(name="a", source=str(src)), data)
    r = _run(Target(name="a", source=str(src), watch="timestamp"), data)
    assert r.status is Status.FIRST and "設定か場所が変わった" in r.message


# --------------------------------------------------------------------------- adding a target as text


ITEM = {
    "name": "集計表",
    "source": r"\\server\share\集計.xlsx",
    "cells": {"合計": "集計!F20"},
    "tags": ["案件A"],
    "points": "合計が変わったら理由を探す。\n前の値も書く。",
}


def test_add_target_keeps_the_rest_of_the_file(tmp_path: Path) -> None:
    text = TEMPLATE + "\n# おわり\nkeep_after: 1\n"
    out = editing.add_target(text, editing.clean_item(ITEM))
    assert out.startswith(TEMPLATE.split("targets:")[0])  # comments above are untouched
    assert out.index("- name: 集計表") < out.index("# おわり") < out.index("keep_after")
    assert "    points: |\n      合計が変わったら理由を探す。\n      前の値も書く。\n" in out
    assert "    source: \\\\server\\share\\集計.xlsx\n" in out
    assert "    cells: {合計: 集計!F20}\n" in out


def test_added_target_loads(tmp_path: Path) -> None:
    cfg = tmp_path / "t.yaml"
    out = editing.add_target(TEMPLATE, editing.clean_item({**ITEM, "where": None, "every": "30m"}))
    t = next(t for t in load_config(cfg, out).targets if t.name == "集計表")
    assert t.cells == {"合計": "集計!F20"} and t.every == "30m" and t.points.startswith("合計が")


@pytest.mark.parametrize(
    "text",
    ["", "keep: 5\n", "keep: 5\ntargets: []\n", "targets:\n- name: a\n  source: a.txt\n"],
)
def test_add_target_to_any_shape_of_list(tmp_path: Path, text: str) -> None:
    out = editing.add_target(text, {"name": "b", "source": "b.txt"})
    names = [t.name for t in load_config(tmp_path / "t.yaml", out).targets]
    assert names[-1] == "b"


def test_add_target_refuses_a_flow_list() -> None:
    with pytest.raises(ValueError, match="画面からは足せません"):
        editing.add_target("targets: [{name: a, source: a.txt}]\n", {"name": "b", "source": "b.txt"})


def test_clean_item_drops_blanks_and_refuses_unknown_keys() -> None:
    assert editing.clean_item({"source": "a", "name": " x ", "key": "", "columns": []}) == {"name": "x", "source": "a"}
    with pytest.raises(ValueError, match="知らない項目"):
        editing.clean_item({"name": "x", "source": "a", "wehre": {}})


# --------------------------------------------------------------------------- the page


def test_page_adds_a_target_without_saving(gui) -> None:
    call, src, app = gui
    before = app.config_path.read_bytes()
    text = call("/api/config")[1]["text"]
    status, r = call(
        "/api/config/add-target", {"text": text, "item": {"name": "時刻", "source": str(src), "watch": "timestamp"}}
    )
    assert status == 200, r
    assert r["ok"] and r["targets"] == ["仕様", "時刻"] and "watch: timestamp" in r["text"]
    assert app.config_path.read_bytes() == before
    status, p = call("/api/preview", {"name": "時刻", "text": r["text"]})
    assert status == 200 and "[ファイル=spec.md]" in p["text"]


def test_page_explains_a_bad_target(gui) -> None:
    call, src, _ = gui
    text = call("/api/config")[1]["text"]
    status, r = call("/api/config/add-target", {"text": text, "item": {"name": "仕様", "source": str(src)}})
    assert status == 422 and "もうあります" in r["error"]
    status, r = call(
        "/api/config/add-target", {"text": text, "item": {"name": "x", "source": str(src), "cells": ["A1"]}}
    )
    assert status == 422 and "Excel" in r["error"]


def test_page_describes_a_source(gui, tmp_path: Path) -> None:
    call, src, _ = gui
    assert call("/api/config/describe", {"source": str(src)})[1]["timestamp"] is True
    r = call("/api/config/describe", {"source": str(tmp_path / "q.xlsx")})[1]
    assert r["excel"] and r["table"]
    r = call("/api/config/describe", {"source": str(tmp_path)})[1]
    assert r["kind"] == "folder" and not r["timestamp"]
    assert call("/api/config/describe", {"source": " "})[1]["kind"] is None
