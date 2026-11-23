"""Files behind a web address (a Confluence attachment, a file on an intranet site), downloaded and read
the way the same file on a share is read: an Excel file by its rows or cells, a Word file by its text."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import url_suffix as suffix_of
from .base import Fetched, sha256_file, textify_copy
from .net import http_get

if TYPE_CHECKING:
    from ..config import Target

# A Confluence attachment link copied from a page names the version shown then: kept, it would
# fetch that same version for ever and never see the file replaced.
_PINNED = {"version", "modificationDate", "api", "cacheVersion"}


def latest_url(url: str) -> str:
    """The address of the file's latest version: a Confluence attachment loses the version it was copied at."""
    parts = urllib.parse.urlsplit(url)
    if "/download/attachments/" not in parts.path:
        return url.split("#")[0]
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k not in _PINNED]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query), fragment=""))


def download(target: Target, workdir: Path) -> Path:
    """The file saved into workdir as original.<ext>; a page answered in its place (a sign-in) is refused."""
    suffix = suffix_of(target.source)
    r = http_get(target, latest_url(target.source))
    ctype = r.headers.get("content-type", "").lower()
    if "text/html" in ctype:
        raise PermissionError(
            "ファイルではなく Web ページが返りました。ログインの画面か、ファイルが無くなった可能性があります"
            + ("。auth を設定してください" if not target.auth else "。資格情報（auth / secret）を確かめてください")
            + f": {target.source}"
        )
    dest = workdir / ("original" + suffix)
    dest.write_bytes(r.content)
    return dest


def fetch(target: Target, workdir: Path) -> Fetched:
    dest = download(target, workdir)
    digest = sha256_file(dest)
    return Fetched(textify_copy(dest, target, digest), digest)
