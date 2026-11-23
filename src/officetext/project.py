"""Microsoft Project plans (.mpp, .mpt, or Project XML) as a Plan: its tasks, matched by UID.

An optional part of officetext. Nothing else in the package imports this module, so it can be left
out; it is the only part that needs something outside Python.

.mpp is an undocumented binary format, so a plan is first converted to Project's own XML (MSPDI),
which Microsoft Project writes itself when it is installed (driven through PowerShell, so no extra
Python package is needed). Project XML (Project's Save As > XML) is read directly, anywhere.

Guards when Project converts a plan:

- the plan Project opens is a copy in a temporary folder, never the file given
- macros are switched off for the session, and the plan is not opened if they cannot be
- it is opened read-only, saved as XML into the temporary folder, and closed without saving
- a hidden Project that does not answer in time is stopped; only one started by this call
  (a Project window the person has open is left alone)

Tasks are identified by UID, which survives inserting and reordering rows (ID is the row number
and shifts when a row is inserted above it).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

NS = "{http://schemas.microsoft.com/project}"
LINK_TYPES = {"0": "FF", "1": "FS", "2": "SF", "3": "SS"}
CONVERT_LIMIT_S = 240  # Project starting cold on a large plan can take a minute or two
MAX_TASKS = 5000
MAX_XML_BYTES = 256 * 1024 * 1024


class ProjectUnavailable(RuntimeError):
    """A .mpp was given but Microsoft Project, which converts it, is not available here."""


@dataclass(frozen=True)
class Task:
    uid: str
    id: str  # the row number shown in Project; changes when rows are inserted above it
    name: str
    outline: str = ""  # "1.2.3"
    level: int = 1
    path: tuple[str, ...] = ()  # the names of the summary tasks above it, outermost first
    start: str = ""  # ISO 8601
    finish: str = ""
    duration_days: str = ""
    percent: int = 0
    milestone: bool = False
    summary: bool = False
    active: bool = True
    critical: bool = False
    baseline_start: str = ""
    baseline_finish: str = ""
    deadline: str = ""
    predecessors: str = ""  # as Project shows them: "3, 5SS+2d" (IDs of the predecessors)
    predecessor_names: str = ""  # the same by name ("画面設計, 結合試験SS+2d"): IDs shift when rows are inserted
    resources: tuple[str, ...] = ()
    notes: str = ""
    custom: dict[str, str] = field(default_factory=dict)  # custom fields by their alias


@dataclass(frozen=True)
class Plan:
    name: str
    tasks: tuple[Task, ...]
    status_date: str = ""  # 状況報告日: "late" is judged at this date
    percent: int = 0  # the whole plan's % complete, as Project computes it
    converted_by: str = ""  # "Microsoft Project", or "" when Project XML was read directly
    warnings: tuple[str, ...] = ()


def read_project(path: Path | str, timeout: float = CONVERT_LIMIT_S) -> Plan:
    """Read a plan. A .mpp / .mpt needs Microsoft Project (ProjectUnavailable otherwise); Project XML
    is read directly."""
    path = Path(path)
    if path.suffix.lower() == ".xml" or is_project_xml(path):
        return parse(_read_limited(path))
    data = _convert(path, timeout)
    return parse(data, converted_by="Microsoft Project")


def is_project_xml(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return b"schemas.microsoft.com/project" in head and b"<Project" in head


def project_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"MSProject.Application"))
        return True
    except OSError:
        return False


def _read_limited(path: Path) -> bytes:
    if path.stat().st_size > MAX_XML_BYTES:
        raise ValueError(f"大きすぎるので読みません（{MAX_XML_BYTES // 1024 // 1024} MB まで）: {path.name}")
    return path.read_bytes()


# --------------------------------------------------------------------------- converting with Project

_PROJECT_PS = r"""
$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject MSProject.Application
try {
  $app.Visible = $false
  try { $app.DisplayAlerts = $false } catch { }
  # macros in the plan must never run: refuse to open it unless they are off for this session
  $app.AutomationSecurity = 3
  if ($app.AutomationSecurity -ne 3) { throw 'could not switch off macros in Project; the plan was not opened' }
  # named arguments through IDispatch: PowerShell cannot skip positional COM arguments
  $bf = [Reflection.BindingFlags]::InvokeMethod
  [void]$app.GetType().InvokeMember('FileOpenEx', $bf, $null, $app, @($env:OFFICETEXT_SRC, $true), $null, $null, @('Name', 'ReadOnly'))
  [void]$app.GetType().InvokeMember('FileSaveAs', $bf, $null, $app, @($env:OFFICETEXT_OUT, 'MSProject.XML'), $null, $null, @('Name', 'FormatID'))
  [void]$app.GetType().InvokeMember('FileCloseEx', $bf, $null, $app, @(0), $null, $null, @('Save'))
  Write-Output 'OK'
} finally {
  try { $app.Quit(0) } catch { }
  [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($app)
  [GC]::Collect(); [GC]::WaitForPendingFinalizers()
}
"""

_GUARD_PS = """
$t0 = Get-Date
$job = Start-Job -ScriptBlock ([scriptblock]::Create($env:OFFICETEXT_SCRIPT))
if (Wait-Job $job -Timeout LIMIT) { Receive-Job $job 2>&1 }
else {
  Stop-Job $job
  # only hidden instances started by this call: a Project window the person opened is left alone
  Get-Process WINPROJ -ErrorAction SilentlyContinue |
    Where-Object { $_.StartTime -ge $t0 -and $_.MainWindowHandle -eq 0 } | Stop-Process -Force
  Write-Output 'TIMEOUT'
}
Remove-Job $job -Force
"""


def _convert(src: Path, timeout: float) -> bytes:
    """Project XML for a .mpp, written by Project from a copy of it. Raises ProjectUnavailable or
    RuntimeError with the reason."""
    if not project_available():
        raise ProjectUnavailable(
            f"{src.name} を読むには Microsoft Project が要ります（この PC には見つかりません）。"
            "Project で開いて「名前を付けて保存」から XML 形式で保存したファイルなら、Project なしで読めます"
        )
    tmp = Path(tempfile.mkdtemp(prefix="officetext-mpp-"))
    try:
        copy = tmp / ("plan" + src.suffix.lower())
        shutil.copyfile(src, copy)  # Project opens this copy, never the file it was given
        out = tmp / "plan.xml"
        limit = int(timeout)
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _GUARD_PS.replace("LIMIT", str(limit))],
                capture_output=True,
                text=True,
                timeout=limit + 60,
                env={
                    **os.environ,
                    "OFFICETEXT_SCRIPT": _PROJECT_PS,
                    "OFFICETEXT_SRC": str(copy.resolve()),
                    "OFFICETEXT_OUT": str(out.resolve()),
                },  # fmt: skip
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            raise RuntimeError(f"Project を動かせませんでした（{type(e).__name__}）: {src.name}") from None
        if "TIMEOUT" in r.stdout:
            raise RuntimeError(
                f"Project が {limit} 秒たっても答えなかったので止めました（見えない画面で何かを聞いている可能性）: {src.name}"
            )
        if out.exists() and out.stat().st_size > 0:
            return _read_limited(out)
        last = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()][-1:] or ["出力なし"]
        raise RuntimeError(f"Project が XML に書き出せませんでした: {src.name}（{last[0][:300]}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- reading Project XML


def _t(el, name: str) -> str:
    v = el.findtext(NS + name) if el is not None else None
    return (v or "").strip()


def _date(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s[:19]) if s else None
    except ValueError:
        return None


def _iso(s: str) -> str:
    d = _date(s)
    return d.isoformat() if d else ""


def _minutes(iso: str) -> float | None:
    """An ISO 8601 duration as Project writes it (PT16H0M0S, P2DT4H...) in minutes."""
    m = re.fullmatch(r"(-)?P(?:([0-9.]+)D)?(?:T(?:([0-9.]+)H)?(?:([0-9.]+)M)?(?:([0-9.]+)S)?)?", iso or "")
    if not m or iso in ("P", "PT"):
        return None
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups()[1:])
    total = d * 24 * 60 + h * 60 + mi + s / 60
    return -total if m.group(1) else total


def _days(minutes: float | None, per_day: float) -> str:
    if minutes is None:
        return ""
    v = minutes / per_day
    return f"{v:.0f}" if abs(v - round(v)) < 0.05 else f"{v:.1f}"


def _clean(s: str, limit: int = 300, one_line: bool = True) -> str:
    """Text from the plan without control characters; one-line fields (names) also lose line breaks."""
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", s or "")
    if one_line:
        s = re.sub(r"[\r\n  ]+", " ", s)
    return s.strip()[:limit]


def _int(s: str, digits: int = 10) -> str:
    return s if re.fullmatch(rf"[0-9]{{1,{digits}}}", s or "") else ""


def parse(xml: bytes, converted_by: str = "") -> Plan:
    from lxml import etree

    root = etree.fromstring(xml, etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))
    if root.tag != NS + "Project":
        raise ValueError("Microsoft Project の XML ではありません（最上位が Project ではない）")
    per_day = float(_t(root, "MinutesPerDay") or 480) or 480.0
    warnings: list[str] = []
    aliases = {}
    for ea in root.iter(NS + "ExtendedAttribute"):
        if ea.getparent() is not None and ea.getparent().tag == NS + "ExtendedAttributes":
            fid = _t(ea, "FieldID")
            if fid:
                aliases[fid] = _clean(_t(ea, "Alias") or _t(ea, "FieldName"), 80)
    names = {}
    for r in root.iter(NS + "Resource"):
        uid, name = _int(_t(r, "UID")), _clean(_t(r, "Name"), 120)
        if uid and name and _t(r, "IsNull") != "1":
            names[uid] = name
    assigned: dict[str, list[str]] = {}
    for a in root.iter(NS + "Assignment"):
        ru, tu = _int(_t(a, "ResourceUID")), _int(_t(a, "TaskUID"))
        if ru in names and tu:
            units = _t(a, "Units")
            try:
                share = f" {float(units) * 100:.0f}%" if units and abs(float(units) - 1.0) > 0.001 else ""
            except ValueError:
                share = ""
            assigned.setdefault(tu, []).append(names[ru] + share)

    raw = []
    percent = 0
    for el in root.iter(NS + "Task"):
        if _t(el, "IsNull") == "1":
            continue
        if _t(el, "OutlineLevel") in ("", "0"):  # the project summary task
            percent = int(float(_t(el, "PercentComplete") or 0))
            continue
        if len(raw) >= MAX_TASKS:
            warnings.append(f"タスクが多いので、最初の {MAX_TASKS} 個だけを読みました")
            break
        raw.append(el)
    uid_to_id = {_int(_t(el, "UID")): _int(_t(el, "ID")) for el in raw}
    uid_to_name = {_int(_t(el, "UID")): _clean(_t(el, "Name")) for el in raw}
    tasks: list[Task] = []
    names_by_outline: dict[str, str] = {}
    for el in raw:
        uid, outline, name = _int(_t(el, "UID")), _clean(_t(el, "OutlineNumber"), 40), _clean(_t(el, "Name"))
        names_by_outline[outline] = name
        parents = outline.split(".")[:-1]
        path = tuple(names_by_outline.get(".".join(parents[: i + 1]), "") for i in range(len(parents)))
        base = next((b for b in el.findall(NS + "Baseline") if _t(b, "Number") in ("0", "")), None)
        preds, named = [], []
        for pl in el.findall(NS + "PredecessorLink"):
            pu = _int(_t(pl, "PredecessorUID"))
            kind = LINK_TYPES.get(_t(pl, "Type"), "FS")
            lag_tenths = _t(pl, "LinkLag")
            lag = ""
            try:
                lag_min = float(lag_tenths) / 10 if lag_tenths else 0.0  # LinkLag is in tenths of a minute
            except ValueError:
                lag_min = 0.0
            if lag_min:
                lag = f"{'+' if lag_min > 0 else '-'}{_days(abs(lag_min), per_day)}d"
            link = f"{'' if kind == 'FS' and not lag else kind}{lag}"
            preds.append(f"{uid_to_id.get(pu) or f'uid {pu}'}{link}")
            named.append(f"{uid_to_name.get(pu) or f'uid {pu}'}{link}")
        custom = {}
        for ea in el.findall(NS + "ExtendedAttribute"):
            fid, val = _t(ea, "FieldID"), _clean(_t(ea, "Value"), 200)
            if val:
                custom[aliases.get(fid) or f"field {fid}"] = val
        tasks.append(
            Task(
                uid=uid,
                id=_int(_t(el, "ID")),
                name=name,
                outline=outline,
                level=max(1, min(int(_int(_t(el, "OutlineLevel"), 3) or 1), 32)),
                path=path,
                start=_iso(_t(el, "Start")),
                finish=_iso(_t(el, "Finish")),
                duration_days=_days(_minutes(_t(el, "Duration")), per_day),
                percent=int(float(_t(el, "PercentComplete") or 0)),
                milestone=_t(el, "Milestone") == "1",
                summary=_t(el, "Summary") == "1",
                active=_t(el, "Active") != "0",
                critical=_t(el, "Critical") == "1",
                baseline_start=_iso(_t(base, "Start")),
                baseline_finish=_iso(_t(base, "Finish")),
                deadline=_iso(_t(el, "Deadline")),
                predecessors=", ".join(preds),
                predecessor_names=", ".join(named),
                resources=tuple(assigned.get(uid, [])),
                notes=_clean(_t(el, "Notes"), 2000, one_line=False),
                custom=custom,
            )
        )
    status = _iso(_t(root, "StatusDate"))
    return Plan(
        name=_clean(_t(root, "Title") or _t(root, "Name"), 200),
        tasks=tuple(tasks),
        status_date=status,
        percent=percent,
        converted_by=converted_by,
        warnings=tuple(warnings),
    )
