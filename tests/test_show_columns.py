"""show: columns read beside a changed row (who answers it, by when) but never compared."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from watchpoint.check import Status, check_target
from watchpoint.config import Config, Target
from watchpoint.editing import clean_item

HEAD = "No.,質問,ステータス,回答者,期限\n"


def _write(path: Path, rows: list[str]) -> None:
    path.write_text(HEAD + "".join(r + "\n" for r in rows), encoding="utf-8")


def _target(path: Path, **kw) -> Target:
    return Target(name="質問一覧", source=str(path), **{"key": "No.", "columns": ["質問", "ステータス"], **kw})


def _check(t: Target, data: Path):
    return check_target(t, Config(targets=[t]), data)


def test_a_change_comes_with_the_columns_to_read_beside_it(tmp_path: Path) -> None:
    src, data = tmp_path / "q.csv", tmp_path / "data"
    t = _target(src, show=["回答者", "期限"])
    _write(src, ["1,,,,", "2,,,佐藤,12/5"])
    assert _check(t, data).status is Status.FIRST

    _write(src, ["1,,,,", "2,保存期間は？,回答待ち,佐藤,12/5"])
    r = _check(t, data)
    assert r.status is Status.CHANGED
    assert "変更 [No.=2] 質問: (空) → 保存期間は？ / ステータス: (空) → 回答待ち（回答者: 佐藤 / 期限: 12/5）" in r.diff
    (entry,) = r.entries
    assert entry["cells"] == [["質問", "", "保存期間は？"], ["ステータス", "", "回答待ち"]]
    assert entry["context"] == [["回答者", "佐藤"], ["期限", "12/5"]]


def test_a_change_in_a_column_only_read_is_not_told(tmp_path: Path) -> None:
    src, data = tmp_path / "q.csv", tmp_path / "data"
    t = _target(src, show=["回答者"])
    _write(src, ["1,保存期間は？,回答待ち,佐藤,"])
    _check(t, data)
    _write(src, ["1,保存期間は？,回答待ち,鈴木,"])  # only who answers it changed
    r = _check(t, data)
    assert r.status is Status.FILTERED and "見るだけの列" in r.message
    _write(src, ["1,保存期間は？,回答済,鈴木,"])
    r = _check(t, data)
    assert r.status is Status.CHANGED and "（回答者: 鈴木）" in r.diff  # the value now, beside the change


def test_without_columns_every_other_column_is_watched(tmp_path: Path) -> None:
    src, data = tmp_path / "q.csv", tmp_path / "data"
    t = Target(name="質問一覧", source=str(src), key="No.", show=["期限"])
    _write(src, ["1,保存期間は？,回答待ち,佐藤,12/5"])
    _check(t, data)
    _write(src, ["1,保存期間は？,回答待ち,鈴木,12/9"])
    r = _check(t, data)
    assert r.status is Status.CHANGED
    assert r.entries[0]["cells"] == [["回答者", "佐藤", "鈴木"]] and r.entries[0]["context"] == [["期限", "12/9"]]


@pytest.mark.parametrize(
    ("kw", "said"),
    [
        ({"key": None, "show": ["回答者"]}, "key"),
        ({"show": []}, "空"),
        ({"show": ["質問"]}, "両方"),
        ({"show": ["No."]}, "両方"),
    ],
)
def test_mistakes_are_explained(tmp_path: Path, kw: dict, said: str) -> None:
    with pytest.raises(ValidationError, match=said):
        _target(tmp_path / "q.csv", **kw)


def test_the_form_writes_show_right_after_columns() -> None:
    item = clean_item({"show": ["期限"], "name": "a", "columns": ["質問"], "source": "q.csv", "key": "No."})
    assert list(item) == ["name", "source", "key", "columns", "show"]


def test_too_many_cells_says_to_watch_rows_and_not_to_split() -> None:
    """Two long columns: an AI editing the config once split such cells over two targets to get past this."""
    with pytest.raises(ValidationError) as e:
        Target(name="a", source="q.xlsx", cells=["一覧!C1:C1500", "一覧!E1:E1500"])
    said = str(e.value)
    assert "3000 個" in said and "分けないでください" in said
    assert "key: No." in said and "columns:" in said and "show:" in said
