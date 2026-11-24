"""Advice: settings that work but fit their use badly are said where the config is checked, never refused."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from watchpoint.advice import config_advice, target_advice
from watchpoint.cli import app
from watchpoint.config import Config, Target, load_config
from watchpoint.report import as_markdown


def test_a_column_of_a_table_watched_by_its_cells_gets_the_rows_example() -> None:
    (said,) = target_advice(Target(name="a", source="q.xlsx", cells=["一覧!C2:C700"]))
    assert "699 行" in said and "挿入" in said and "key: No." in said and "show:" in said


def test_a_few_fixed_cells_get_no_advice() -> None:
    assert target_advice(Target(name="a", source="q.xlsx", cells={"合計": "集計!F20", "承認": "B3:D5"})) == []
    assert target_advice(Target(name="a", source="q.xlsx", key="No.")) == []


def test_targets_split_over_one_file_are_told_they_can_be_one() -> None:
    a = Target(name="C列", source="q.xlsx", cells=["C2:C700"])
    b = Target(name="E列", source="q.xlsx", cells=["E2:E700"])
    advice = config_advice(Config(targets=[a, b]))
    assert [x.split("」")[0] for x in advice] == ["「C列", "「E列"]
    assert all("ほかにもあります（2 件）" in x for x in advice)


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "targets.yaml"
    cfg.write_text(
        "targets:\n  - name: 回答欄\n    source: q.xlsx\n    cells: [D1:D300]\n    enabled: false\n", encoding="utf-8"
    )
    return cfg


def test_doctor_says_it(tmp_path: Path) -> None:
    r = CliRunner().invoke(app, ["doctor", "-c", str(_config(tmp_path))])
    assert "助言" in r.output and "回答欄" in r.output and "key: No." in r.output


def test_the_markdown_for_an_agent_carries_it_and_the_chat_copy_does_not(tmp_path: Path) -> None:
    advice = config_advice(load_config(_config(tmp_path)))
    md = as_markdown([], total=1, advice=advice)
    assert "## 設定の助言" in md and "\n- 「回答欄」" in md and "\n    key: No." in md  # the example stays in its item
    assert "設定の助言" not in as_markdown([], total=1, chat=True, advice=advice)


def test_the_page_gets_it_when_validating_and_saving(gui) -> None:
    call, _src, app_ = gui
    text = "targets:\n  - name: 回答欄\n    source: q.xlsx\n    cells: [D1:D300]\n    enabled: false\n"
    status, r = call("/api/config/validate", {"text": text})
    assert status == 200 and r["ok"] and r["advice"] and "回答欄" in r["advice"][0]
    _, cur = call("/api/config")
    status, saved = call("/api/config/save", {"text": text, "hash": cur["hash"]})
    assert status == 200 and saved["advice"] == r["advice"]
