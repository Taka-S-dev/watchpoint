"""Excel and Word files (SharePoint) saved as a copy by a hidden, read-only Office."""

from __future__ import annotations

import threading
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from .base import Fetched, sha256_file, textify_copy
from .file import copy_source

if TYPE_CHECKING:
    from ..config import Target

_office_lock = threading.Lock()  # one Office instance at a time
OFFICE_DEFAULT_TIMEOUT = 120.0
_IMAGE = {"Excel.Application": "EXCEL.EXE", "Word.Application": "WINWORD.EXE"}


def _office_pids(image: str) -> set[int]:
    import csv
    import io
    import subprocess

    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    pids = set()
    for row in csv.reader(io.StringIO(out)):
        if len(row) > 1 and row[1].isdigit():
            pids.add(int(row[1]))
    return pids


def _own_pid(app) -> set[int]:
    """The process of this Office instance, from its main window (Excel has Application.Hwnd)."""
    try:
        import win32process

        return {win32process.GetWindowThreadProcessId(int(app.Hwnd))[1]}
    except Exception:  # noqa: BLE001 - Word has no Application.Hwnd
        return set()


def _single(pids: set[int]) -> set[int]:
    return pids if len(pids) == 1 else set()


def _kill(pids: set[int]) -> None:
    import subprocess

    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def fetch(target: Target, workdir: Path) -> Fetched:
    """Let Excel or Word open the file read-only (with the user's Office sign-in) and save a copy.

    Used for SharePoint Online without sync and without an app registration.
    The source is never saved: Excel uses SaveCopyAs, Word saves a copy and
    closes without saving the original.

    The Office instance is hidden, so a sign-in prompt would block forever. A
    watchdog ends the instance this call started (never the user's own Excel)
    when it does not finish within the timeout.
    """
    timeout = target.timeout if "timeout" in target.model_fields_set else OFFICE_DEFAULT_TIMEOUT
    dest = office_copy(target.source, workdir, timeout)
    digest = sha256_file(dest)
    return Fetched(textify_copy(dest, target, digest), digest)


def office_copy(source: str, workdir: Path, timeout: float = OFFICE_DEFAULT_TIMEOUT) -> Path:
    """Have a hidden Excel or Word save a copy of source into workdir (as original.<ext>) and return it.

    Only a URL (SharePoint) is opened where it is. A file on this PC or a share is copied first and
    Excel or Word opens the copy, so the original is never open in an Office app, not even read-only.
    """
    is_url = source.startswith(("http://", "https://"))
    suffix = Path(urllib.parse.urlsplit(source).path).suffix.lower() if is_url else Path(source).suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".docx"):
        raise ValueError(f"fetch: office で扱えるのは .xlsx / .xlsm / .docx です（{suffix or '拡張子なし'}）")
    dest = (workdir / ("original" + suffix)).resolve()
    if not is_url:
        local = workdir / "local"
        local.mkdir(parents=True, exist_ok=True)
        source = str(copy_source(source, local).resolve())  # the same read-only copy as any file
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        raise RuntimeError("fetch: office には Windows と pywin32 が要ります") from None

    progid = "Word.Application" if suffix == ".docx" else "Excel.Application"
    with _office_lock:
        pythoncom.CoInitialize()
        before = _office_pids(_IMAGE[progid])
        started: set[int] = set()
        timed_out = threading.Event()

        def watchdog() -> None:
            timed_out.set()
            if started:
                _kill(started)
                return
            # Still starting: end a new instance only if exactly one appeared (the person's own
            # Excel opened in the same seconds must never be killed).
            new = _office_pids(_IMAGE[progid]) - before
            if len(new) == 1:
                _kill(new)

        timer = threading.Timer(timeout, watchdog)
        timer.daemon = True
        app = None
        try:
            timer.start()
            app = win32com.client.DispatchEx(progid)
            started.update(_own_pid(app) or _single(_office_pids(_IMAGE[progid]) - before))
            app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable: macros in the file never run
            if progid == "Excel.Application":
                _excel_copy(app, source, dest)
            else:
                _word_copy(app, source, dest)
        except NotReadOnly:
            raise
        except Exception as e:  # noqa: BLE001
            if timed_out.is_set():
                raise _office_timeout(timeout, progid, source) from None
            raise ConnectionError(
                f"{_app_name(progid)} で開けませんでした（サインインかアクセス権を確かめてください）: {source}（{e}）"
            ) from None
        finally:
            timer.cancel()
            app = None
            pythoncom.CoUninitialize()
    if timed_out.is_set() and not dest.exists():
        raise _office_timeout(timeout, progid, source)
    if not dest.exists():
        raise ConnectionError(f"コピーを保存できませんでした: {source}")
    return dest


def _app_name(progid: str) -> str:
    return "Word" if progid.startswith("Word") else "Excel"


def _office_timeout(timeout: float, progid: str, source: str) -> TimeoutError:
    return TimeoutError(
        f"{timeout:.0f} 秒たっても開けませんでした。サインインの画面が出ている可能性があります。"
        f"いつもの {_app_name(progid)} で一度開いてサインインしてから、もう一度試してください: {source}"
    )


class NotReadOnly(Exception):
    """Office opened the source so it could be edited; nothing was saved and it was closed unsaved."""


def _refuse_unless_read_only(read_only, app_name: str, source: str) -> None:
    """Stop before anything else happens if Office did not honour ReadOnly=True.

    With AutoSave and co-authoring on SharePoint, a file opened for editing could have values
    recalculated on open (NOW(), links) saved back to the original. Closing unsaved here means
    the original is never touched, at the price of a failed check.
    """
    if read_only is not True:
        raise NotReadOnly(
            f"{app_name} が読み取り専用で開けなかったので、何もせずに閉じました（元のファイルは書き換えていません）。"
            f"ブラウザで落としたファイルを見張る方法に切り替えてください: {source}"
        )


def _excel_copy(app, source: str, dest: Path) -> None:
    wb = None
    try:
        app.Visible = False
        app.DisplayAlerts = False
        app.AskToUpdateLinks = False
        app.EnableEvents = False
        wb = app.Workbooks.Open(
            source, UpdateLinks=0, ReadOnly=True, IgnoreReadOnlyRecommended=True, Notify=False, AddToMru=False
        )
        _refuse_unless_read_only(wb.ReadOnly, "Excel", source)
        wb.SaveCopyAs(str(dest))
    finally:
        try:
            if wb is not None:
                wb.Close(SaveChanges=False)
                wb = None  # release the proxy before Excel goes away
            app.Quit()
        except Exception:  # noqa: BLE001 - already gone (e.g. ended by the watchdog)
            pass


def _word_copy(app, source: str, dest: Path) -> None:
    doc = None
    try:
        app.Visible = False
        app.DisplayAlerts = 0
        doc = app.Documents.Open(source, ReadOnly=True, AddToRecentFiles=False, Visible=False, ConfirmConversions=False)
        _refuse_unless_read_only(doc.ReadOnly, "Word", source)
        doc.SaveAs2(FileName=str(dest), FileFormat=16, AddToRecentFiles=False)  # 16 = wdFormatDocumentDefault
    finally:
        try:
            if doc is not None:
                doc.Close(SaveChanges=0)
                doc = None
            app.Quit(SaveChanges=0)
        except Exception:  # noqa: BLE001
            pass
