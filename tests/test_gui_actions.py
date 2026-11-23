import codecs
import threading
import time

import watchpoint.app as g


def _wait_idle(call) -> None:
    deadline = time.monotonic() + 10
    while call("/api/job")[1]["job"]["active"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_copy_for_ai_uses_saved_state(gui) -> None:
    call, src, _ = gui
    assert call("/api/markdown", {})[1]["count"] == 0
    call.check({})
    src.write_text("# 仕様\n- 保持は 90 日\n", encoding="utf-8")
    call.check({})
    status, md = call("/api/markdown", {})
    assert status == 200
    assert md["count"] == 1
    assert "## 仕様" in md["text"]
    assert "### 読むポイント\n保持の日数を見る" in md["text"]
    assert "+- 保持は 90 日" in md["text"]


def test_the_page_copies_only_what_a_summary_needs(gui) -> None:
    call, src, app = gui
    call.check({})
    src.write_text("# 仕様\n- 保持は 90 日\n", encoding="utf-8")
    call.check({})
    md = call("/api/markdown", {})[1]
    assert str(src) not in md["text"] and str(app.config_path.parent) not in md["text"]  # no paths at all
    assert "watchpoint ack" not in md["text"] and "今回のテキスト" not in md["text"]
    assert md["changed"] == 1 and md["failed"] == 0 and list(md["seen"]) == ["仕様"]
    with_places = call("/api/markdown", {"places": True})[1]
    assert f"- 元の場所: {src}" in with_places["text"]


def test_history_and_log_say_what_started_each_check(gui, tmp_path) -> None:
    from watchpoint.check import check_target
    from watchpoint.config import load_config
    from watchpoint.report import generations
    from watchpoint.store import TargetStore

    call, src, app = gui
    status, _ = call("/api/check", {"names": ["仕様"], "via": "added"})
    assert status == 200
    deadline = time.monotonic() + 30
    while call("/api/job")[1]["job"]["active"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    src.write_text("# 仕様\n- 保持は 90 日\n", encoding="utf-8")
    call.check({"names": ["仕様"]})
    cfg = load_config(app.config_path)
    src.write_text("# 仕様\n- 保持は 120 日\n", encoding="utf-8")
    check_target(cfg.targets[0], cfg, cfg.data_path(app.config_path), via="scheduler")
    store = TargetStore(cfg.data_path(app.config_path), "仕様")
    vias = [g.via for g in generations(store, store.load())]
    assert vias == ["タスク スケジューラ", "画面で押した", "追加したとき"]
    log = call("/api/log")[1]["entries"]
    assert [e.get("via") for e in log[-3:]] == ["added", "page", "scheduler"]
    assert call("/api/check", {"names": ["仕様"], "via": "rm -rf"})[0] == 400


def test_acknowledging_after_the_copy_keeps_a_newer_change(gui) -> None:
    call, src, _ = gui
    call.check({})
    src.write_text("# 仕様\n- 保持は 90 日\n", encoding="utf-8")
    call.check({})
    copied = call("/api/markdown", {})[1]["seen"]
    src.write_text("# 仕様\n- 保持は 120 日\n", encoding="utf-8")
    call.check({})  # a newer change arrives while the person reads the summary
    status, r = call("/api/ack", {"names": ["仕様"], "note": "AI で要約して確認", "seen": copied})
    assert status == 200 and r["stale"] == ["仕様"] and r["done"] == []


def test_a_second_check_is_refused_while_one_runs(gui, monkeypatch) -> None:
    call, _, _ = gui
    gate = threading.Event()
    real = g.check_target

    def slow(*a, **k):
        gate.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(g, "check_target", slow)
    status, job = call("/api/check", {})
    assert status == 200 and job["active"]
    assert call("/api/check", {})[0] == 409
    deadline = time.monotonic() + 5
    while call("/api/overview")[1]["job"]["running"] != ["仕様"]:  # progress names the target being fetched
        assert time.monotonic() < deadline
        time.sleep(0.02)
    gate.set()
    _wait_idle(call)
    assert call("/api/job")[1]["job"]["results"][0]["status"] == "初回"


def test_config_edit_validates_keeps_a_backup_and_detects_conflicts(gui) -> None:
    call, _, app = gui
    status, c = call("/api/config")
    original = c["text"]
    assert status == 200 and "name: 仕様" in original

    bad = original + "  - name: 壊れた\n    sources: x\n"
    v = call("/api/config/validate", {"text": bad})[1]
    assert v["ok"] is False and "壊れた" in v["error"]
    assert call("/api/config/save", {"text": bad, "hash": c["hash"]})[0] == 422
    assert app.config_path.read_text(encoding="utf-8") == original  # nothing written

    draft = original.replace("points: 保持の日数を見る", "points: 保持の日数を見る\n    ignore: ['保持']")
    status, p = call("/api/preview", {"name": "仕様", "text": draft})
    assert status == 200 and p["text"] == "# 仕様"  # the draft is used, not the saved file

    status, saved = call("/api/config/save", {"text": draft, "hash": c["hash"]})
    assert status == 200 and saved["targets"] == ["仕様"]
    assert app.config_path.read_text(encoding="utf-8") == draft
    assert app.config_path.with_name("targets.yaml.bak").read_text(encoding="utf-8") == original
    assert call("/api/config")[1]["text"] == draft

    # Saving over a version the page did not load is refused.
    assert call("/api/config/save", {"text": original, "hash": c["hash"]})[0] == 409
    assert app.config_path.read_text(encoding="utf-8") == draft


def test_config_save_keeps_crlf_and_bom(gui) -> None:
    call, _, app = gui
    lf = app.config_path.read_bytes().replace(b"\r\n", b"\n")
    app.config_path.write_bytes(codecs.BOM_UTF8 + lf.replace(b"\n", b"\r\n"))
    c = call("/api/config")[1]
    assert "\r" not in c["text"] and not c["text"].startswith(codecs.BOM_UTF8.decode())  # what a browser text box holds
    edited = c["text"].replace("保持の日数を見る", "保持の日数と圧縮を見る")
    assert call("/api/config/save", {"text": edited, "hash": c["hash"]})[0] == 200
    saved = app.config_path.read_bytes()
    assert saved.startswith(codecs.BOM_UTF8)
    assert saved.count(b"\r\n") == saved.count(b"\n") > 0
    assert "保持の日数と圧縮を見る" in saved.decode("utf-8-sig")
    assert call("/api/overview")[0] == 200  # the BOM does not break reading the config


def test_ack_several_at_once(gui, tmp_path) -> None:
    call, src, app = gui
    other = tmp_path / "other.md"
    other.write_text("a\n", encoding="utf-8")
    with app.config_path.open("a", encoding="utf-8") as f:
        f.write(f"  - name: 別紙\n    source: '{other}'\n")
    call.check({})
    src.write_text("# 仕様\n- 保持は 1 日\n", encoding="utf-8")
    other.write_text("a\nb\n", encoding="utf-8")
    call.check({})
    assert [t["pending"] for t in call("/api/overview")[1]["targets"]] == [True, True]
    assert call("/api/ack", {"names": ["仕様", "別紙"]})[1]["done"] == ["仕様", "別紙"]
    assert [t["pending"] for t in call("/api/overview")[1]["targets"]] == [False, False]
