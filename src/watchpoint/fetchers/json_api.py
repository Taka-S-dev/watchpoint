"""JSON APIs, with offset paging for ticket lists (Redmine and the like)."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from ..textify import json_records, json_to_text, records_to_text, shape
from .base import Fetched, sha256_text
from .net import http_get

if TYPE_CHECKING:
    from ..config import Target


def fetch(target: Target, workdir: Path) -> Fetched:
    if target.paginate == "offset":
        records = paginate_offset(target)
        (workdir / "original.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
        text = shape(records_to_text(records, target), target)
        return Fetched(text, sha256_text(text), meta={"count": len(records)})
    r = http_get(target, target.source, accept="application/json")
    (workdir / "original.json").write_bytes(r.content)
    try:
        data = r.json()
    except ValueError:
        raise ValueError(f"JSON として読めませんでした（ログイン画面が返っている可能性）: {target.source}") from None
    text = shape(json_to_text(data, target), target)
    return Fetched(text, sha256_text(text))


def paginate_offset(target: Target, page_cap: int = 50) -> list[dict]:
    """Redmine style: offset/limit parameters and a total_count in the response."""
    parsed = urllib.parse.urlsplit(target.source)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)  # f[]=a&f[]=b keeps both
    given = dict(pairs)
    pairs = [(k, v) for k, v in pairs if k not in ("offset", "limit")]
    base = urllib.parse.urlunsplit(parsed._replace(query=""))
    limit = int(given.get("limit", 100))
    offset = int(given.get("offset", 0))
    page_size: int | None = None  # what the server really returns per page (it may cap limit)
    records: list[dict] = []
    for _ in range(page_cap):
        params = [*pairs, ("offset", str(offset)), ("limit", str(limit))]
        r = http_get(target, base, params=params, accept="application/json")
        try:
            data = r.json()
        except ValueError:
            raise ValueError(
                f"JSON として読めませんでした（ログイン画面が返っている可能性）: {target.source}"
            ) from None
        page = json_records(data, target)
        records += page
        total = data.get("total_count") if isinstance(data, dict) else None
        offset += len(page)
        if page_size is None:
            page_size = len(page)
        if not page or (total is not None and offset >= int(total)):
            return records
        if total is None and len(page) < min(limit, page_size):
            return records
    raise ValueError(f"{page_cap} ページを超えました。条件を絞ってください: {target.source}")
