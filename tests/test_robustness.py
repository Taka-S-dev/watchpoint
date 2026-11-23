import threading
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import watchpoint.fetchers.base as base
import watchpoint.fetchers.file as files
from watchpoint.check import Status, check_target
from watchpoint.cli import app
from watchpoint.config import Config, Target
from watchpoint.lock import Busy, data_lock

runner = CliRunner()


def _run(t: Target, data: Path, **cfg):
    return check_target(t, Config(targets=[t], **cfg), data)


def test_csv_with_key_is_compared_by_record(tmp_path: Path) -> None:
    src = tmp_path / "issues.csv"
    t = Target(name="c", source=str(src), key="#", columns=["題名", "ステータス"])
    data = tmp_path / "data"
    src.write_text("#,題名,ステータス,更新日\n101,ログが出ない,新規,11/20\n", encoding="cp932")
    _run(t, data)
    src.write_text(
        "#,題名,ステータス,更新日\n102,画面が固まる,新規,11/22\n101,ログが出ない,解決,11/22\n", encoding="cp932"
    )
    r = _run(t, data)
    assert "変更 [#=101] ステータス: 新規 → 解決" in r.diff
    assert "追加 [#=102] 題名=画面が固まる | ステータス=新規" in r.diff


def test_text_diff_names_the_heading_it_belongs_to(tmp_path: Path) -> None:
    src = tmp_path / "spec.md"
    t = Target(name="s", source=str(src))
    data = tmp_path / "data"
    body = ["# 仕様", "", "## ログ", "保存先は D:", "保持は 30 日", "", "## 画面", "色は青", "文字は黒"]
    src.write_text("\n".join(body) + "\n", encoding="utf-8")
    _run(t, data)
    body[8] = "文字は白"
    src.write_text("\n".join(body) + "\n", encoding="utf-8")
    r = _run(t, data)
    hunk = next(line for line in r.diff.splitlines() if line.startswith("@@"))
    assert hunk.endswith("画面")


def test_a_file_being_saved_is_fetched_again(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "a.txt"
    src.write_text("v1\n", encoding="utf-8")
    monkeypatch.setattr(base, "RETRY_WAIT_SECONDS", 0)
    real = files.copy_source
    calls = {"n": 0}

    def flaky(source, dest):
        calls["n"] += 1
        if calls["n"] == 1:
            raise base.FileBusy("保存中")
        return real(source, dest)

    monkeypatch.setattr(files, "copy_source", flaky)
    assert _run(Target(name="a", source=str(src)), tmp_path / "data").status is Status.FIRST
    assert calls["n"] == 2


def test_a_broken_excel_reports_a_readable_reason(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(base, "RETRY_WAIT_SECONDS", 0)
    src = tmp_path / "q.xlsx"
    src.write_bytes(b"not a zip")
    r = _run(Target(name="q", source=str(src)), tmp_path / "data")
    assert r.status is Status.FAILED and "保存中か、壊れている" in r.message


def test_environment_variables_in_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("WP_TEST_ROOT", str(tmp_path))
    assert Target(
        name="x", source="%WP_TEST_ROOT%\\a.txt" if __import__("os").name == "nt" else "$WP_TEST_ROOT/a.txt"
    ).source.startswith(str(tmp_path))


def test_keep_originals_false_keeps_only_text(tmp_path: Path) -> None:
    src = tmp_path / "a.md"
    src.write_text("x\n", encoding="utf-8")
    r = _run(Target(name="a", source=str(src)), tmp_path / "data", keep_originals=False)
    assert (r.snapshot / "text.txt").exists()
    assert not list(r.snapshot.glob("original*"))


def test_lock_lets_one_run_at_a_time(tmp_path: Path) -> None:
    held = threading.Event()
    release = threading.Event()

    def holder():
        with data_lock(tmp_path):
            held.set()
            release.wait(5)

    th = threading.Thread(target=holder)
    th.start()
    held.wait(5)
    try:
        with pytest.raises(Busy), data_lock(tmp_path, wait=0.6):
            pass
    finally:
        release.set()
        th.join()
    with data_lock(tmp_path, wait=1):
        pass


def _write_config(tmp_path: Path, targets: list[dict]) -> Path:
    cfg = tmp_path / "targets.yaml"
    cfg.write_text(yaml.safe_dump({"targets": targets}, allow_unicode=True), encoding="utf-8")
    return cfg


def test_cli_changed_only_exit_code_list_and_clean(tmp_path: Path) -> None:
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("1\n", encoding="utf-8")
    b.write_text("1\n", encoding="utf-8")
    cfg = _write_config(tmp_path, [{"name": "A", "source": str(a)}, {"name": "B", "source": str(b)}])
    c = ["--config", str(cfg)]

    assert runner.invoke(app, ["check", *c, "--exit-code"]).exit_code == 0
    a.write_text("2\n", encoding="utf-8")
    res = runner.invoke(app, ["check", *c, "-f", "md", "--changed-only", "--exit-code"])
    assert res.exit_code == 1
    assert "## A" in res.output and "## B" not in res.output and "全 2 件" in res.output

    res = runner.invoke(app, ["list", *c])
    assert res.exit_code == 0 and "A" in res.output and "テキスト" in res.output

    _write_config(tmp_path, [{"name": "A", "source": str(a)}])
    res = runner.invoke(app, ["clean", *c])
    assert "B" in res.output and (tmp_path / "data" / "B").exists()
    runner.invoke(app, ["clean", *c, "--yes"])
    assert not (tmp_path / "data" / "B").exists() and (tmp_path / "data" / "A").exists()
