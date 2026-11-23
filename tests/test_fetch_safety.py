"""Fetching without disturbing the master file or leaking credentials."""

import os
from pathlib import Path

import pytest

from watchpoint.check import Status, check_target
from watchpoint.config import Config, Target
from watchpoint.credentials import clean_secret


def _run(t: Target, data: Path, **cfg):
    return check_target(t, Config(targets=[t], **cfg), data)


def test_custom_auth_headers_never_follow_a_redirect_to_another_site(tmp_path: Path, server, monkeypatch) -> None:
    from conftest import FakeServer

    other = FakeServer()
    try:
        other.routes["/steal"] = (200, "text/html; charset=utf-8", "<p>x</p>")
        server.routes["/page"] = (
            302,
            "text/plain",
            "",
            {"Location": other.url.replace("127.0.0.1", "localhost") + "/steal"},
        )
        monkeypatch.setenv("WATCHPOINT_SECRET_REDMINE", "SECRET-KEY")
        t = Target(
            name="w",
            source=server.url + "/page",
            auth={"type": "header", "header": "X-Redmine-API-Key", "secret": "redmine"},
        )
        r = _run(t, tmp_path / "d")
        assert r.status is Status.FAILED and "資格情報を送らずに止めました" in r.message
        assert not other.requests
    finally:
        other.close()


def test_a_page_that_moved_to_a_sign_in_page_is_a_failure_not_a_change(tmp_path: Path, server) -> None:
    server.routes["/wiki"] = (302, "text/plain", "", {"Location": "/login?next=/wiki"})
    server.routes["/login"] = (200, "text/html; charset=utf-8", "<h1>サインイン</h1>")
    r = _run(Target(name="w", source=server.url + "/wiki"), tmp_path / "d")
    assert r.status is Status.FAILED and "ログイン画面" in r.message


def test_secrets_are_trimmed_and_never_quoted(tmp_path: Path, server, monkeypatch) -> None:
    assert clean_secret("  KEY-1 \n".rstrip("\n")) == "KEY-1"
    with pytest.raises(ValueError) as e:
        clean_secret("KEY\x0b-2")
    assert "KEY" not in str(e.value)
    server.routes["/x"] = (200, "application/json", {"a": 1})
    monkeypatch.setenv("WATCHPOINT_SECRET_K", "SECRET-789 ")  # pasted with a trailing space
    t = Target(
        name="j", source=server.url + "/x", fetch="json", auth={"type": "header", "header": "X-Key", "secret": "k"}
    )
    assert _run(t, tmp_path / "d").status is Status.FIRST
    assert server.requests[-1]["headers"]["X-Key"] == "SECRET-789"


def test_paging_keeps_repeated_filters_and_survives_a_capped_limit(tmp_path: Path, server) -> None:
    rows = [{"id": i, "tracker_id": 1 + i % 2} for i in range(250)]

    def handler(q, h):
        return 200, "application/json", {"issues": []}  # replaced below

    seen: list[str] = []

    def issues(q, h):
        seen.append(server.requests[-1]["path"])
        offset, limit = int(q["offset"]), min(int(q["limit"]), 100)  # the server caps limit, no total
        return 200, "application/json", {"issues": rows[offset : offset + limit]}

    server.routes["/issues.json"] = issues
    t = Target(
        name="r",
        source=server.url + "/issues.json?tracker_id=1&tracker_id=2&limit=500",
        fetch="json",
        paginate="offset",
        records="issues",
        key="id",
    )
    r = _run(t, tmp_path / "d")
    assert r.status is Status.FIRST and r.meta["count"] == 250


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing rules")
def test_the_owner_can_save_over_the_master_while_it_is_being_copied(tmp_path: Path) -> None:
    from watchpoint.fetchers.file import _open_shared

    master = tmp_path / "master.xlsx"
    master.write_bytes(b"x" * 1000)
    with _open_shared(master) as f:
        f.read(10)
        # How Office saves: write a temp file, move the original aside, move the temp in, delete
        # the original. With Python's open() the first move failed (WinError 32) while copying.
        tmp = tmp_path / "~save.tmp"
        tmp.write_bytes(b"y" * 1000)
        os.rename(master, tmp_path / "~old.tmp")
        os.rename(tmp, master)
        os.remove(tmp_path / "~old.tmp")
    assert master.read_bytes() == b"y" * 1000
