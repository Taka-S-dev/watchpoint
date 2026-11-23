"""Allow one watchpoint at a time per data folder (a person and an AI agent may run it together)."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


class Busy(Exception):
    pass


@contextmanager
def data_lock(data_dir: Path, wait: float = 30.0):
    """Hold an OS file lock; it is released by the OS even if the process dies."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".lock"
    f = open(path, "a+b")  # noqa: SIM115 - held open for the whole run; closed in the finally below
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                _lock(f)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy(f"ほかの watchpoint が動いています（{wait:.0f} 秒待ちました）: {path}") from None
                time.sleep(0.5)
        try:
            yield
        finally:
            _unlock(f)
    finally:
        f.close()


if os.name == "nt":
    import msvcrt

    def _lock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f) -> None:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
