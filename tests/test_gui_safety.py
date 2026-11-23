"""What the page and its API must refuse, and acknowledging only what was on screen."""

import threading
from pathlib import Path

import pytest

from watchpoint.check import Status, acknowledge, check_target
from watchpoint.config import Config, Target
from watchpoint.store import TargetStore


def test_ack_refuses_a_copy_nobody_has_seen(tmp_path: Path) -> None:
    from watchpoint.check import Stale

    src = tmp_path / "m.txt"
    src.write_text("a\n", encoding="utf-8")
    t = Target(name="m", source=str(src))
    cfg = Config(targets=[t])
    data = tmp_path / "d"
    check_target(t, cfg, data)
    src.write_text("a\nb\n", encoding="utf-8")
    check_target(t, cfg, data)
    shown = TargetStore(data, t.store_key).load().snapshot
    src.write_text("a\nb\nc 重要\n", encoding="utf-8")
    check_target(t, cfg, data)  # a background check brings a newer copy
    with pytest.raises(Stale):
        acknowledge(t, cfg, data, seen=shown)
    assert check_target(t, cfg, data).status is Status.CHANGED


def test_page_api_rejects_what_it_should(gui, monkeypatch) -> None:
    call, src, app = gui
    import http.client

    port = call.port

    def raw(path: str, length: str) -> int:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest("POST", path, skip_host=True)
        for k, v in {
            "Host": f"127.0.0.1:{port}",
            "X-Watchpoint-Token": app.token,
            "Content-Type": "application/json",
            "Content-Length": length,
        }.items():
            c.putheader(k, v)
        c.endheaders(b"{}")
        status = c.getresponse().status
        c.close()
        return status

    assert raw("/api/markdown", "-1") == 400
    assert call("/api/check", {"names": []})[0] == 400  # an empty list is not "everything"
    assert call("/api/markdown", {"names": []})[1]["count"] == 0
    data = app.config_path.parent / "data"
    data.mkdir(exist_ok=True)
    (data / "log.jsonl").write_bytes(b'{"name": "a"}\n\xff\xfe broken\n')
    assert call("/api/log")[0] == 200


def test_opening_a_copy_never_runs_a_program(gui, tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from watchpoint import gui as gui_mod

    call, _, app = gui
    tool = tmp_path / "tool.bat"
    tool.write_text("echo hi\n", encoding="utf-8")
    app.config_path.write_text(
        f"keep_originals: true\ntargets:\n  - name: tool\n    source: '{tool}'\n", encoding="utf-8"
    )
    call.check({"names": ["tool"]})
    started, shown = [], []
    monkeypatch.setattr(gui_mod.os, "startfile", started.append, raising=False)
    monkeypatch.setattr(gui_mod.os, "name", "nt")
    monkeypatch.setattr(subprocess, "Popen", lambda args, *a, **k: shown.append(args))
    status, body = call("/api/open", {"name": "tool"})
    assert status == 200 and body["shown_in_folder"] and not started and shown[0][0] == "explorer"


def test_two_saves_from_the_same_page_version_cannot_both_win(gui) -> None:
    call, _, _ = gui
    c = call("/api/config")[1]
    results: list[int] = []

    def save(i: int) -> None:
        results.append(call("/api/config/save", {"text": c["text"] + f"# {i}\n", "hash": c["hash"]})[0])

    threads = [threading.Thread(target=save, args=(i,)) for i in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == [200, 409, 409, 409]
