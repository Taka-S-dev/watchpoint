"""Web pages, JSON APIs, Confluence and Office, against a local fake server."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from watchpoint.check import Status, check_target
from watchpoint.cli import app
from watchpoint.config import Config, Target
from watchpoint.fetchers.confluence import page_ref

runner = CliRunner()


def _run(t: Target, data: Path, **cfg):
    return check_target(t, Config(targets=[t], **cfg), data)


PAGE = """<html><head><script>var x=1</script><style>p{}</style></head><body>
<nav>メニュー</nav>
<div id="main"><h2>お知らせ</h2><p>メンテナンスは <b>12/1</b> です。</p>
<ul><li>A 機能</li><li>B 機能</li></ul>
<table><tr><th>版</th><th>日付</th></tr><tr><td>1.2</td><td>11/20</td></tr></table>
<span class="views">閲覧 {views}</span></div></body></html>"""


def test_web_page_with_selector_and_removed_counter(tmp_path: Path, server) -> None:
    views = {"n": 1}
    server.routes["/news"] = lambda q, h: (200, "text/html; charset=utf-8", PAGE.replace("{views}", str(views["n"])))
    t = Target(name="w", source=server.url + "/news", selector="#main", remove=[".views"])
    data = tmp_path / "data"
    r = _run(t, data)
    text = (r.snapshot / "text.txt").read_text(encoding="utf-8")
    assert text.splitlines() == [
        "## お知らせ",
        "メンテナンスは 12/1 です。",
        "- A 機能",
        "- B 機能",
        "| 版 | 日付 |",
        "| 1.2 | 11/20 |",
    ]
    views["n"] = 99  # the view counter changes on every load
    assert _run(t, data).status is Status.UNCHANGED


def test_web_page_errors_are_explained(tmp_path: Path, server) -> None:
    server.routes["/secret"] = (401, "text/html", "no")
    r = _run(Target(name="w", source=server.url + "/secret"), tmp_path / "data")
    assert r.status is Status.FAILED and "401" in r.message and "auth" in r.message
    r = _run(Target(name="w2", source=server.url + "/news", selector="#none"), tmp_path / "data")
    assert r.status is Status.FAILED and "404" in r.message


def test_redmine_style_json_with_pagination_and_api_key(tmp_path: Path, server, monkeypatch) -> None:
    monkeypatch.setenv("WATCHPOINT_SECRET_REDMINE", "k123")
    issues = [
        {"id": i, "subject": f"件名{i}", "status": {"id": 1, "name": "新規"}, "updated_on": "2026-11-20"}
        for i in range(1, 6)
    ]

    def handler(q, h):
        if h.get("X-Redmine-API-Key") != "k123":
            return 401, "application/json", {"errors": ["auth"]}
        off, lim = int(q.get("offset", 0)), int(q.get("limit", 25))
        return (
            200,
            "application/json",
            {"issues": issues[off : off + lim], "total_count": len(issues), "offset": off, "limit": lim},
        )

    server.routes["/issues.json"] = handler
    t = Target(
        name="r",
        source=server.url + "/issues.json?assigned_to_id=me&limit=2",
        records="issues",
        paginate="offset",
        key="id",
        columns=["subject", "status.name"],
        auth={"type": "header", "header": "X-Redmine-API-Key", "secret": "redmine"},
    )
    data = tmp_path / "data"
    r = _run(t, data)
    assert r.status is Status.FIRST
    assert len([q for q in server.requests if q["path"] == "/issues.json"]) == 3  # 2 + 2 + 1
    issues[2]["status"]["name"] = "解決"
    issues.append({"id": 6, "subject": "件名6", "status": {"id": 1, "name": "新規"}, "updated_on": "2026-11-22"})
    r = _run(t, data)
    assert "変更 [id=3] status.name: 新規 → 解決" in r.diff
    assert "追加 [id=6] subject=件名6 | status.name=新規" in r.diff


CONF_PAGE = """<html><head><title>設計 - 開発 - Confluence</title>
<meta name="ajs-page-id" content="123"><meta name="ajs-page-title" content="設計">
<meta name="ajs-page-version" content="{version}"><meta name="ajs-space-key" content="DEV"></head>
<body><div id="header"><nav>スペース一覧 ダッシュボード</nav></div>
<div id="content"><div class="page-metadata"><span class="author">田中</span> が最終更新</div>
<div id="main-content" class="wiki-content"><h2>ログ</h2><p>保持は{days}日</p>
<table><tr><th>項目</th><th>値</th></tr><tr><td>形式</td><td>JSON</td></tr></table></div>
<div id="likes-and-labels-container">いいね {likes}</div></div></body></html>"""


def test_a_confluence_server_page_is_read_as_people_read_it_and_its_api_never_called(
    tmp_path: Path, server, monkeypatch
) -> None:
    """On Server / Data Center the REST API is often switched off or not to be used."""
    monkeypatch.setenv("WATCHPOINT_SECRET_CONF", "pat")
    page = {"version": 3, "days": 30, "likes": 1}

    def handler(q, h):
        assert h.get("Authorization") == "Bearer pat"
        return 200, "text/html; charset=utf-8", CONF_PAGE.format(**page)

    server.routes["/confluence/pages/viewpage.action"] = handler
    server.routes["/confluence/pages/viewpageattachments.action"] = (200, "text/html; charset=utf-8", "<p>添付なし</p>")
    t = Target(
        name="c",
        source=server.url + "/confluence/pages/viewpage.action?pageId=123",
        auth={"type": "bearer", "secret": "conf"},
    )
    assert t.kind == "confluence"
    data = tmp_path / "data"
    r = _run(t, data)
    assert (r.snapshot / "text.txt").read_text(encoding="utf-8").splitlines() == [
        "# 設計",
        "版: 3（田中）",
        "## ログ",
        "保持は30日",
        "| 項目 | 値 |",
        "| 形式 | JSON |",
    ]
    page["likes"] = 5  # outside the page body: not a change
    assert _run(t, data).status is Status.UNCHANGED
    page.update(version=4, days=60)
    r = _run(t, data)
    assert r.status is Status.CHANGED and "版 4、田中" in r.message and "+保持は60日" in r.diff
    assert "+版: 4（田中）" in r.diff  # the diff says a new revision was made, and by whom
    page["version"] = 5  # an edit that changed no visible text (formatting, a picture) is still an edit
    r = _run(t, data)
    assert r.status is Status.CHANGED and "+版: 5（田中）" in r.diff
    assert not [q for q in server.requests if "/rest/" in q["path"]]


def test_a_confluence_revision_is_read_from_the_page_when_it_has_no_version_meta() -> None:
    from watchpoint.fetchers.confluence import _revision

    html = """<div class="page-metadata"><span class="author">鈴木</span> が作成、
    <span class="editor">田中</span> が <a class="last-modified"
    href="/pages/diffpagesbyversion.action?pageId=9&selectedPageVersions=6&selectedPageVersions=7">11月 20, 2026</a> に最終更新</div>"""
    assert _revision(html, {}) == {"version": "7", "by": "田中", "when": "11月 20, 2026"}
    assert _revision("<p>no metadata</p>", {}) == {"version": None, "by": None, "when": None}


ATTACHMENTS = """<table class="attachments">
<tr><td><a href="/confluence/download/attachments/123/%E8%B3%AA%E5%95%8F%E8%A1%A8.xlsx?version={v}&modificationDate={ms}&api=v2">質問表.xlsx</a></td></tr>
<tr><td><a href="/confluence/download/attachments/999/other.pdf?version=9">another page's file</a></td></tr>
</table>{more}"""


def test_a_replaced_confluence_attachment_is_a_change_though_the_page_version_is_not(tmp_path: Path, server) -> None:
    """An Excel attached to a page and shown on it as a table is replaced without a new page version."""
    att = {"v": 1, "ms": 1795000000000}
    server.routes["/confluence/pages/viewpage.action"] = (
        200,
        "text/html; charset=utf-8",
        CONF_PAGE.format(version=3, days=30, likes=0),
    )

    def attachments(q, h):
        if q.get("startIndex") == "20":  # the second page of a long list
            return (
                200,
                "text/html; charset=utf-8",
                (
                    '<a href="/confluence/download/attachments/123/b.csv?version=2&modificationDate=1795000000000">b.csv</a>'
                ),
            )
        more = '<a href="/confluence/pages/viewpageattachments.action?pageId=123&startIndex=20">次へ</a>'
        return 200, "text/html; charset=utf-8", ATTACHMENTS.format(more=more, **att)

    server.routes["/confluence/pages/viewpageattachments.action"] = attachments
    t = Target(name="c", source=server.url + "/confluence/pages/viewpage.action?pageId=123")
    data = tmp_path / "data"
    r = _run(t, data)
    lines = (r.snapshot / "text.txt").read_text(encoding="utf-8").splitlines()
    stamp = __import__("datetime").datetime.fromtimestamp(1795000000).strftime("%Y-%m-%d %H:%M")
    assert lines[2:4] == [f"添付: b.csv（版 2、{stamp}）", f"添付: 質問表.xlsx（版 1、{stamp}）"]
    att.update(v=2, ms=1795000600000)
    r = _run(t, data)
    assert r.status is Status.CHANGED and "+添付: 質問表.xlsx（版 2、" in r.diff and "版: 3" not in r.diff
    assert not [q for q in server.requests if "/rest/" in q["path"]]


def test_a_confluence_display_url_is_read_too(tmp_path: Path, server) -> None:
    server.routes["/confluence/display/DEV/Design"] = lambda q, h: (
        200,
        "text/html; charset=utf-8",
        CONF_PAGE.format(version=1, days=30, likes=0),
    )
    t = Target(name="c", source=server.url + "/confluence/display/DEV/Design")
    assert t.kind == "confluence"
    r = _run(t, tmp_path / "data")
    assert r.status is Status.FIRST and "保持は30日" in (r.snapshot / "text.txt").read_text(encoding="utf-8")


def test_a_confluence_sign_in_form_is_not_compared_as_the_page(tmp_path: Path, server) -> None:
    form = '<html><body><form id="loginform" action="/dologin.action"><input name="os_username"></form></body></html>'
    server.routes["/confluence/pages/viewpage.action"] = (200, "text/html; charset=utf-8", form)
    t = Target(name="c", source=server.url + "/confluence/pages/viewpage.action?pageId=123")
    r = _run(t, tmp_path / "data")
    assert r.status is Status.FAILED and "ログイン" in r.message and "auth" in r.message


def test_confluence_rest_url_uses_the_api_with_a_version_probe(tmp_path: Path, server, monkeypatch) -> None:
    monkeypatch.setenv("WATCHPOINT_SECRET_CONF", "pat")
    page = {
        "title": "設計",
        "version": {"number": 3, "by": {"displayName": "田中"}, "when": "2026-11-20T10:00"},
        "body": {"storage": {"value": "<h1>ログ</h1><p>保持は30日</p>"}},
    }

    def handler(q, h):
        assert h.get("Authorization") == "Bearer pat"
        if q.get("expand") == "version":
            return 200, "application/json", {"version": page["version"]}
        return 200, "application/json", page

    server.routes["/confluence/rest/api/content/123"] = handler
    t = Target(
        name="c",
        source=server.url + "/confluence/rest/api/content/123",
        fetch="confluence",
        auth={"type": "bearer", "secret": "conf"},
    )
    data = tmp_path / "data"
    _run(t, data)
    before = len(server.requests)
    assert _run(t, data).status is Status.UNCHANGED
    assert [r["query"].get("expand") for r in server.requests[before:]] == ["version"]  # body not fetched

    page["version"] = {"number": 4, "by": {"displayName": "鈴木"}, "when": "2026-11-22T09:00"}
    page["body"]["storage"]["value"] = "<h1>ログ</h1><p>保持は60日</p>"
    r = _run(t, data)
    assert r.status is Status.CHANGED and "版 4、鈴木" in r.message and "+保持は60日" in r.diff
    assert r.diff.splitlines()[2].endswith("ログ")  # hunk names its heading


def _book(rows: list[list]) -> bytes:
    import io

    from openpyxl import Workbook

    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_an_attached_excel_is_downloaded_and_compared_by_its_rows(tmp_path: Path, server) -> None:
    book = {"rows": [["No.", "状態"], [1, "未回答"], [2, "未回答"]]}
    server.routes["/confluence/download/attachments/123/qa.xlsx"] = lambda q, h: (200, XLSX, _book(book["rows"]))
    # a link copied from the page names the version shown then: the latest is fetched all the same
    t = Target(
        name="a",
        source=server.url + "/confluence/download/attachments/123/qa.xlsx?version=1&modificationDate=17&api=v2",
        key="No.",
    )
    assert t.kind == "download"
    data = tmp_path / "data"
    assert _run(t, data).status is Status.FIRST
    book["rows"][2][1] = "回答済"
    r = _run(t, data)
    assert r.status is Status.CHANGED and r.entries[0]["cells"] == [["状態", "未回答", "回答済"]]
    assert all(not q["query"] for q in server.requests)  # version, modificationDate and api left out


def test_targets_reading_one_file_download_it_once_per_check(tmp_path: Path, server) -> None:
    """Column B in one target and column G in another: one check downloads the workbook once."""
    from watchpoint.check import check_all

    server.routes["/files/qa.xlsx"] = lambda q, h: (200, XLSX, _book([["No.", "質問", "回答"], [1, "a", "b"]]))
    url = server.url + "/files/qa.xlsx"
    targets = [Target(name="b", source=url, cells=["B1:B9"]), Target(name="c", source=url, cells=["C1:C9"])]
    results = check_all(targets, Config(targets=targets, workers=2), tmp_path / "data")
    assert [r.status for r in results] == [Status.FIRST, Status.FIRST]
    assert [q["path"] for q in server.requests].count("/files/qa.xlsx") == 1
    # outside a check (trying one target), every fetch is its own
    _run(targets[0], tmp_path / "other")
    assert [q["path"] for q in server.requests].count("/files/qa.xlsx") == 2


def test_a_failed_shared_fetch_fails_every_target_without_asking_again(tmp_path: Path, server) -> None:
    from watchpoint.check import check_all

    server.routes["/files/qa.xlsx"] = lambda q, h: (200, "text/html", b"<form id='loginform'></form>")
    url = server.url + "/files/qa.xlsx"
    targets = [Target(name="b", source=url, fail_after=1), Target(name="c", source=url, fail_after=1)]
    results = check_all(targets, Config(targets=targets, workers=2), tmp_path / "data")
    assert [r.status for r in results] == [Status.FAILED, Status.FAILED]
    assert all("Web ページが返りました" in r.message for r in results)
    assert [q["path"] for q in server.requests].count("/files/qa.xlsx") == 1


def test_only_the_excel_files_attached_to_a_confluence_page_are_watched(tmp_path: Path, server) -> None:
    """attachments: ['*.xlsx'] watches the page's Excel files by their rows, not the page itself."""
    files = {
        "qa.xlsx": {"v": 1, "rows": [["No.", "状態"], [1, "未回答"], [2, "未回答"]]},
        "plan.XLSX": {"v": 4, "rows": [["No.", "状態"], [1, "済"]]},
    }
    listing = lambda q, h: (  # noqa: E731
        200,
        "text/html; charset=utf-8",
        "".join(
            f'<a href="/wiki/download/attachments/123/{n}?version={f["v"]}&modificationDate=17">{n}</a>'
            for n, f in files.items()
        )
        + '<a href="/wiki/download/attachments/123/memo.docx?version=2">memo.docx</a>',
    )
    server.routes["/wiki/pages/viewpageattachments.action"] = listing
    for name in files:
        server.routes[f"/wiki/download/attachments/123/{name}"] = lambda q, h, n=name: (
            200,
            XLSX,
            _book(files[n]["rows"]),
        )
    server.routes["/wiki/pages/viewpage.action"] = lambda q, h: pytest.fail("the page itself is not read")
    t = Target(
        name="c",
        source=server.url + "/wiki/pages/viewpage.action?pageId=123",
        attachments=["*.xlsx"],
        key="No.",
    )
    data = tmp_path / "data"
    r = _run(t, data)
    text = (r.snapshot / "text.txt").read_text(encoding="utf-8")
    assert "## plan.XLSX / シート: Sheet" in text and "## qa.xlsx / シート: Sheet" in text and "memo" not in text

    before = len(server.requests)
    assert _run(t, data).status is Status.UNCHANGED
    assert [q["path"] for q in server.requests[before:]] == ["/wiki/pages/viewpageattachments.action"]  # no download

    files["qa.xlsx"].update(v=2)
    files["qa.xlsx"]["rows"][2][1] = "回答済"
    r = _run(t, data)
    assert r.status is Status.CHANGED
    assert r.entries == [
        {
            "kind": "changed",
            "key": "No.=2",
            "cells": [["状態", "未回答", "回答済"]],
            "row": [["状態", "回答済"]],
            "section": "qa.xlsx / シート: Sheet",
        }
    ]


def test_attachments_are_for_a_confluence_server_page() -> None:
    with pytest.raises(ValueError, match="attachments"):
        Target(name="a", source="https://h/news.html", attachments=["*.xlsx"])
    with pytest.raises(ValueError, match="attachments"):
        Target(name="a", source="https://x.atlassian.net/wiki/spaces/K/pages/1/T", attachments=["*.xlsx"])


def test_a_page_answered_for_a_file_is_not_compared(tmp_path: Path, server) -> None:
    server.routes["/files/qa.xlsx"] = (200, "text/html; charset=utf-8", "<form id='loginform'></form>")
    r = _run(Target(name="a", source=server.url + "/files/qa.xlsx"), tmp_path / "data")
    assert r.status is Status.FAILED and "ログイン" in r.message


def test_web_addresses_of_files_are_downloads() -> None:
    from watchpoint.fetchers.download import latest_url

    assert Target(name="a", source="https://h/files/%E8%A1%A8.xlsx").kind == "download"
    assert Target(name="a", source="https://h/files/report.docx?x=1").kind == "download"
    assert Target(name="a", source="https://h/download/attachments/1/image.png").kind == "download"
    assert Target(name="a", source="https://h/news.html").kind == "web"
    assert Target(name="a", source="https://x.sharepoint.com/sites/a/b.xlsx").kind == "office"
    assert (
        latest_url("https://h/download/attachments/1/a.xlsx?version=3&modificationDate=9&api=v2&preview=x#top")
        == "https://h/download/attachments/1/a.xlsx?preview=x"
    )
    assert latest_url("https://h/files/a.xlsx?version=3") == "https://h/files/a.xlsx?version=3"  # not Confluence's


def test_confluence_urls() -> None:
    cloud = page_ref("https://x.atlassian.net/wiki/spaces/K/pages/123/Title")
    assert cloud.uses_api and cloud.api_url() == "https://x.atlassian.net/wiki/rest/api/content/123"
    server_page = page_ref("https://h/confluence/pages/viewpage.action?pageId=9")
    assert not server_page.uses_api and (server_page.base, server_page.page_id) == ("https://h/confluence", "9")
    rest = page_ref("https://h/rest/api/content/5")
    assert rest.uses_api and rest.api_url() == "https://h/rest/api/content/5"
    display = page_ref("https://h/confluence/display/KEY/%E8%A8%AD%E8%A8%88+%E6%9B%B8")
    assert not display.uses_api and (display.base, display.space, display.title) == (
        "https://h/confluence",
        "KEY",
        "設計 書",
    )
    with pytest.raises(ValueError):
        page_ref("https://h/confluence/dashboard.action")


def test_notify_drops_one_file_per_new_change(tmp_path: Path) -> None:
    import yaml

    src = tmp_path / "a.md"
    src.write_text("1\n", encoding="utf-8")
    drop = tmp_path / "drop"
    cfg = tmp_path / "t.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"notify": {"drop_folder": str(drop)}, "targets": [{"name": "A", "source": str(src)}]}, allow_unicode=True
        ),
        encoding="utf-8",
    )
    c = ["--config", str(cfg)]
    runner.invoke(app, ["check", *c, "--notify"])
    src.write_text("2\n", encoding="utf-8")
    runner.invoke(app, ["check", *c, "--notify"])
    runner.invoke(app, ["check", *c, "--notify"])  # same unacknowledged change: no second notification
    files = sorted(drop.glob("*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert (
        body["v"] == 1
        and body["type"] == "notify"
        and body["source"] == "watchpoint"
        and body["text"].startswith("A: ")
    )
    assert not list(drop.glob(".*"))


def _excel_available() -> bool:
    try:
        import winreg

        import win32com.client  # noqa: F401

        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Excel.Application")
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _excel_available(), reason="Excel is not installed")
def test_office_fetch_saves_a_copy_and_leaves_the_source_alone(tmp_path: Path) -> None:
    from openpyxl import Workbook

    src = tmp_path / "q.xlsx"
    wb = Workbook()
    wb.active.append(["No.", "質問"])
    wb.active.append([1, "保存先は？"])
    wb.save(src)
    before = (src.read_bytes(), src.stat().st_mtime_ns)
    t = Target(name="o", source=str(src), fetch="office", key="No.")
    r = _run(t, tmp_path / "data")
    assert r.status is Status.FIRST, r.message
    assert "[No.=1] 質問=保存先は？" in (r.snapshot / "text.txt").read_text(encoding="utf-8")
    assert (src.read_bytes(), src.stat().st_mtime_ns) == before
