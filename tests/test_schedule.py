import datetime as dt
import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import yaml
from typer.testing import CliRunner

from watchpoint import schedule
from watchpoint.cli import app
from watchpoint.lock import data_lock

NS = {"t": schedule.TASK_NS}


@pytest.fixture(autouse=True)
def _own_memory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))  # not the real profile


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    src = tmp_path / "memo.txt"
    src.write_text("one\n", encoding="utf-8")
    drop = tmp_path / "drop"
    cfg = tmp_path / "targets.yaml"
    cfg.write_text(
        yaml.safe_dump({"notify": {"drop_folder": str(drop)}, "targets": [{"name": "メモ", "source": str(src)}]}),
        encoding="utf-8",
    )
    return cfg, src, drop


def _drops(drop: Path) -> list[str]:
    return (
        [json.loads(f.read_text(encoding="utf-8"))["text"] for f in sorted(drop.glob("*.json"))]
        if drop.exists()
        else []
    )


def test_task_runs_windowless_per_user_and_never_overlaps(tmp_path: Path) -> None:
    folder = tmp_path / "with space"
    xml = schedule.task_xml(7, r"C:\py\pythonw.exe", ["-m", "watchpoint", "--config", str(folder / "t.yaml")], folder)
    root = ET.fromstring(xml)
    assert root.find(".//t:Repetition/t:Interval", NS).text == "PT7M"
    assert root.find(".//t:LogonType", NS).text == "InteractiveToken"  # no stored password, no admin
    assert root.find(".//t:MultipleInstancesPolicy", NS).text == "IgnoreNew"
    assert root.find(".//t:StartWhenAvailable", NS).text == "true"
    assert root.find(".//t:DisallowStartIfOnBatteries", NS).text == "false"
    assert f'"{folder / "t.yaml"}"' in root.find(".//t:Arguments", NS).text


def _later(monkeypatch, minutes: int) -> None:
    """Move the checker's clock forward, past the default every (30m)."""
    import watchpoint.check as check

    base = dt.datetime.now().replace(microsecond=0)
    monkeypatch.setattr(check, "_now", lambda: base + dt.timedelta(minutes=minutes))


def test_run_checks_due_targets_and_notifies_each_change_once(tmp_path: Path, monkeypatch) -> None:
    cfg, src, drop = _setup(tmp_path)
    assert schedule.run(cfg, 5) == 0
    beat = schedule.read_heartbeat(tmp_path / "data")
    assert beat["result"] == "ok" and beat["checked"] == 1 and beat["changed"] == 0

    src.write_text("one\ntwo\n", encoding="utf-8")
    schedule.run(cfg, 5)
    assert _drops(drop) == []  # the target has no every: the default 30m has not passed yet
    _later(monkeypatch, 31)
    schedule.run(cfg, 5)
    _later(monkeypatch, 62)
    schedule.run(cfg, 5)  # still unacknowledged: no second notice
    assert len(_drops(drop)) == 1 and _drops(drop)[0].startswith("メモ:")
    assert schedule.read_heartbeat(tmp_path / "data")["changed"] == 1
    assert schedule.heartbeat_view(tmp_path / "data")["state"] == "ok"


def test_broken_config_is_reported_once_and_recovery_too(tmp_path: Path) -> None:
    cfg, _, drop = _setup(tmp_path)
    schedule.run(cfg, 5)
    good = cfg.read_text(encoding="utf-8")
    cfg.write_text(good + "  - name: 壊れた\n    sourc: x\n", encoding="utf-8")
    assert schedule.run(cfg, 5) == 2
    assert schedule.run(cfg, 5) == 2
    view = schedule.heartbeat_view(tmp_path / "data")
    assert view["state"] == "error"
    cfg.write_text(good, encoding="utf-8")
    assert schedule.run(cfg, 5) == 0
    texts = _drops(drop)
    assert len(texts) == 2  # the drop folder is remembered from the last good run
    assert texts[0].startswith("watchpoint の自動チェックが止まっています") and "戻りました" in texts[1]


def test_run_steps_aside_while_the_gui_holds_the_data(tmp_path: Path, monkeypatch) -> None:
    cfg, _, _ = _setup(tmp_path)
    monkeypatch.setattr(schedule, "LOCK_WAIT_SECONDS", 0.2)
    with data_lock(tmp_path / "data"):
        assert schedule.run(cfg, 5) == 0
    assert schedule.read_heartbeat(tmp_path / "data")["skipped"] == "busy"


def test_output_goes_to_a_file_when_there_is_no_console(tmp_path: Path, monkeypatch) -> None:
    cfg, _, _ = _setup(tmp_path)
    cfg.write_text("targets: [", encoding="utf-8")  # broken: makes the run write something
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    schedule.run(cfg, 5)
    print("from the run")
    sys.stdout.close()
    assert "from the run" in (tmp_path / "data" / schedule.RUN_LOG).read_text(encoding="utf-8")


def test_stopped_runs_are_called_out_in_status_and_the_gui(tmp_path: Path, gui) -> None:
    cfg, _, _ = _setup(tmp_path)
    data = tmp_path / "data"
    schedule.run(cfg, 5)
    beat = schedule.read_heartbeat(data)
    beat["finished"] = (dt.datetime.now() - dt.timedelta(hours=3)).isoformat()
    (data / schedule.HEARTBEAT).write_text(json.dumps(beat), encoding="utf-8")
    assert schedule.heartbeat_view(data)["state"] == "late"
    out = CliRunner().invoke(app, ["status", "-c", str(cfg)]).output
    assert "自動チェックが3時間動いていません" in "".join(out.split())  # rich wraps at spaces

    call, _, _ = gui
    assert call("/api/overview")[1]["schedule"]["state"] == "late"  # the fixture shares this data folder


def test_notice_shows_what_changed_not_the_context_around_it(tmp_path: Path) -> None:
    from watchpoint.check import notice_lines

    diff = "--- a\n+++ b\n@@ -1 +1,2 @@\n one\n+two\n" + "".join(f"+x{i}\n" for i in range(5))
    assert notice_lines(diff) == ["+two", "+x0", "+x1", "ほか 3 行"]
    assert notice_lines("## Sheet1\n変更 [12] 状態: 未回答 → 回答済み\n") == ["変更 [12] 状態: 未回答 → 回答済み"]


@pytest.fixture
def sched(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    src = tmp_path / "memo.txt"
    src.write_text("one\n", encoding="utf-8")
    drop = tmp_path / "drop"
    cfg = tmp_path / "targets.yaml"
    good = yaml.safe_dump(
        {
            "data_dir": str(tmp_path / "elsewhere"),
            "notify": {"drop_folder": str(drop)},
            "targets": [{"name": "m", "source": str(src)}],
        }
    )
    cfg.write_text(good, encoding="utf-8")
    return cfg, good, drop, tmp_path / "elsewhere"


def _notices(drop: Path) -> list[str]:
    return (
        [json.loads(f.read_text(encoding="utf-8"))["text"] for f in sorted(drop.glob("*.json"))]
        if drop.exists()
        else []
    )


def test_a_damaged_heartbeat_does_not_stop_the_watcher(sched) -> None:
    cfg, _, _, data = sched
    data.mkdir(parents=True)
    for bad in (
        '"x"',
        "[1]",
        '{"finished": 123}',
        '{"finished": "yesterday"}',
        '{"finished": "2026-11-23T08:00:00+09:00"}',
    ):
        (data / schedule.HEARTBEAT).write_text(bad, encoding="utf-8")
        schedule.heartbeat_view(data)  # must not raise
        assert schedule.run(cfg, 5) == 0
        assert schedule.read_heartbeat(data)["result"] == "ok"


def test_failure_is_told_once_even_if_its_text_changes_and_a_busy_run_does_not_hide_it(sched, monkeypatch) -> None:
    cfg, good, drop, data = sched
    schedule.run(cfg, 5)
    from watchpoint import schedule as s

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError(f"share gone at {calls['n']}")  # different text every run

    monkeypatch.setattr(s, "check_all", boom)
    for _ in range(3):
        schedule.run(cfg, 5)
    monkeypatch.setattr(s, "LOCK_WAIT_SECONDS", 0.1)
    with data_lock(data):
        schedule.run(cfg, 5)
    assert schedule.heartbeat_view(data)["state"] == "error"  # still failing, though skipped
    monkeypatch.undo()
    monkeypatch.setenv("LOCALAPPDATA", str(cfg.parent / "localappdata"))
    schedule.run(cfg, 5)
    texts = _notices(drop)
    assert len(texts) == 2 and "止まっています" in texts[0] and "戻りました" in texts[1]


def test_a_broken_config_is_told_where_the_data_really_is(sched) -> None:
    cfg, good, drop, data = sched
    schedule.run(cfg, 5)
    cfg.write_text(good + "  - name: 壊れた\n    sourc: x\n", encoding="utf-8")
    assert schedule.run(cfg, 5) == 2
    assert schedule.read_heartbeat(data)["result"] == "error"  # the custom data_dir, not <config>/data
    assert not (cfg.parent / "data").exists()
    assert any("止まっています" in t for t in _notices(drop))


def test_a_run_that_never_finished_is_reported(sched) -> None:
    cfg, _, drop, data = sched
    schedule.run(cfg, 5)
    beat = schedule.read_heartbeat(data)
    beat["running_since"] = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat()
    (data / schedule.HEARTBEAT).write_text(json.dumps(beat), encoding="utf-8")
    schedule.run(cfg, 5)
    assert any("終わりませんでした" in t for t in _notices(drop))
