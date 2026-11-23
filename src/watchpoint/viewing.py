"""Showing a fetched copy to the person, so that nothing they do with it reaches the original or the history.

What opens is never the original, and never the copy kept in the history either: it is a third
copy in the temp folder, marked read-only, so Excel or Word opens it read-only and an edit
cannot be saved over anything by accident. Formats that could run something (scripts,
programs, shortcuts, macro files, web pages) are only shown in Explorer: the copy has lost the
original's "downloaded from the internet" mark, so opening it could run it.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .store import safe_name

OPEN_DIRECTLY = {
    ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml",
    ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp",
}  # fmt: skip


def view_dir() -> Path:
    return Path(tempfile.gettempdir()) / "watchpoint-view"


def viewing_copy(kept: Path, name: str, snapshot: str) -> Path:
    """A read-only copy of a kept file, named so the person can tell it is not the original."""
    folder = view_dir()
    folder.mkdir(parents=True, exist_ok=True)
    label = "テキスト" if kept.name == "text.txt" else "コピー"
    dest = folder / f"{safe_name(name)}_{snapshot}（{label}・読み取り専用）{kept.suffix.lower()}"
    if dest.exists():
        try:
            os.chmod(dest, stat.S_IWRITE)
            dest.unlink()
        except OSError:
            return dest  # still open in Excel from last time: show that one
    shutil.copyfile(kept, dest)
    os.chmod(dest, stat.S_IREAD)  # Excel and Word open it read-only
    return dest


def show(kept: Path, name: str, snapshot: str) -> tuple[Path, bool]:
    """Open a read-only viewing copy with its app, or point at it in Explorer. Returns (path, opened directly)."""
    path = viewing_copy(kept, name, snapshot)
    direct = path.suffix.lower() in OPEN_DIRECTLY
    if os.name == "nt":
        if direct:
            os.startfile(path)  # noqa: S606 - a read-only copy, never the original
        else:
            subprocess.Popen(["explorer", f"/select,{path}"])  # noqa: S603,S607 - shows it, runs nothing
    return path, direct


# --------------------------------------------------------------------------- the place a target watches


def place_of(target) -> tuple[str, str] | None:
    """How and where "open the place" goes: ("browser", url), ("folder", dir) or ("select", file).

    None when there is no safe default: opening a SharePoint file's URL would open the master itself
    in Excel or Word on the web, and an API or a command has no place to show. `link:` sets one.
    """
    if target.link:
        return _resolve(target.link)
    kind = target.kind
    if kind in ("web", "confluence", "download"):
        return ("browser", target.source)
    if kind == "file":
        return ("select", os.path.expandvars(os.path.expanduser(target.source)))
    if kind == "folder":
        p = Path(os.path.expandvars(os.path.expanduser(target.source)))
        while not p.is_dir() and p != p.parent:  # a pattern such as 議事録/*.docx: its folder
            p = p.parent
        return ("folder", str(p))
    return None


def _resolve(link: str) -> tuple[str, str]:
    if link.lower().startswith(("http://", "https://")):
        return ("browser", link)
    p = Path(os.path.expandvars(os.path.expanduser(link)))
    return ("folder", str(p)) if p.is_dir() else ("select", str(p))


PLACE_LABEL = {"browser": "ページを開く", "folder": "フォルダを開く", "select": "フォルダで開く"}


def open_place(how: str, where: str) -> None:
    """Show the place. A file is only selected in Explorer; nothing but a folder or a web page is opened."""
    if how == "browser":
        if not where.lower().startswith(("http://", "https://")):
            raise ValueError(f"開けるのは http(s) の URL だけです: {where}")
        import webbrowser

        webbrowser.open(where)
        return
    p = Path(where)
    if how == "folder":
        if not p.is_dir():
            raise FileNotFoundError(f"フォルダが見つかりません: {where}")
        subprocess.Popen(["explorer", str(p)])  # noqa: S603,S607 - a folder, checked above
        return
    if not p.exists():
        if p.parent.is_dir():  # the file is gone (renamed, deleted): at least its folder
            subprocess.Popen(["explorer", str(p.parent)])  # noqa: S603,S607
            return
        raise FileNotFoundError(f"見つかりません: {where}")
    subprocess.Popen(["explorer", f"/select,{p}"])  # noqa: S603,S607 - shows it, runs nothing
