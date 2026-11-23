"""What every fetcher returns, and the helpers they share. A fetcher only reads its source and
writes only into the work folder it is given."""

from __future__ import annotations

import hashlib
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..textify import UnsupportedFormat, file_to_text

if TYPE_CHECKING:
    from ..config import Target

RETRY_WAIT_SECONDS = 2.0
UNSUPPORTED_MARK = "(テキストにできない形式"


class FileBusy(Exception):
    """The source changed while it was being copied, or could not be read as its format."""


@dataclass
class Fetched:
    text: str
    digest: str
    meta: dict = field(default_factory=dict)
    probe: str | None = None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def retrying(fn):
    """A file caught in the middle of a save is fetched once more after a short wait."""
    for attempt in (1, 2):
        try:
            return fn()
        except FileBusy:
            if attempt == 2:
                raise
            time.sleep(RETRY_WAIT_SECONDS)
    raise AssertionError("unreachable")


def textify_copy(original: Path, target: Target, digest: str) -> str:
    try:
        return file_to_text(original, target)
    except UnsupportedFormat as e:
        return f"{UNSUPPORTED_MARK} {e}: sha256={digest})\n"
    except (zipfile.BadZipFile, KeyError, EOFError) as e:
        raise FileBusy(
            f"ファイルを読めませんでした（保存中か、壊れている可能性）: {target.source}（{type(e).__name__}）"
        ) from e
