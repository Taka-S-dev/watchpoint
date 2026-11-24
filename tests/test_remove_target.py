"""Removing a target from the page: only its entry leaves the config, and its history stays unless asked."""

from __future__ import annotations

from pathlib import Path

import pytest

from watchpoint.config import load_config
from watchpoint.editing import remove_target

CONFIG = """\
# 見張りの一覧
every: 1h

targets:
  # 質問表の B 列
  - name: 質問B
    source: q.xlsx
    cells: [B1:B9]

  - name: 質問G
    source: q.xlsx
    rules:
      - field: 状態
        required: true

  - name: 仕様
    source: spec.md

keep: 20
"""


def test_the_first_entry_goes_with_the_comment_right_above_it() -> None:
    out = remove_target(CONFIG, "質問B")
    assert "質問B" not in out and "B 列" not in out
    assert out.startswith("# 見張りの一覧\nevery: 1h\n\ntargets:\n  - name: 質問G\n")


def test_a_middle_entry_leaves_one_blank_line_between_the_others() -> None:
    out = remove_target(CONFIG, "質問G")
    assert "質問G" not in out and "required" not in out
    assert "    cells: [B1:B9]\n\n  - name: 仕様\n" in out


def test_the_last_entry_keeps_the_next_key_apart() -> None:
    out = remove_target(CONFIG, "仕様")
    assert "spec.md" not in out
    assert out.endswith("        required: true\n\nkeep: 20\n")


def test_removing_every_entry_leaves_an_empty_list(tmp_path: Path) -> None:
    out = remove_target(remove_target(remove_target(CONFIG, "質問B"), "質問G"), "仕様")
    assert "targets: []" in out and "keep: 20" in out
    (tmp_path / "t.yaml").write_text(out, encoding="utf-8")
    assert load_config(tmp_path / "t.yaml").targets == []


def test_what_cannot_be_removed_here_is_explained() -> None:
    with pytest.raises(ValueError, match="targets にありません"):
        remove_target(CONFIG, "無い")
    with pytest.raises(ValueError, match="画面からは"):
        remove_target("targets:\n  - {name: x, source: y}\n", "x")


def test_the_page_removes_a_target_and_keeps_its_history(gui) -> None:
    call, _src, app = gui
    call.check({"names": ["仕様"]})
    data_dir = load_config(app.config_path).data_path(app.config_path)
    kept = [p for p in data_dir.iterdir() if p.is_dir()]
    assert kept

    status, out = call("/api/targets/remove", {"name": "仕様"})
    assert status == 200 and out["history_dropped"] is False
    assert "仕様" not in app.config_path.read_text(encoding="utf-8")
    assert "仕様" in app.config_path.with_name("targets.yaml.bak").read_text(encoding="utf-8")
    assert all(p.exists() for p in kept)  # added back under the same name, it carries on
    assert call("/api/overview")[1]["targets"] == []


def test_the_page_drops_the_history_when_asked(gui) -> None:
    call, _src, app = gui
    call.check({"names": ["仕様"]})
    data_dir = load_config(app.config_path).data_path(app.config_path)
    before = {p.name for p in data_dir.iterdir() if p.is_dir()}

    status, out = call("/api/targets/remove", {"name": "仕様", "history": "drop"})
    assert status == 200 and out["history_dropped"] is True
    assert {p.name for p in data_dir.iterdir() if p.is_dir()} == before - {"仕様"}


def test_an_unknown_target_is_not_found(gui) -> None:
    call, _src, app = gui
    before = app.config_path.read_text(encoding="utf-8")
    status, out = call("/api/targets/remove", {"name": "無い"})
    assert status == 404 and "見つかりません" in out["error"]
    assert app.config_path.read_text(encoding="utf-8") == before
