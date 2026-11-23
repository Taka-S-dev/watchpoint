"""Web pages (HTML, or JSON when the server says so)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..textify import html_to_text, json_to_text, shape
from .base import Fetched, sha256_text
from .net import http_get

if TYPE_CHECKING:
    from ..config import Target


def fetch(target: Target, workdir: Path) -> Fetched:
    r = http_get(target, target.source)
    ctype = r.headers.get("content-type", "")
    (workdir / ("original.json" if "json" in ctype else "original.html")).write_bytes(r.content)
    if "json" in ctype:
        try:
            data = r.json()
        except ValueError:
            raise ValueError(
                f"JSON として読めませんでした（ログイン画面が返っている可能性）: {target.source}"
            ) from None
        text = shape(json_to_text(data, target), target)
    elif "charset=" in ctype.lower():
        text = shape(html_to_text(r.text, target), target, tables=False)
    else:
        # No charset in the header (common on intranet pages in Shift_JIS): let the
        # HTML parser read <meta charset> from the bytes instead of assuming UTF-8.
        text = shape(html_to_text(r.content, target), target, tables=False)
    meta = {k: r.headers[k] for k in ("last-modified", "etag") if k in r.headers}
    return Fetched(text, sha256_text(text), meta=meta)
