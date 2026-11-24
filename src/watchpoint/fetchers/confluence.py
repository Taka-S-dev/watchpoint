"""Confluence pages, from the URL seen in the browser.

Server and Data Center pages are read as people read them: the page itself, kept to its body
(#main-content). Their REST API is never called: on many such servers it is switched off or not
allowed, and a request to it can be logged as a breach of that. Cloud renders its pages in the
browser, so there the page HTML holds no body and the REST API is used, as it is for a source
that is itself a REST URL.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..textify import html_to_text, shape
from .base import Fetched, sha256_file, sha256_text
from .net import http_get

if TYPE_CHECKING:
    from ..config import Target

BODY = "#main-content"  # the page body in the Server / Data Center layout


@dataclass(frozen=True)
class PageRef:
    """Where a page is: its wiki's base URL and the page's id, or its space and title."""

    base: str
    page_id: str = ""
    space: str = ""
    title: str = ""
    cloud: bool = False
    rest: bool = False  # the source is a REST URL

    @property
    def uses_api(self) -> bool:
        return self.cloud or self.rest

    def api_url(self) -> str:
        if not self.page_id:
            raise ValueError(
                "Confluence の API で読むにはページ ID が要ります（/pages/123/ の形の URL にしてください）"
            )
        return f"{self.base}/rest/api/content/{self.page_id}"


def page_ref(source: str) -> PageRef:
    """The page a browser URL (or a REST URL) points at.

    Cloud:   https://x.atlassian.net/wiki/spaces/KEY/pages/123456/Title
    Server:  https://host/confluence/pages/viewpage.action?pageId=123456
             https://host/confluence/display/KEY/Title
    REST:    https://host/rest/api/content/123456
    """
    parsed = urllib.parse.urlsplit(source)
    path = parsed.path
    qs = dict(urllib.parse.parse_qsl(parsed.query))
    cloud = parsed.netloc.lower().endswith(".atlassian.net")
    cut = re.search(r"/(rest/api/content|spaces|pages|display|x)(/|\.|$)", path)
    prefix = path[: cut.start()] if cut else ""
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, prefix.rstrip("/"), "", ""))
    m = re.search(r"/rest/api/content/(\d+)", path)
    if m:
        return PageRef(base, page_id=m.group(1), cloud=cloud, rest=True)
    page_id = qs.get("pageId", "")
    m = re.search(r"/pages/(\d+)", path)
    if m and not page_id:
        page_id = m.group(1)
    if page_id.isdigit():
        return PageRef(base, page_id=page_id, cloud=cloud)
    m = re.search(r"/display/([^/?#]+)/([^/?#]+)", path)
    if m:
        space, title = (urllib.parse.unquote_plus(g) for g in m.groups())
        return PageRef(base, space=space, title=title, cloud=cloud)
    raise ValueError(
        "URL からページが分かりません（pageId=… か /pages/123/ か /display/スペース/タイトル の形にしてください）: "
        + source
    )


def probe(target: Target) -> str | None:
    """A fingerprint taken without fetching: the page version when the API is used, the versions of the
    watched attachments when only those are watched. A page read as HTML has no cheaper fingerprint than
    the page itself, so it is always fetched."""
    ref = page_ref(target.source)
    if target.attachments:
        page_id = _page_id(target, ref)
        return _attachments_probe(_matching(target, _attachments(target, ref.base, page_id)))
    if not ref.uses_api:
        return None
    page = _api_page(target, ref, expand="version")
    return f"version={page['version']['number']}"


def fetch(target: Target, workdir: Path) -> Fetched:
    ref = page_ref(target.source)
    if target.attachments:
        return _fetch_attached(target, ref, workdir)
    return _fetch_api(target, ref, workdir) if ref.uses_api else _fetch_page(target, ref, workdir)


# ---- only the files attached to the page ---------------------------------------------------------------


def _page_id(target: Target, ref: PageRef) -> str:
    """The page's id: in its address, or (for /display/SPACE/Title) on the page itself."""
    if ref.page_id:
        return ref.page_id
    html = http_get(target, target.source.split("#")[0], accept="text/html").text
    page_id = _page_meta(html).get("ajs-page-id", "")
    if not page_id:
        if _is_login_page(html):
            raise PermissionError("Confluence がログインを求めています。auth を設定してください")
        raise ValueError(f"ページの ID が分かりませんでした。pageId=… の形の URL にしてください: {target.source}")
    return page_id


def _matching(target: Target, found: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
    import fnmatch

    patterns = [p.strip().lower() for p in target.attachments or [] if p.strip()]
    return {n: v for n, v in found.items() if any(fnmatch.fnmatchcase(n.lower(), p) for p in patterns)}


def _attachments_probe(files: dict[str, tuple[int, int]]) -> str:
    return "attachments=" + ";".join(f"{n}={v}:{at}" for n, (v, at) in sorted(files.items()))


def _fetch_attached(target: Target, ref: PageRef, workdir: Path) -> Fetched:
    """Each matching attached file, downloaded and read as that file, under a heading naming it.

    The headings of a file's own parts (an Excel sheet) are named after the file too, so rows of two
    files with the same sheet name are never matched against each other.
    """
    from ..textify.base import SECTION_PREFIX
    from .base import textify_copy
    from .download import download

    page_id = _page_id(target, ref)
    files = _matching(target, _attachments(target, ref.base, page_id))
    parts: list[str] = []
    for i, name in enumerate(sorted(files)):
        url = f"{ref.base}/download/attachments/{page_id}/{urllib.parse.quote(name)}"
        one = target.model_copy(update={"source": url, "attachments": None})
        where = workdir / "attachments" / f"{i + 1:03d}"
        where.mkdir(parents=True, exist_ok=True)
        try:
            copy = download(one, where)
            (where / "name.txt").write_text(name, encoding="utf-8")
            lines = textify_copy(copy, one, sha256_file(copy)).splitlines()
        except PermissionError:
            raise  # signed out: every file would fail alike, so the check fails rather than reporting each
        except (OSError, ValueError) as e:
            # one file laid out otherwise (no `key` column) or gone must not stop the others being watched
            lines = [f"{SECTION_PREFIX}{name}", f"読めませんでした（{e}）"]
        if not lines or not lines[0].startswith(SECTION_PREFIX):
            parts.append(f"{SECTION_PREFIX}{name}")
        parts += [
            f"{SECTION_PREFIX}{name} / {ln[len(SECTION_PREFIX) :]}" if ln.startswith(SECTION_PREFIX) else ln
            for ln in lines
        ]
    if not files:
        parts.append(f"（当てはまる添付はありません: {', '.join(target.attachments or [])}）")
    text = "\n".join(parts) + "\n"
    return Fetched(text, sha256_text(text), meta={"title": f"添付 {len(files)} 件"}, probe=_attachments_probe(files))


# ---- the page as people read it (Server / Data Center) ------------------------------------------------


def _fetch_page(target: Target, ref: PageRef, workdir: Path) -> Fetched:
    r = http_get(target, target.source.split("#")[0], accept="text/html")
    html = r.text
    (workdir / "original.html").write_text(html, encoding="utf-8")
    meta = _page_meta(html)
    if not meta.get("ajs-page-id") and _is_login_page(html):
        # some servers answer a page that needs signing in with the sign-in form itself, not a redirect
        raise PermissionError(
            "Confluence がログインを求めています。auth を設定してください"
            if not target.auth
            else "Confluence がログインを求めています。資格情報（auth / secret）を確かめてください"
        )
    picked = target.model_copy(update={"selector": target.selector or BODY})
    try:
        body = html_to_text(html, picked)
    except ValueError:
        raise ValueError(
            f"ページの本文（{picked.selector}）が見つかりません。Confluence のページか、"
            f"selector を確かめてください: {target.source}"
        ) from None
    title = meta.get("ajs-page-title") or _html_title(html)
    rev = _revision(html, meta)
    # The revision is compared with the body: every edit of the page then shows, one that changed no
    # visible text too (formatting, a picture, a macro's settings), and says who made it. A body that
    # changed under the same revision was changed by a macro (a Jira list, an included page), not edited.
    head = [f"# {title}"]
    if rev["version"] or rev["by"] or rev["when"]:
        who = "、".join(v for v in (rev["by"], rev["when"]) if v)
        head.append(f"版: {rev['version'] or '?'}" + (f"（{who}）" if who else ""))
    # A file attached to the page (an Excel shown as a table on it) is replaced without a new page
    # version: the attachments are listed with their own versions, so replacing one is a change too.
    page_id = meta.get("ajs-page-id") or ref.page_id
    if page_id:
        head += _attachment_lines(target, ref.base, page_id)
    text = shape("\n".join(head) + "\n" + body, target, tables=False)
    meta = {"title": title, **rev}
    embedded = _embedded_files(html)
    if embedded:
        meta["embedded"] = embedded
    return Fetched(text, sha256_text(text), meta=meta)


# Macros that show an attached file inside the page (the Office Excel / Word viewers, the file viewer).
# What they show is drawn by the browser from the file, so none of it is in the page's text.
_VIEWER_MACROS = re.compile(r"^(view(xls|doc|ppt|pdf|file|-file)|office-)", re.I)
_FILE_IN = re.compile(r"/download/attachments/\d+/([^/?#\"']+)")
EMBEDDABLE = (".xlsx", ".xlsm", ".xls", ".csv", ".docx", ".doc")


def _embedded_files(html: str) -> list[str]:
    """Names of attached files the page shows through a viewer macro: their contents are not in its text."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one(BODY) or soup
    names: set[str] = set()
    for macro in body.select("[data-macro-name]"):
        if not _VIEWER_MACROS.match(str(macro.get("data-macro-name", ""))):
            continue
        for node in [macro, *macro.find_all(True)]:
            for value in node.attrs.values():
                for v in value if isinstance(value, list) else [value]:
                    m = _FILE_IN.search(str(v))
                    if m:
                        names.add(urllib.parse.unquote(m.group(1)))
            alias = node.get("data-linked-resource-default-alias")
            if alias:
                names.add(str(alias))
    return sorted(n for n in names if n.lower().endswith(EMBEDDABLE))


MAX_ATTACHMENT_PAGES = 20  # pages of the attachment list followed, whatever the server answers


def _attachments(target: Target, base: str, page_id: str) -> dict[str, tuple[int, int]]:
    """The files attached to a page: name -> (version, when that version was uploaded, in ms).

    Read from the page's attachment list (pages/viewpageattachments.action, a page people use, not the
    REST API): every download link there names the file, its version and its upload time
    (/download/attachments/<page>/<name>?version=3&modificationDate=<ms>).
    """
    from bs4 import BeautifulSoup

    found: dict[str, tuple[int, int]] = {}
    url = f"{base}/pages/viewpageattachments.action"
    queue, seen = [{"pageId": page_id}], set()
    while queue and len(seen) < MAX_ATTACHMENT_PAGES:
        params = queue.pop(0)
        key = tuple(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)
        html = http_get(target, url, params=params, accept="text/html").text
        if _is_login_page(html):
            raise PermissionError("Confluence が添付ファイルの一覧でログインを求めています。auth を確かめてください")
        for a in BeautifulSoup(html, "html.parser").select("a[href]"):
            parts = urllib.parse.urlsplit(a["href"])
            q = dict(urllib.parse.parse_qsl(parts.query))
            if f"/download/attachments/{page_id}/" in parts.path:
                name = urllib.parse.unquote(parts.path.rsplit("/", 1)[-1])
                version, at = q.get("version", ""), q.get("modificationDate", "")
                seen_at = (int(version) if version.isdigit() else 0, int(at) if at.isdigit() else 0)
                found[name] = max(found.get(name, (0, 0)), seen_at)
            elif parts.path.endswith("viewpageattachments.action") and q.get("pageId") == page_id and "startIndex" in q:
                queue.append(q)  # the next pages of a long list (not the same list sorted otherwise)
    return found


def _attachment_lines(target: Target, base: str, page_id: str) -> list[str]:
    """One line per file attached to the page: its name, version and when that version was uploaded."""
    try:
        found = _attachments(target, base, page_id)
    except (OSError, ValueError) as e:
        # said in the text rather than failing the check: the page itself was read
        return [f"添付: 一覧を読めませんでした（{e}）"]
    lines = []
    for name in sorted(found):
        version, at = found[name]
        info = [f"版 {version}"] if version else []
        if at:
            info.append(dt.datetime.fromtimestamp(at / 1000).strftime("%Y-%m-%d %H:%M"))
        lines.append(f"添付: {name}" + (f"（{'、'.join(info)}）" if info else ""))
    return lines


def _page_meta(html: str) -> dict[str, str]:
    """Confluence's <meta name="ajs-…" content="…"> values: page id, title, version, space."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    for m in soup.find_all("meta"):
        name = m.get("name") or ""
        if name.startswith("ajs-") and m.get("content") is not None:
            out.setdefault(name, str(m["content"]))
    return out


def _is_login_page(html: str) -> bool:
    return bool(re.search(r"""(id|name)=["']?(loginform|login-container|os_username)\b""", html))


def _html_title(html: str) -> str:
    from bs4 import BeautifulSoup

    t = BeautifulSoup(html, "html.parser").title
    return (t.get_text(strip=True).split(" - ")[0] if t else "") or "（題名なし）"


def _revision(html: str, meta: dict[str, str]) -> dict[str, str | None]:
    """The page's revision as the page shows it: its version, who last changed it and when.

    The version comes from the page's ajs-page-version, else from the link to the latest change
    (diffpagesbyversion.action?…&selectedPageVersions=N), and who and when from the line under the
    title (.page-metadata), whose last modifier is .editor (.author when the creator made the last edit).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    version = meta.get("ajs-page-version") or None
    if not version:
        numbers = [
            int(n)
            for a in soup.select('a[href*="selectedPageVersions"]')
            for n in re.findall(r"selectedPageVersions=(\d+)", a.get("href") or "")
        ]
        version = str(max(numbers)) if numbers else None
    # one at a time: a selector list would take whichever comes first, the creator before the editor
    by = soup.select_one(".page-metadata .editor") or soup.select_one(".page-metadata .author")
    when = soup.select_one(".page-metadata .last-modified")
    return {
        "version": version,
        "by": by.get_text(" ", strip=True) or None if by else None,
        "when": when.get_text(" ", strip=True) or None if when else None,
    }


# ---- the REST API (Cloud, or a REST URL) --------------------------------------------------------------


def _api_page(target: Target, ref: PageRef, expand: str) -> dict:
    url = ref.api_url()
    r = http_get(target, url, params={"expand": expand}, accept="application/json")
    try:
        return r.json()
    except ValueError:
        raise ValueError(
            f"Confluence の API から JSON が返りませんでした（ログインの画面が返った可能性。URL か auth を確かめてください）: {url}"
        ) from None


def _fetch_api(target: Target, ref: PageRef, workdir: Path) -> Fetched:
    page = _api_page(target, ref, expand="body.storage,version")
    (workdir / "original.json").write_text(json.dumps(page, ensure_ascii=False, indent=1), encoding="utf-8")
    body = page.get("body", {}).get("storage", {}).get("value", "")
    title = page.get("title", "")
    text = shape(f"# {title}\n" + html_to_text(f"<body>{body}</body>", target), target, tables=False)
    v = page.get("version", {})
    meta = {
        "title": title,
        "version": v.get("number"),
        "by": (v.get("by") or {}).get("displayName"),
        "when": v.get("when"),
    }
    return Fetched(text, sha256_text(text), meta=meta, probe=f"version={v.get('number')}")
