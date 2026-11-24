import http.client


def test_page_carries_the_token_and_api_needs_it(gui) -> None:
    call, _, app = gui
    # Knowing the port is not enough: the page (which holds the key) needs the URL from the terminal.
    status, page = call("/", token=None)
    assert status == 403 and app.token not in page
    assert call("/?k=wrong", token=None)[0] == 403
    port = int(call.port)
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("GET", f"/?k={app.token}", headers={"Host": f"127.0.0.1:{port}"})
    res = conn.getresponse()
    res.read()
    cookie = res.getheader("Set-Cookie")
    assert res.status == 303 and res.getheader("Location") == "/"
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    conn.request("GET", "/", headers={"Host": f"127.0.0.1:{port}", "Cookie": cookie.split(";")[0]})
    res = conn.getresponse()
    page = res.read().decode()
    conn.close()
    assert res.status == 200 and app.token in page and "__WATCHPOINT_TOKEN__" not in page

    assert call("/api/overview", token=None)[0] == 403
    assert call("/api/overview", token="wrong")[0] == 403
    # Another host name pointing at this port (DNS rebinding) is refused, even with the token.
    assert call("/api/overview", host="evil.example:80")[0] == 403
    assert call("/api/ack", raw=b"{}")[0] == 415


def test_check_ack_and_history_through_the_page(gui) -> None:
    call, src, _ = gui
    status, ov = call("/api/overview")
    assert status == 200
    assert [t["name"] for t in ov["targets"]] == ["仕様"]
    assert ov["targets"][0]["status"] == "未チェック"

    r = call.check({})
    assert r["results"][0]["status"] == "初回"
    assert (r["total"], r["running"], r["error"]) == (1, [], None)

    src.write_text("# 仕様\n- 保持は 60 日（変更）\n", encoding="utf-8")
    r = call.check({"names": ["仕様"]})
    assert r["changed"] == 1

    status, d = call("/api/target?name=%E4%BB%95%E6%A7%98")
    assert status == 200
    assert d["pending"] is True
    assert d["points"] == "保持の日数を見る"
    assert d["changes"]["pending"] is True
    assert "+- 保持は 60 日（変更）" in d["changes"]["text"]
    assert any("[30→60]" in line for line in d["changes"]["inline"])
    assert [g["marks"] for g in d["history"]] == [["最新"], ["確認済み"]]

    status, r = call("/api/ack", {"names": ["仕様"], "note": "佐藤さんに確認済み"})
    assert r["done"] == ["仕様"]
    status, d = call("/api/target?name=%E4%BB%95%E6%A7%98")
    assert d["pending"] is False
    assert d["changes"]["pending"] is False
    assert d["history"][0]["note"] == "佐藤さんに確認済み"

    status, ch = call("/api/diff?name=%E4%BB%95%E6%A7%98&from=2&to=1")
    assert status == 200
    assert "+- 保持は 60 日（変更）" in ch["text"]
    status, t = call("/api/text?name=%E4%BB%95%E6%A7%98&gen=2")
    assert t["text"] == "# 仕様\n- 保持は 30 日\n"
    assert call("/api/text?name=%E4%BB%95%E6%A7%98&gen=9")[0] == 400

    status, log = call("/api/log")
    assert [e["status"] for e in log["entries"]] == ["初回", "変更あり", "確認済み"]


def test_preview_does_not_save_and_errors_are_messages(gui) -> None:
    call, src, app = gui
    status, p = call("/api/preview", {"name": "仕様"})
    assert status == 200
    assert p["text"].startswith("# 仕様")
    assert call("/api/overview")[1]["targets"][0]["status"] == "未チェック"

    assert call("/api/target?name=nope")[0] == 404
    assert call("/api/target")[0] == 400
    assert call("/api/check", {"names": "仕様"})[0] == 400

    src.unlink()
    status, err = call("/api/preview", {"name": "仕様"})
    assert status == 502
    assert "取得できませんでした" in err["error"]


def test_config_errors_reach_the_page(gui) -> None:
    call, _, app = gui
    app.config_path.write_text("targets: [{name: x}]\n", encoding="utf-8")
    status, err = call("/api/overview")
    assert status == 422
    assert err["error"]


def test_gui_command_prints_a_url_that_opens_the_page(tmp_path, monkeypatch) -> None:
    import re

    from typer.testing import CliRunner

    from watchpoint import gui as gui_mod
    from watchpoint.cli import app

    cfg = tmp_path / "t.yaml"
    cfg.write_text("targets: []\n", encoding="utf-8")
    monkeypatch.setattr(gui_mod.ThreadingHTTPServer, "serve_forever", lambda self: None)
    out = CliRunner().invoke(app, ["gui", "-c", str(cfg), "--no-browser"]).output
    assert re.search(r"http://127\.0\.0\.1:\d+/\?k=[\w-]{20,}", out)


def test_a_check_names_its_targets_so_the_page_can_say_which_wait(gui) -> None:
    call, _src, _app = gui
    job = call.check({"names": ["仕様"]})
    assert job["names"] == ["仕様"] and job["total"] == 1
