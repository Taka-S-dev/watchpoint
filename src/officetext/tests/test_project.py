"""officetext.project reads a Project plan's tasks, and has Project convert only a copy of a .mpp."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from officetext import project
from officetext.project import ProjectUnavailable, read_project

PLAN = """<?xml version="1.0" encoding="UTF-8"?>
<Project xmlns="http://schemas.microsoft.com/project">
 <Title>次期システム開発</Title><StatusDate>2026-11-20T17:00:00</StatusDate><MinutesPerDay>480</MinutesPerDay>
 <Tasks>
  <Task><UID>0</UID><ID>0</ID><Name>次期システム開発</Name><OutlineLevel>0</OutlineLevel><PercentComplete>35</PercentComplete></Task>
  <Task><UID>1</UID><ID>1</ID><Name>設計</Name><OutlineNumber>1</OutlineNumber><OutlineLevel>1</OutlineLevel>
   <Summary>1</Summary><Start>2026-11-02T08:00:00</Start><Finish>2026-11-30T17:00:00</Finish></Task>
  <Task><UID>7</UID><ID>2</ID><Name>画面設計</Name><OutlineNumber>1.1</OutlineNumber><OutlineLevel>2</OutlineLevel>
   <Start>2026-11-02T08:00:00</Start><Finish>2026-11-13T17:00:00</Finish><Duration>PT80H0M0S</Duration>
   <PercentComplete>100</PercentComplete>
   <Baseline><Number>0</Number><Start>2026-11-02T08:00:00</Start><Finish>2026-11-11T17:00:00</Finish></Baseline></Task>
  <Task><UID>9</UID><ID>3</ID><Name>DB 設計</Name><OutlineNumber>1.2</OutlineNumber><OutlineLevel>2</OutlineLevel>
   <Start>2026-11-16T08:00:00</Start><Finish>2026-11-30T17:00:00</Finish><Duration>PT88H0M0S</Duration>
   <PercentComplete>10</PercentComplete><Notes>レビュー待ち</Notes>
   <PredecessorLink><PredecessorUID>7</PredecessorUID><Type>1</Type><LinkLag>4800</LinkLag></PredecessorLink></Task>
  <Task><UID>12</UID><ID>4</ID><Name>設計完了</Name><OutlineNumber>2</OutlineNumber><OutlineLevel>1</OutlineLevel>
   <Milestone>1</Milestone><Start>2026-11-30T17:00:00</Start><Finish>2026-11-30T17:00:00</Finish></Task>
 </Tasks>
 <Resources><Resource><UID>1</UID><Name>佐藤</Name></Resource></Resources>
 <Assignments><Assignment><TaskUID>9</TaskUID><ResourceUID>1</ResourceUID><Units>0.5</Units></Assignment></Assignments>
</Project>
"""


def test_tasks_from_project_xml(tmp_path: Path) -> None:
    path = tmp_path / "計画.xml"
    path.write_text(PLAN, encoding="utf-8")
    plan = read_project(path)
    assert (plan.name, plan.status_date, plan.percent, plan.converted_by) == (
        "次期システム開発", "2026-11-20T17:00:00", 35, ""
    )  # fmt: skip
    by_uid = {t.uid: t for t in plan.tasks}
    assert list(by_uid) == ["1", "7", "9", "12"]  # the plan's own summary task is the Plan
    db = by_uid["9"]
    assert (db.id, db.outline, db.level, db.path) == ("3", "1.2", 2, ("設計",))
    assert db.predecessor_names == "画面設計FS+1d"
    assert db.predecessors == "2FS+1d" and db.resources == ("佐藤 50%",) and db.notes == "レビュー待ち"
    assert by_uid["7"].baseline_finish == "2026-11-11T17:00:00" and by_uid["7"].duration_days == "10"
    assert by_uid["12"].milestone and by_uid["1"].summary


def test_a_mpp_needs_project_and_says_how_to_do_without(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(project, "project_available", lambda: False)
    (tmp_path / "計画.mpp").write_bytes(b"\xd0\xcf\x11\xe0 not really a plan")
    with pytest.raises(ProjectUnavailable, match="XML 形式で保存"):
        read_project(tmp_path / "計画.mpp")


def test_project_opens_a_copy_and_the_temporary_folder_is_removed(tmp_path: Path, monkeypatch) -> None:
    """What Project is asked to open is a copy in a temporary folder, never the plan given."""
    master = tmp_path / "share" / "計画.mpp"
    master.parent.mkdir()
    master.write_bytes(b"\xd0\xcf\x11\xe0 the master")
    before = (master.read_bytes(), master.stat().st_mtime_ns)
    seen = {}

    def fake_powershell(args, **kw):
        env = kw["env"]
        seen.update(src=Path(env["OFFICETEXT_SRC"]), out=Path(env["OFFICETEXT_OUT"]))
        assert seen["src"].read_bytes() == before[0]  # a copy of it
        seen["out"].write_text(PLAN, encoding="utf-8")  # as Project's Save As > XML would

        class Done:
            stdout, stderr = "OK", ""

        return Done()

    monkeypatch.setattr(project, "project_available", lambda: True)
    monkeypatch.setattr(project.subprocess, "run", fake_powershell)
    plan = read_project(master)
    assert plan.converted_by == "Microsoft Project" and len(plan.tasks) == 4
    assert seen["src"].resolve() != master.resolve() and seen["src"].parent.name.startswith("officetext-mpp-")
    assert not seen["src"].parent.exists()  # the temporary folder is gone
    assert (master.read_bytes(), master.stat().st_mtime_ns) == before


def test_a_project_that_does_not_answer_is_reported(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "計画.mpp").write_bytes(b"x")

    class Timeout:
        stdout, stderr = "TIMEOUT", ""

    monkeypatch.setattr(project, "project_available", lambda: True)
    monkeypatch.setattr(project.subprocess, "run", lambda *a, **k: Timeout())
    with pytest.raises(RuntimeError, match="止めました"):
        read_project(tmp_path / "計画.mpp", timeout=5)


def test_xml_that_is_not_a_plan_is_refused(tmp_path: Path) -> None:
    (tmp_path / "a.xml").write_text("<root/>", encoding="utf-8")
    with pytest.raises(ValueError, match="Project の XML ではありません"):
        read_project(tmp_path / "a.xml")


_MAKE_MPP = r"""
$app = New-Object -ComObject MSProject.Application
try {
  $app.Visible = $false
  $app.AutomationSecurity = 3
  $bf = [Reflection.BindingFlags]::InvokeMethod
  [void]$app.GetType().InvokeMember('FileOpenEx', $bf, $null, $app, @($env:PLAN_XML), $null, $null, @('Name'))
  [void]$app.GetType().InvokeMember('FileSaveAs', $bf, $null, $app, @($env:PLAN_MPP, 'MSProject.MPP'), $null, $null, @('Name', 'FormatID'))
  [void]$app.GetType().InvokeMember('FileCloseEx', $bf, $null, $app, @(0), $null, $null, @('Save'))
} finally { try { $app.Quit(0) } catch { } }
"""


@pytest.mark.skipif(os.name != "nt" or not project.project_available(), reason="needs Microsoft Project")
def test_a_real_mpp_through_project(tmp_path: Path) -> None:  # pragma: no cover - runs where Project is installed
    """Project makes a .mpp from the sample plan, then read_project has Project convert it back."""
    import subprocess

    xml, mpp = tmp_path / "plan.xml", tmp_path / "plan.mpp"
    xml.write_text(PLAN, encoding="utf-8")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _MAKE_MPP], check=False, timeout=300,
                   env={**os.environ, "PLAN_XML": str(xml), "PLAN_MPP": str(mpp)})  # fmt: skip
    assert mpp.exists(), "Project did not save the sample as .mpp"
    plan = read_project(mpp)
    assert plan.converted_by == "Microsoft Project"
    assert [t.name for t in plan.tasks] == ["設計", "画面設計", "DB 設計", "設計完了"]
