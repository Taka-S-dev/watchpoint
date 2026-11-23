"""Things that break in real use: hangs, races, collisions, encodings, junk files, damaged state."""

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import watchpoint.fetchers.net as net
import watchpoint.fetchers.office as office
from watchpoint.check import Status, check_all, check_target
from watchpoint.cli import app
from watchpoint.config import Config, ConfigError, Target, load_config

runner = CliRunner()


def _run(t: Target, data: Path, **cfg):
    return check_target(t, Config(targets=[t], **cfg), data)


def test_parallel_checks_do_not_corrupt_the_log(tmp_path: Path) -> None:
    targets = []
    for i in range(24):
        p = tmp_path / f"f{i}.md"
        p.write_text(f"{i}\n", encoding="utf-8")
        targets.append(Target(name=f"t{i}", source=str(p)))
    data = tmp_path / "data"
    check_all(targets, Config(targets=targets, workers=8), data)
    lines = (data / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 24
    assert sorted(json.loads(line)["name"] for line in lines) == sorted(t.name for t in targets)


def test_names_that_share_a_data_folder_are_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "t.yaml"
    for a, b in (("Spec", "spec"), ("a/b", "a_b")):
        cfg.write_text(
            yaml.safe_dump({"targets": [{"name": a, "source": "x"}, {"name": b, "source": "y"}]}), encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="同じデータのフォルダ"):
            load_config(cfg)
    cfg.write_text(
        yaml.safe_dump({"targets": [{"name": "Spec", "source": "x"}, {"name": "spec", "id": "spec-2", "source": "y"}]}),
        encoding="utf-8",
    )
    assert len(load_config(cfg).targets) == 2


def test_shift_jis_page_without_charset_header(tmp_path: Path, server) -> None:
    html = '<html><head><meta charset="Shift_JIS"></head><body><p>お知らせ：保守は12月</p></body></html>'
    server.routes["/sjis"] = (200, "text/html", html.encode("cp932"))
    r = _run(Target(name="s", source=server.url + "/sjis"), tmp_path / "data")
    assert (r.snapshot / "text.txt").read_text(encoding="utf-8").strip() == "お知らせ：保守は12月"


def test_transient_server_errors_are_retried_once(tmp_path: Path, server, monkeypatch) -> None:
    monkeypatch.setattr(net, "HTTP_RETRY_SECONDS", 0)
    calls = {"n": 0}

    def flaky(q, h):
        calls["n"] += 1
        return (503, "text/html", "busy") if calls["n"] == 1 else (200, "text/html; charset=utf-8", "<p>ok</p>")

    server.routes["/flaky"] = flaky
    r = _run(Target(name="f", source=server.url + "/flaky"), tmp_path / "data")
    assert r.status is Status.FIRST and calls["n"] == 2

    server.routes["/down"] = (503, "text/html", "busy")
    r = _run(Target(name="d", source=server.url + "/down"), tmp_path / "data")
    assert r.status is Status.FAILED and "503" in r.message


def test_folder_listing_ignores_lock_and_cache_files(tmp_path: Path) -> None:
    d = tmp_path / "share"
    d.mkdir()
    (d / "a.docx").write_bytes(b"1")
    t = Target(name="f", source=str(d))
    data = tmp_path / "data"
    _run(t, data)
    for junk in ("Thumbs.db", "desktop.ini", "~$a.docx", ".~lock.a.docx#", "b.docx.tmp", "c.crdownload"):
        (d / junk).write_bytes(b"x")
    assert _run(t, data).status is Status.UNCHANGED


def test_a_damaged_state_file_is_set_aside(tmp_path: Path) -> None:
    src = tmp_path / "a.md"
    src.write_text("1\n", encoding="utf-8")
    t = Target(name="a", source=str(src))
    data = tmp_path / "data"
    _run(t, data)
    (data / "a" / "state.json").write_text("{ broken", encoding="utf-8")
    r = _run(t, data)
    assert r.status is Status.FIRST
    assert list((data / "a").glob("state.broken-*.json"))


def test_data_dir_can_live_elsewhere(tmp_path: Path) -> None:
    src = tmp_path / "a.md"
    src.write_text("1\n", encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    local = tmp_path / "local-data"
    cfg = shared / "targets.yaml"
    cfg.write_text(
        yaml.safe_dump({"data_dir": str(local), "targets": [{"name": "A", "source": str(src)}]}), encoding="utf-8"
    )
    assert runner.invoke(app, ["check", "--config", str(cfg)]).exit_code == 0
    assert (local / "A" / "state.json").exists() and not (shared / "data").exists()


def _excel_available() -> bool:
    try:
        import winreg

        import win32com.client  # noqa: F401

        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Excel.Application")
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _excel_available(), reason="Excel is not installed")
def test_office_fetch_that_hangs_is_ended_by_the_watchdog(tmp_path: Path, monkeypatch) -> None:
    import datetime as dt

    from openpyxl import Workbook

    src = tmp_path / "q.xlsx"
    wb = Workbook()
    wb.active.append(["x"])
    wb.save(src)

    def hang(app, source, dest):
        # Stand-in for a hidden sign-in prompt: Excel itself blocks for a long time.
        app.Wait(app.Evaluate("NOW()+TIME(0,0,60)"))

    monkeypatch.setattr(office, "_excel_copy", hang)
    before = office._office_pids("EXCEL.EXE")
    started = dt.datetime.now()
    r = _run(Target(name="o", source=str(src), fetch="office", timeout=3), tmp_path / "data")
    assert (dt.datetime.now() - started).total_seconds() < 30
    assert r.status is Status.FAILED and "秒たっても開けませんでした" in r.message, r.message
    # Only the Excel this call started was ended; nothing it started is left running.
    import time

    for _ in range(20):
        if not (office._office_pids("EXCEL.EXE") - before):
            break
        time.sleep(0.5)
    assert not (office._office_pids("EXCEL.EXE") - before)
