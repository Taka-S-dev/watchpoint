"""Folders as a listing of files (name, size, time), and a file's time alone."""

from __future__ import annotations

import datetime as dt
import glob
from pathlib import Path
from typing import TYPE_CHECKING

from .base import SECTION_PREFIX, shape

if TYPE_CHECKING:
    from ..config import Target

_NOISE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
_NOISE_SUFFIXES = (".tmp", ".crdownload", ".partial")


def _is_noise_file(name: str) -> bool:
    """Lock files, thumbnail caches and half-finished downloads come and go on their own."""
    low = name.lower()
    return low.startswith(("~$", ".~lock.", "~wr")) or low in _NOISE_NAMES or low.endswith(_NOISE_SUFFIXES)


def folder_listing(target: Target) -> str:
    """Files under a folder (or matching a wildcard) with size and modified time. Nothing is copied."""
    src = target.source
    if "*" in src or "?" in src:
        parts = Path(src).parts
        fixed = next(i for i, p in enumerate(parts) if "*" in p or "?" in p)
        root = Path(*parts[:fixed]) if fixed else Path(".")
        matches = glob.glob(src, recursive=target.recursive)
    else:
        root = Path(src)
        if not root.is_dir():
            raise FileNotFoundError(f"見つかりません: {src}")
        matches = glob.glob(str(root / ("**/*" if target.recursive else "*")), recursive=target.recursive)
    if not root.exists():
        raise FileNotFoundError(f"見つかりません: {root}")
    files = sorted(Path(m) for m in matches if Path(m).is_file() and not _is_noise_file(Path(m).name))
    out = [f"{SECTION_PREFIX}フォルダ: {src}"]
    for f in files:
        st = f.stat()
        try:
            rel = f.relative_to(root)
        except ValueError:
            rel = f
        mtime = dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        out.append(f"[ファイル={rel}] サイズ={st.st_size:,} | 更新={mtime}")
    return shape("\n".join(out) + "\n", target)


def file_stamp(target: Target) -> str:
    """A file's size and modified time, for `watch: timestamp`. The file is not opened."""
    src = Path(target.source)
    if not src.exists():
        raise FileNotFoundError(f"見つかりません: {target.source}")
    if not src.is_file():
        raise IsADirectoryError(f"ファイルではありません: {target.source}")
    st = src.stat()
    mtime = dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return f"[ファイル={src.name}] サイズ={st.st_size:,} | 更新={mtime}\n"
