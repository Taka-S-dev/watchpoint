"""Telling the addresses SharePoint hands out apart, and finding the file's own address in them.

A file on SharePoint has several addresses. Only the file's own one (…/sites/A/Shared Documents/q.xlsx)
names the file in a way Excel can open read-only and watchpoint can keep comparing; the others are made
for a browser:

- a sharing link (`/:x:/r/…`, `/:x:/s/…`), made by 「リンクをコピー」
- the browser view (`/_layouts/15/Doc.aspx?sourcedoc=…`), the address bar while the file is open in it

Some of them carry the file's address (an `/r/` link, an on-premises `sourcedoc=/sites/…`); the rest name
the file by an id only the server can resolve, and the person has to look the address up.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

OFFICE_SUFFIXES = (".xlsx", ".xlsm", ".docx")
# /:x:/r/sites/… or /:x:/s/site/token: x for Excel, w for Word (the files fetch: office reads)
_SHARING = re.compile(r"^/:([xw]):/([a-z])(/.*)?$", re.I)
_VIEWERS = (
    "/_layouts/15/doc.aspx",
    "/_layouts/15/xlviewer.aspx",
    "/_layouts/15/wopiframe.aspx",
    "/_layouts/15/doc2.aspx",
)
# what a browser adds to the file's own address (?web=1 opens it in the browser instead)
_BROWSER_QUERY = {"web", "d", "csf", "e", "cid", "ovuser", "clickparams", "wdorigin", "wdlor", "isspolink"}

HOW_TO_FIND = (
    "ファイルの URL は、Excel・Word のアプリでそのファイルを開き、［ファイル］→［情報］→［パスのコピー］で取れます"
    "（ブラウザで開いているときは［ファイル］→［情報］→［デスクトップ アプリで開く］から）"
)


@dataclass(frozen=True)
class Address:
    form: str  # "file", "sharing" or "browser"
    label: str  # what to call it to the person
    file_url: str | None  # the file's own address: the same as the input for "file", else when it can be read

    def advice(self, source: str) -> str:
        """What to write instead, for an address that is not the file's own."""
        if self.file_url:
            return f"これは{self.label}です。ファイルの URL に直してください: {self.file_url}"
        return f"これは{self.label}で、どのファイルかを URL から読めません。{HOW_TO_FIND}: {source}"


def looks_like_sharepoint(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    path = parts.path.lower()
    return (
        parts.hostname is not None
        and parts.scheme in ("http", "https")
        and (
            parts.hostname.lower().endswith(".sharepoint.com")
            or "/_layouts/15/" in path
            or bool(_SHARING.match(parts.path))
        )
    )


def classify(url: str) -> Address | None:
    """What kind of SharePoint address url is, or None when it is not one (or not of a file)."""
    if not looks_like_sharepoint(url):
        return None
    parts = urllib.parse.urlsplit(url.strip())
    origin = f"{parts.scheme}://{parts.netloc}"
    path_low = parts.path.lower()

    m = _SHARING.match(parts.path)
    if m:
        if m.group(2).lower() == "r" and m.group(3):  # the /r/ form carries the path
            return Address("sharing", "共有リンク", _file_url(origin, m.group(3)))
        return Address("sharing", "共有リンク", None)

    if any(path_low.endswith(v) for v in _VIEWERS):
        query = dict(urllib.parse.parse_qsl(parts.query))
        doc = query.get("sourcedoc") or query.get("sourceDoc") or ""
        if doc.startswith("/"):  # on-premises SharePoint names the file here; Online uses an id ({GUID})
            return Address("browser", "ブラウザで開いたときの URL", _file_url(origin, urllib.parse.quote(doc)))
        return Address("browser", "ブラウザで開いたときの URL", None)

    if urllib.parse.unquote(path_low).endswith(OFFICE_SUFFIXES):
        return Address("file", "ファイルの URL", _file_url(origin, parts.path, parts.query))
    return None


def _file_url(origin: str, path: str, query: str = "") -> str:
    """The file's own address: the path, with only the query a browser adds left out."""
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True) if k.lower() not in _BROWSER_QUERY]
    return origin + path + (f"?{urllib.parse.urlencode(kept)}" if kept else "")
