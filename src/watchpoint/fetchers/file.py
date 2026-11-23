"""Files and folders on this PC or a share, copied without ever being opened for writing."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ..textify import file_stamp, folder_listing
from .base import Fetched, FileBusy, sha256_file, sha256_text, textify_copy

if TYPE_CHECKING:
    from ..config import Target


def probe(target: Target) -> str:
    """size and mtime: an unchanged file is not copied again."""
    try:
        st = Path(target.source).stat()
    except FileNotFoundError:
        raise FileNotFoundError(f"見つかりません: {target.source}") from None
    if target.watch != "timestamp" and target.max_size_mb and st.st_size > target.max_size_mb * 1024 * 1024:
        raise ValueError(
            f"大きすぎます（{st.st_size / 1024 / 1024:.1f} MB > max_size_mb {target.max_size_mb}）: {target.source}"
        )
    return f"size={st.st_size};mtime={st.st_mtime_ns}"


def fetch_folder(target: Target) -> Fetched:
    text = folder_listing(target)
    return Fetched(text, sha256_text(text))


def fetch_timestamp(target: Target) -> Fetched:
    text = file_stamp(target)
    return Fetched(text, sha256_text(text))


def _open_shared(src: Path):
    """Open for reading while letting others write, rename and delete the file.

    Python's open() does not share delete access on Windows, and Office and most editors
    save by renaming: while a large file on a slow share was being copied, the owner's
    save failed with "file in use".
    """
    if os.name != "nt":
        return open(src, "rb")  # noqa: SIM115 - closed by the caller
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]  # fmt: skip
    generic_read, share_all, open_existing, sequential = 0x80000000, 0x7, 3, 0x08000000
    handle = kernel32.CreateFileW(str(src), generic_read, share_all, None, open_existing, sequential, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def _copy_shared(src: Path, dest: Path) -> None:
    with _open_shared(src) as fin, open(dest, "wb") as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def copy_source(source: str, dest_dir: Path) -> Path:
    """Copy the source into dest_dir without ever opening it for writing.

    If the file changes while it is copied (someone is saving it), the copy is
    rejected so a half-written file is never compared.
    """
    src = Path(source)
    if not src.exists():
        raise FileNotFoundError(f"見つかりません: {source}")
    if not src.is_file():
        raise IsADirectoryError(f"ファイルではありません: {source}")
    dest = dest_dir / ("original" + src.suffix.lower())
    before = src.stat()
    try:
        _copy_shared(src, dest)  # reads the source only
    except PermissionError:
        raise PermissionError(
            f"読めませんでした（アクセス権がないか、ほかのアプリが開いて読めない状態）: {source}"
        ) from None
    after = src.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise FileBusy(f"コピーしている間にファイルが変わりました（保存中の可能性）: {source}")
    return dest


def fetch(target: Target, workdir: Path) -> Fetched:
    original = copy_source(target.source, workdir)
    digest = sha256_file(original)
    return Fetched(textify_copy(original, target, digest), digest)
