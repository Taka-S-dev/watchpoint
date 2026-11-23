"""Running `check --due --notify` unattended from the Windows Task Scheduler.

The task starts pythonw (no console window) every few minutes. Each run leaves
a heartbeat next to the data, so a watcher that has quietly stopped shows up in
`status` and the GUI instead of looking like "nothing changed".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from xml.etree import ElementTree as ET

from . import notify
from .check import Status, check_all, send_notifications
from .config import Config, ConfigError, load_config
from .lock import Busy, data_lock
from .store import replace_file

HEARTBEAT = "heartbeat.json"
RUN_LOG = "scheduled.log"
RUN_LOG_MAX = 1_000_000
DEFAULT_TASK = "watchpoint"
LOCK_WAIT_SECONDS = 60
RUN_TIME_LIMIT = dt.timedelta(minutes=30)  # the task's ExecutionTimeLimit
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def fallback_data_dir(config_path: Path) -> Path:
    """Where the data would be if the config cannot be read (the default data_dir)."""
    return config_path.parent / "data"


# --------------------------------------------------------------------------- what is remembered per config
#
# Kept outside the data folder, keyed by the config path: when the config is broken (or the data
# folder unreachable), the run still knows where the data and the drop folder are, and which notice
# it already gave, so the person hears about it once instead of every few minutes.


def _memory_path(config_path: Path) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "state") / "watchpoint"
    key = hashlib.sha256(str(config_path).lower().encode("utf-8")).hexdigest()[:16]
    return base / f"schedule-{key}.json"


def recall(config_path: Path) -> dict:
    try:
        data = json.loads(_memory_path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(config_path: Path, **values) -> None:
    path = _memory_path(config_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {**recall(config_path), "config": str(config_path), **values}
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        replace_file(tmp, path)
    except OSError:
        pass


def remember_config(config_path: Path, cfg: Config) -> None:
    remember(
        config_path,
        data_dir=str(cfg.data_path(config_path)),
        drop_folder=cfg.notify.drop_folder if cfg.notify else None,
    )


# --------------------------------------------------------------------------- heartbeat


def read_heartbeat(data_dir: Path) -> dict | None:
    try:
        data = json.loads((data_dir / HEARTBEAT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_heartbeat(data_dir: Path, beat: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / f".{HEARTBEAT}.tmp"
    tmp.write_text(json.dumps(beat, ensure_ascii=False, indent=1), encoding="utf-8")
    replace_file(tmp, data_dir / HEARTBEAT)


def stale_after(every: int) -> dt.timedelta:
    """A run is late once three intervals have passed (a sleeping PC skips a few)."""
    return dt.timedelta(minutes=max(3 * every, every + 10))


def _local(stamp) -> dt.datetime:
    t = dt.datetime.fromisoformat(stamp)  # TypeError / ValueError for anything else
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def heartbeat_view(data_dir: Path, now: dt.datetime | None = None) -> dict:
    """What `status` and the GUI say about the unattended runs.

    state: none (never ran) / ok / late (no run for a while) / error (the last run could not check).
    """
    beat = read_heartbeat(data_dir)
    if not beat or not beat.get("finished"):
        return {"state": "none", "text": "自動チェックは動いていません（watchpoint schedule install で始めます）"}
    now = now or _now()
    try:
        finished = _local(beat["finished"])
        every = max(1, int(beat.get("every") or 5))
        running = _local(beat["running_since"]) if beat.get("running_since") else None
    except (TypeError, ValueError):
        return {"state": "error", "text": f"{HEARTBEAT} を読めません（次の自動チェックで書き直されます）"}
    view = {"finished": beat["finished"], "every": every, "result": beat.get("result"), "error": beat.get("error")}
    stamp = f"{finished:%m/%d %H:%M}"
    if running is not None and now - running <= RUN_TIME_LIMIT:
        return view | {"state": "ok", "text": f"自動チェック {every} 分おき・{running:%H:%M} からチェック中"}
    if now - finished > stale_after(every):
        minutes = int((now - finished).total_seconds() // 60)
        ago = f"{round(minutes / 60)} 時間" if minutes >= 120 else f"{minutes} 分"
        return view | {
            "state": "late",
            "text": f"自動チェックが {ago}動いていません（最後 {stamp}。{every} 分おきの設定）",
        }
    if beat.get("result") == "error":
        return view | {"state": "error", "text": f"自動チェックが失敗しています（{stamp}）: {beat.get('error')}"}
    detail = "（この回はほかのチェック中のため見送り）" if beat.get("skipped") else ""
    return view | {"state": "ok", "text": f"自動チェック {every} 分おき・最後 {stamp}{detail}"}


# --------------------------------------------------------------------------- one unattended run


def _redirect_output(data_dir: Path) -> None:
    """pythonw has no console: send anything printed to a small log next to the data."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        log = data_dir / RUN_LOG
        try:
            if log.stat().st_size > RUN_LOG_MAX:
                log.replace(log.with_name(RUN_LOG + ".1"))
        except OSError:
            pass
        f = open(log, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115 - until exit
    except OSError:
        f = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - the data folder is unreachable
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def run(config_path: Path, every: int) -> int:
    """One run for the scheduler. Never raises; the outcome goes to the heartbeat (and a notice if it breaks)."""
    try:
        return _run(config_path.resolve(), every)
    except Exception:  # noqa: BLE001 - the last line of defence: a raise here is invisible to everyone
        try:
            traceback.print_exc()
        except Exception:  # noqa: BLE001, S110
            pass
        return 1


def _run(config_path: Path, every: int) -> int:
    memory = recall(config_path)
    cfg: Config | None = None
    error: Exception | None = None
    try:
        cfg = load_config(config_path)
        data_dir = cfg.data_path(config_path)
        remember_config(config_path, cfg)
        drop_folder = cfg.notify.drop_folder if cfg.notify else None
    except Exception as e:  # noqa: BLE001 - a broken config is reported, not raised
        error = e
        data_dir = Path(memory["data_dir"]) if memory.get("data_dir") else fallback_data_dir(config_path)
        drop_folder = memory.get("drop_folder")
    _redirect_output(data_dir)

    before = read_heartbeat(data_dir) or {}
    started = _now().isoformat()
    interrupted = bool(before.get("running_since"))  # the previous run never got to write its end
    try:
        _write_heartbeat(data_dir, {**before, "running_since": started})
    except OSError:
        pass

    beat: dict = {"started": started, "every": every, "config": str(config_path)}
    code = 0
    try:
        if error is not None:
            raise error
        assert cfg is not None
        with data_lock(data_dir, wait=LOCK_WAIT_SECONDS):
            results = check_all(cfg.targets, cfg, data_dir, due_only=True, via="scheduler")
            sent = send_notifications(results, cfg, data_dir) if cfg.notify else 0
        counts = {s: sum(r.status is s for r in results) for s in (Status.CHANGED, Status.FAILED)}
        beat |= {
            "result": "ok",
            "checked": sum(r.status not in (Status.NOT_DUE, Status.SKIPPED) for r in results),
            "changed": counts[Status.CHANGED],
            "failed": counts[Status.FAILED],
            "sent": sent,
        }
    except Busy:
        # The GUI or a manual check holds the data; the next run catches up. Keep the last real
        # outcome, so a failing watcher does not look healthy just because one run was skipped.
        beat |= {k: before[k] for k in ("result", "error", "checked", "changed", "failed", "sent") if k in before}
        beat["skipped"] = "busy"
    except ConfigError as e:
        beat |= {"result": "error", "error": str(e).splitlines()[0]}
        code = 2
    except Exception as e:  # noqa: BLE001 - the scheduler shows nothing, so everything is recorded
        traceback.print_exc()
        beat |= {"result": "error", "error": f"{type(e).__name__}: {e}"}
        code = 1
    beat["finished"] = _now().isoformat()
    _announce(config_path, memory, drop_folder, beat, interrupted)
    try:
        _write_heartbeat(data_dir, beat)
    except OSError as e:
        print(f"{HEARTBEAT} を書けませんでした: {e}", file=sys.stderr)
    return code


def _announce(config_path: Path, memory: dict, folder: str | None, beat: dict, interrupted: bool) -> None:
    """Tell the person once when unattended runs start failing, and once when they work again.

    Which notice was given last is remembered outside the data folder, so an error whose text
    changes every run (a time, a counter) or an unreachable data folder does not repeat it.
    """
    if beat.get("skipped"):
        return
    failing = beat.get("result") == "error"
    told = memory.get("announced")
    text = None
    if failing and told != "failing":
        text = f"watchpoint の自動チェックが止まっています: {beat['error']}"
        state = "failing"
    elif interrupted and not failing and told != "failing":
        text = f"watchpoint の前回の自動チェックが終わりませんでした（{RUN_TIME_LIMIT.seconds // 60} 分で止められた可能性）"
        state = "ok"
    elif not failing and told == "failing":
        text = "watchpoint の自動チェックが戻りました"
        state = "ok"
    if text is None or not folder:
        return
    try:
        notify.drop(Path(folder), text)
    except Exception:  # noqa: BLE001 - the drop folder is unreachable; the heartbeat still records the run
        return
    remember(config_path, announced=state)


# --------------------------------------------------------------------------- the scheduled task


def launcher() -> tuple[str, list[str]]:
    """pythonw next to the running interpreter, so no console window opens every few minutes."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    # -P: the working folder (the config folder) is not put on sys.path, so a stray yaml.py there is not imported
    return str(windowless if windowless.exists() else exe), ["-P", "-m", "watchpoint"]


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def task_xml(every: int, command: str, args: list[str], workdir: Path, start: dt.datetime | None = None) -> str:
    """A per-user task: runs only while the person is signed in, so no password is stored."""
    start = (start or _now()).replace(second=0)
    ET.register_namespace("", TASK_NS)

    def sub(parent, tag, text=None):
        node = ET.SubElement(parent, f"{{{TASK_NS}}}{tag}")
        if text is not None:
            node.text = text
        return node

    task = ET.Element(f"{{{TASK_NS}}}Task", {"version": "1.2"})
    info = sub(task, "RegistrationInfo")
    sub(info, "Description", "watchpoint: 間隔の過ぎた見張りをチェックし、変更を知らせとして置く")
    trigger = sub(sub(task, "Triggers"), "TimeTrigger")
    rep = sub(trigger, "Repetition")
    sub(rep, "Interval", f"PT{every}M")
    sub(rep, "StopAtDurationEnd", "false")
    sub(trigger, "StartBoundary", start.isoformat())
    sub(trigger, "Enabled", "true")
    principal = sub(sub(task, "Principals"), "Principal", None)
    principal.set("id", "Author")
    sub(principal, "LogonType", "InteractiveToken")
    sub(principal, "RunLevel", "LeastPrivilege")
    settings = sub(task, "Settings")
    for tag, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("ExecutionTimeLimit", f"PT{RUN_TIME_LIMIT.seconds // 60}M"),
        ("Enabled", "true"),
    ):
        sub(settings, tag, value)
    idle = sub(settings, "IdleSettings")
    sub(idle, "StopOnIdleEnd", "false")
    actions = sub(task, "Actions", None)
    actions.set("Context", "Author")
    exec_ = sub(actions, "Exec")
    sub(exec_, "Command", command)
    sub(exec_, "Arguments", " ".join(_quote(a) for a in args))
    sub(exec_, "WorkingDirectory", str(workdir))
    return ET.tostring(task, encoding="unicode", xml_declaration=False)


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    if os.name != "nt":
        raise ScheduleError("タスク スケジューラは Windows だけです")
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        encoding="mbcs" if os.name == "nt" else "utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class ScheduleError(Exception):
    pass


def install(name: str, config_path: Path, every: int) -> str:
    remember_config(config_path.resolve(), load_config(config_path))  # known even if the config breaks later
    command, prefix = launcher()
    args = [*prefix, "schedule", "run", "--config", str(config_path.resolve()), "--every", str(every)]
    xml = task_xml(every, command, args, config_path.resolve().parent)
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "task.xml"
        f.write_text('<?xml version="1.0" encoding="UTF-16"?>\n' + xml, encoding="utf-16")
        out = _schtasks("/Create", "/TN", name, "/XML", str(f), "/F")
    if out.returncode != 0:
        raise ScheduleError((out.stderr or out.stdout).strip() or f"schtasks が {out.returncode} で終わりました")
    return " ".join([_quote(command), *(_quote(a) for a in args)])


def remove(name: str) -> bool:
    try:
        return _schtasks("/Delete", "/TN", name, "/F").returncode == 0
    except (ScheduleError, OSError):
        return False


def run_now(name: str) -> None:
    out = _schtasks("/Run", "/TN", name)
    if out.returncode != 0:
        raise ScheduleError((out.stderr or out.stdout).strip())


def query(name: str) -> dict | None:
    """The registered task as Windows holds it, or None when there is none."""
    try:
        out = _schtasks("/Query", "/TN", name, "/XML")
        if out.returncode != 0:
            return None
        root = ET.fromstring(out.stdout.strip().split("?>", 1)[-1])
    except (ScheduleError, OSError, ET.ParseError):
        return None
    ns = {"t": TASK_NS}

    def text(path: str) -> str | None:
        node = root.find(path, ns)
        return node.text if node is not None else None

    interval = text(".//t:Repetition/t:Interval") or ""
    return {
        "enabled": (text("./t:Settings/t:Enabled") or "true") == "true",
        "every": int(interval[2:-1]) if interval.startswith("PT") and interval.endswith("M") else None,
        "command": text(".//t:Exec/t:Command"),
        "arguments": text(".//t:Exec/t:Arguments") or "",
    }
