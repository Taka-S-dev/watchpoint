"""One fetch of a file per check, shared by every target that reads the same file.

Several targets often watch one workbook (column B in one, column G in another). Without this, a
check would start a hidden Excel, or download the file, once per target: slow, and a sign-in
timeout would be waited out once per target. Inside `shared_copies()`, the first target to ask
fetches the file and the others wait for it and get a copy; a failure is shared the same way.
Outside it (trying one target, a single command) every fetch is its own.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from pathlib import Path


class _Entry:
    def __init__(self, where: Path) -> None:
        self.where = where
        self.lock = threading.Lock()
        self.path: Path | None = None
        self.error: Exception | None = None


class _Copies:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.entries: dict[Hashable, _Entry] = {}

    def get(self, key: Hashable, make: Callable[[Path], Path]) -> Path:
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                entry = self.entries[key] = _Entry(self.root / str(len(self.entries) + 1))
        with entry.lock:  # a second target for the same file waits here for the first
            if entry.path is None and entry.error is None:
                entry.where.mkdir(parents=True)
                try:
                    entry.path = make(entry.where)
                except Exception as e:
                    entry.error = e
            if entry.error is not None:
                raise entry.error
            assert entry.path is not None
            return entry.path


_active: _Copies | None = None
_active_lock = threading.Lock()


@contextmanager
def shared_copies() -> Iterator[None]:
    """Share fetched files between the targets checked inside this block (an inner block joins the outer)."""
    global _active
    with _active_lock:
        if _active is not None:
            owner = False
        else:
            owner = True
            tmp = tempfile.TemporaryDirectory(prefix="watchpoint-shared-")
            _active = _Copies(Path(tmp.name))
    try:
        yield
    finally:
        if owner:
            with _active_lock:
                _active = None
            tmp.cleanup()


def forget(key: Hashable) -> None:
    """Drop a shared file found unreadable (caught mid-save), so the next ask fetches it again."""
    copies = _active
    if copies is not None:
        with copies.lock:
            copies.entries.pop(key, None)


def copy_of(key: Hashable, make: Callable[[Path], Path], workdir: Path) -> Path:
    """make(dir) saves the file into dir and returns it; the file is returned inside workdir.

    Within `shared_copies()` make runs once per key and every caller gets its own copy, so a
    target's saved original never depends on another's.
    """
    copies = _active
    if copies is None:
        return make(workdir)
    src = copies.get(key, make)
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / src.name
    shutil.copy2(src, dest)
    return dest
