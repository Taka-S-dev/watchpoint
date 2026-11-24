"""group: targets of one project (several pages) under one heading, picked and checked together."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from watchpoint.cli import app
from watchpoint.config import Target, load_config
from watchpoint.editing import clean_item

CONFIG = """\
targets:
  - name: 設計（概要）
    group: 〇〇案件
    source: '{a}'
  - name: 設計（画面）
    group: 〇〇案件
    source: '{b}'
  - name: 週報
    tags: [毎週]
    source: '{c}'
"""


def _config(tmp_path: Path) -> Path:
    files = {}
    for k in "abc":
        f = tmp_path / f"{k}.txt"
        f.write_text(k, encoding="utf-8")
        files[k] = f
    cfg = tmp_path / "targets.yaml"
    cfg.write_text(CONFIG.format(**files), encoding="utf-8")
    return cfg


def test_a_group_is_one_name_and_one_level() -> None:
    assert Target(name="a", source="x.txt", group=" 〇〇案件 ").group == "〇〇案件"
    assert Target(name="a", source="x.txt", group="  ").group is None
    with pytest.raises(ValidationError, match="1 段"):
        Target(name="a", source="x.txt", group="〇〇案件/設計")


def test_the_form_writes_the_group_right_after_the_name() -> None:
    item = clean_item({"source": "x.txt", "group": "〇〇案件", "name": "a", "tags": ["毎週"]})
    assert list(item) == ["name", "group", "source", "tags"]


def test_tag_on_the_command_line_picks_a_group_too(tmp_path: Path) -> None:
    """Asking for 〇〇案件 works the same whether it is a tag or a group."""
    r = CliRunner().invoke(app, ["list", "-c", str(_config(tmp_path)), "-t", "〇〇案件"])
    assert r.exit_code == 0
    assert "設計（概要）" in r.output and "設計（画面）" in r.output and "週報" not in r.output
    r = CliRunner().invoke(app, ["list", "-c", str(_config(tmp_path)), "-t", "無い"])
    assert r.exit_code == 2 and "〇〇案件" in r.output and "毎週" in r.output  # what there is


def test_the_page_lists_groups_and_moves_a_target(tmp_path: Path, gui) -> None:
    call, src, app_ = gui
    status, ov = call("/api/overview")
    assert [t["group"] for t in ov["targets"]] == [None]

    status, out = call("/api/targets/group", {"name": "仕様", "group": "〇〇案件"})
    assert status == 200
    assert load_config(app_.config_path).targets[0].group == "〇〇案件"
    assert call("/api/target?name=" + "%E4%BB%95%E6%A7%98")[1]["group"] == "〇〇案件"

    status, out = call("/api/targets/group", {"name": "仕様", "group": ""})  # out of the group again
    assert status == 200 and load_config(app_.config_path).targets[0].group is None
    assert "group:" not in app_.config_path.read_text(encoding="utf-8")  # the line goes, not an empty value


def test_several_targets_move_with_one_write_or_none_at_all(gui) -> None:
    """Dropping the picked targets on a group: one save (one .bak); one that cannot move stops them all."""
    call, src, app_ = gui
    text = app_.config_path.read_text(encoding="utf-8")
    text += f"  - name: 仕様2\n    source: '{src}'\n"
    app_.config_path.write_text(text, encoding="utf-8")

    status, out = call("/api/targets/group", {"names": ["仕様", "仕様2"], "group": "〇〇案件"})
    assert status == 200 and out["group"] == "〇〇案件"
    assert [t.group for t in load_config(app_.config_path).targets] == ["〇〇案件", "〇〇案件"]
    assert "group:" not in app_.config_path.with_name("targets.yaml.bak").read_text(encoding="utf-8")

    before = app_.config_path.read_text(encoding="utf-8") + f"  - {{name: 一行, source: '{src}'}}\n"
    app_.config_path.write_text(before, encoding="utf-8")
    status, out = call("/api/targets/group", {"names": ["仕様", "一行"], "group": None})
    assert status == 422 and "一行" in out["error"]
    assert app_.config_path.read_text(encoding="utf-8") == before  # 仕様 was not moved alone
