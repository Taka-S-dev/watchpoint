"""Leaving notifications in a drop folder, one JSON file each (folder hand-off format v1).

Any app that watches the folder can show them. The file is written under a
dot-name first and renamed, so a reader never sees half a file.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets as pysecrets
from pathlib import Path

SOURCE = "watchpoint"
MAX_TEXT = 1000


def _clean(text: str) -> str:
    text = "".join(" " if (ch < " " and ch != "\n") or "\x7f" <= ch <= "\x9f" else ch for ch in text)
    return text[:MAX_TEXT].strip() or "(空)"


def drop(folder: Path, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    name = f"{now:%Y%m%d-%H%M%S}-{now.microsecond // 1000:03d}-{SOURCE}-{pysecrets.token_hex(4)}.json"
    body = {"v": 1, "type": "notify", "text": _clean(text), "source": SOURCE}
    tmp = folder / f".{name}.tmp"
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    final = folder / name
    os.replace(tmp, final)
    return final
