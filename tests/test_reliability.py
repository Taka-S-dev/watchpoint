"""The promise watchpoint keeps: a change that was not acknowledged, its history and its notice are never lost
by how checks, views and settings happen to be combined."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import watchpoint.check as chk
import watchpoint.notify as notify
from watchpoint.check import Status, check_target, send_notifications
from watchpoint.config import Config, load_config
from watchpoint.store import TargetStore


@pytest.fixture
def clock(monkeypatch):
    """Each check runs 40 minutes after the previous one, so every copy gets its own generation."""
    now = [dt.datetime(2026, 11, 23, 9, 0)]

    def tick() -> dt.datetime:
        now[0] += dt.timedelta(minutes=40)
        return now[0]

    monkeypatch.setattr(chk, "_now", lambda: now[0])
    return tick


def _config(tmp_path: Path, body: str, **notify_opts) -> Config:
    opts = ", ".join(f"{k}: {str(v).lower()}" for k, v in notify_opts.items())
    drop = (tmp_path / "drop").as_posix()
    text = f"notify: {{drop_folder: '{drop}'{', ' + opts if opts else ''}}}\ntargets:\n{body}"
    return load_config(tmp_path / "targets.yaml", text)


def _drops(tmp_path: Path) -> list[str]:
    folder = tmp_path / "drop"
    return sorted(p.read_text(encoding="utf-8") for p in folder.glob("*.json")) if folder.exists() else []


@pytest.fixture
def unreachable_drop(monkeypatch):
    """The drop folder cannot be written (a share that is down, a full disk) until `.restore()`."""

    class Switch:
        def __init__(self) -> None:
            self.real = notify.drop
            monkeypatch.setattr(notify, "drop", self._fail)

        @staticmethod
        def _fail(*a, **k):
            raise OSError("drop folder unreachable")

        def restore(self) -> None:
            monkeypatch.setattr(notify, "drop", self.real)

    return Switch


# --------------------------------------------------------------------------- notices are kept until written


def test_a_rule_warning_found_by_a_check_that_does_not_notify_is_still_notified(tmp_path, clock) -> None:
    src = tmp_path / "q.csv"
    src.write_text("No.,状態\n1,保留\n", encoding="utf-8")
    cfg = _config(tmp_path, f"  - name: q\n    source: '{src.as_posix()}'\n    key: No.\n"
                            "    rules: [{field: 状態, one_of: [未回答, 回答済]}]\n")  # fmt: skip
    data = tmp_path / "data"
    clock()
    check_target(cfg.targets[0], cfg, data)  # pressed on the page: nothing is sent
    clock()
    r = check_target(cfg.targets[0], cfg, data)  # the scheduled check that notifies
    assert send_notifications([r], cfg, data) == 1 and "保留" in _drops(tmp_path)[0]
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert r.alerts and send_notifications([r], cfg, data) == 0  # told once
    src.write_text("No.,状態\n1,未回答\n", encoding="utf-8")
    clock()
    check_target(cfg.targets[0], cfg, data)
    src.write_text("No.,状態\n1,保留\n", encoding="utf-8")
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    before = sum("要注意" in d for d in _drops(tmp_path))
    send_notifications([r], cfg, data)  # (the row going back to 保留 is also told, as a change undone)
    assert sum("要注意" in d for d in _drops(tmp_path)) == before + 1  # it cleared and came back: told again


def test_a_failure_notice_that_could_not_be_written_is_sent_by_the_next_check(
    tmp_path, clock, unreachable_drop
) -> None:
    cfg = _config(tmp_path, f"  - name: gone\n    source: '{(tmp_path / 'none.txt').as_posix()}'\n    fail_after: 1\n",
                  on_failure=True)  # fmt: skip
    data = tmp_path / "data"
    drop = unreachable_drop()
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert r.new_since_notified and send_notifications([r], cfg, data) == 0
    drop.restore()
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert send_notifications([r], cfg, data) == 1 and "取得できませんでした" in _drops(tmp_path)[0]
    clock()
    assert not check_target(cfg.targets[0], cfg, data).new_since_notified  # once per run of failures


def test_a_change_notice_that_could_not_be_written_is_sent_by_the_next_check(tmp_path, clock, unreachable_drop) -> None:
    src = tmp_path / "a.txt"
    src.write_text("one\n", encoding="utf-8")
    cfg = _config(tmp_path, f"  - name: a\n    source: '{src.as_posix()}'\n")
    data = tmp_path / "data"
    clock()
    check_target(cfg.targets[0], cfg, data)
    src.write_text("two\n", encoding="utf-8")
    drop = unreachable_drop()
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert r.status is Status.CHANGED and send_notifications([r], cfg, data) == 0
    drop.restore()
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert send_notifications([r], cfg, data) == 1 and "+two" in _drops(tmp_path)[0]
    log = (data / "log.jsonl").read_text(encoding="utf-8")
    assert "通知できず" in log  # the failed write is on record


def test_the_warning_about_set_aside_changes_waits_for_a_writable_drop_folder(
    tmp_path, clock, unreachable_drop
) -> None:
    src = tmp_path / "a.txt"
    src.write_text("one\n", encoding="utf-8")
    body = f"  - name: a\n    source: '{src.as_posix()}'\n"
    cfg = _config(tmp_path, body)
    data = tmp_path / "data"
    for text in ("one\n", "two\n"):
        src.write_text(text, encoding="utf-8")
        clock()
        check_target(cfg.targets[0], cfg, data)
    cfg = _config(tmp_path, body + "    normalize: [case]\n")  # changes how the text is made
    drop = unreachable_drop()
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert r.warning and r.notice and send_notifications([r], cfg, data) == 0
    drop.restore()
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert send_notifications([r], cfg, data) == 1 and "未確認の変更" in _drops(tmp_path)[0]
    clock()
    assert not check_target(cfg.targets[0], cfg, data).notice
    assert TargetStore(data, "a").load().notice is None


# --------------------------------------------------------------------------- unacknowledged changes are kept


def test_changes_set_aside_by_a_settings_change_survive_pruning_until_acknowledged(tmp_path, clock) -> None:
    from watchpoint.check import acknowledge
    from watchpoint.report import latest_changes, saved_results

    src = tmp_path / "a.txt"
    head = f"keep: 1\nnotify: {{drop_folder: '{(tmp_path / 'drop').as_posix()}'}}\ntargets:\n"
    item = f"  - name: a\n    source: '{src.as_posix()}'\n"
    cfg = load_config(tmp_path / "t.yaml", head + item)
    data = tmp_path / "data"
    for text in ("one\n", "two\n"):
        src.write_text(text, encoding="utf-8")
        clock()
        check_target(cfg.targets[0], cfg, data)
    store = TargetStore(data, "a")
    before = store.load()
    pair = (before.baseline, before.snapshot)
    cfg = load_config(tmp_path / "t.yaml", head + item + "    normalize: [case]\n")
    for _ in range(3):  # later checks prune down to keep: 1
        clock()
        r = check_target(cfg.targets[0], cfg, data)
    assert all(store.snapshot_dir(s).exists() for s in pair)  # neither end of the unseen change is gone
    assert r.status is Status.CHANGED and r.held and not r.new_since_notified and "設定を変える前" in r.message
    assert "+two" in r.diff
    state = store.load()
    shown = latest_changes(cfg.targets[0], cfg, store, state)
    assert shown and shown.pending and "+two" in shown.text
    assert [x.status for x in saved_results(cfg.targets, cfg, data)] == [Status.CHANGED]
    assert acknowledge(cfg.targets[0], cfg, data, seen=r.generation)
    state = store.load()
    after = latest_changes(cfg.targets[0], cfg, store, state)
    assert state.set_aside == [] and (after is None or not after.pending)
    clock()
    check_target(cfg.targets[0], cfg, data)
    assert not any(store.snapshot_dir(s).exists() for s in pair)  # pruned once seen


# --------------------------------------------------------------------------- looking does not change anything


def test_comparing_generations_in_the_history_does_not_rewrite_what_was_saved(tmp_path, clock) -> None:
    from watchpoint.check import acknowledge
    from watchpoint.report import changes_between, latest_changes

    src = tmp_path / "a.txt"
    cfg = _config(tmp_path, f"  - name: a\n    source: '{src.as_posix()}'\n")
    data = tmp_path / "data"
    for text in ("A\n", "B\n", "C\n"):
        src.write_text(text, encoding="utf-8")
        clock()
        check_target(cfg.targets[0], cfg, data)
        if text == "B\n":
            acknowledge(cfg.targets[0], cfg, data)
    store = TargetStore(data, "a")
    state = store.load()
    saved = {p.name: p.read_text(encoding="utf-8") for p in store.snapshot_dir(state.snapshot).iterdir()}
    first = changes_between(cfg.targets[0], cfg, store, state, state.history[0], state.history[-1])  # A -> C
    assert "-A" in first.text and "+C" in first.text
    latest_changes(cfg.targets[0], cfg, store, state)
    after = {p.name: p.read_text(encoding="utf-8") for p in store.snapshot_dir(state.snapshot).iterdir()}
    assert after == saved and "-B" in saved["diff.txt"] and "-A" not in saved["diff.txt"]
    assert store.compared_with(state.snapshot) == state.history[1]  # saved against B, the copy it was checked with
    acknowledge(cfg.targets[0], cfg, data)
    shown = latest_changes(cfg.targets[0], cfg, store, store.load())
    assert shown and "-B" in shown.text and " → " in shown.title  # the acknowledged change says what it was against


# --------------------------------------------------------------------------- normalize touches values only


def test_nfkc_keeps_a_separator_inside_a_cell_inside_the_cell(tmp_path, clock) -> None:
    from watchpoint.textify import parse_records

    src = tmp_path / "c.csv"
    src.write_text("No.,memo\n1,A | flag=OLD\n", encoding="utf-8")
    cfg = _config(tmp_path, f"  - name: c\n    source: '{src.as_posix()}'\n    key: No.\n    normalize: [nfkc]\n")
    data = tmp_path / "data"
    clock()
    check_target(cfg.targets[0], cfg, data)
    store = TargetStore(data, "c")
    fields = next(iter(parse_records(store.read_text(store.load().snapshot)).fields.values()))
    assert fields == {"memo": "A ｜ flag=OLD"}  # one cell, not a made-up flag column


def test_case_normalizes_values_but_not_column_names_or_what_rules_name(tmp_path, clock) -> None:
    src = tmp_path / "s.csv"
    src.write_text("No.,Status\n1,BAD\n2,OK\n", encoding="utf-8")
    cfg = _config(tmp_path, f"  - name: s\n    source: '{src.as_posix()}'\n    key: No.\n    normalize: [case]\n"
                            "    rules: [{field: Status, one_of: [OK]}]\n")  # fmt: skip
    data = tmp_path / "data"
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert r.alerts == ["No. 1: Status が「bad」です（OK のどれかのはず）"]  # BAD is caught, OK is not
    text = TargetStore(data, "s").read_text(TargetStore(data, "s").load().snapshot)
    assert "[No.=2] Status=ok" in text and "@列 Status" in text


def test_a_rule_condition_is_compared_after_the_same_normalization(tmp_path, clock) -> None:
    src = tmp_path / "s.csv"
    src.write_text("No.,状態,回答\n1,ＤＯＮＥ,\n", encoding="utf-8")  # full-width letters
    cfg = _config(tmp_path, f"  - name: s\n    source: '{src.as_posix()}'\n    key: No.\n    normalize: [nfkc, case]\n"
                            "    rules: [{field: 回答, required: true, when: {状態: DONE}}]\n")  # fmt: skip
    data = tmp_path / "data"
    clock()
    assert check_target(cfg.targets[0], cfg, data).alerts == ["No. 1: 回答 が空です（状態 が done）"]


# --------------------------------------------------------------------------- one place down stops only itself


def test_a_share_that_refuses_access_fails_only_its_own_target(tmp_path, clock, monkeypatch) -> None:
    from pathlib import Path as P

    real = P.is_dir

    def refusing(self, *a, **k):
        if str(self).startswith("\\\\refuse"):
            raise PermissionError(13, "Access is denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(P, "is_dir", refusing)
    ok = tmp_path / "ok.txt"
    ok.write_text("fine\n", encoding="utf-8")
    cfg = _config(tmp_path, f"  - name: ok\n    source: '{ok.as_posix()}'\n"
                            "  - name: denied\n    source: '//refuse/share/q.xlsx'\n    cells: [A1]\n"
                            "  - name: 止めた見張り\n    source: '//refuse/share/folder'\n    enabled: false\n")  # fmt: skip
    assert str(P(cfg.targets[1].source)).startswith("\\\\refuse")  # the refusal above really applies to it
    data = tmp_path / "data"
    clock()
    results = {t.name: check_target(t, cfg, data) for t in cfg.targets}
    assert results["ok"].status is Status.FIRST
    assert results["denied"].status in (Status.FAILED, Status.RETRYING)
    assert results["止めた見張り"].status is Status.SKIPPED


def test_a_trailing_separator_says_folder_without_touching_the_place() -> None:
    from watchpoint.config import Target

    assert Target(name="f", source="\\\\nowhere\\share\\議事録\\").kind == "folder"


# --------------------------------------------------------------------------- a change that was undone


def test_a_change_that_was_undone_is_still_to_be_acknowledged(tmp_path, clock) -> None:
    from watchpoint.check import acknowledge, has_pending
    from watchpoint.report import latest_changes

    src = tmp_path / "a.txt"
    cfg = _config(tmp_path, f"  - name: a\n    source: '{src.as_posix()}'\n")
    data = tmp_path / "data"
    for text in ("A\n", "B\n"):
        src.write_text(text, encoding="utf-8")
        clock()
        check_target(cfg.targets[0], cfg, data)
    src.write_text("A\n", encoding="utf-8")
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert r.status is Status.CHANGED and "確認済みと同じ内容に戻りました" in r.message and "+B" in r.diff
    store = TargetStore(data, "a")
    shown = latest_changes(cfg.targets[0], cfg, store, store.load())
    assert shown and shown.pending and "戻りました" in shown.message  # the page and status agree with check
    assert send_notifications([r], cfg, data) == 1 and "戻りました" in _drops(tmp_path)[0]
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert r.status is Status.CHANGED and send_notifications([r], cfg, data) == 0  # told once
    assert acknowledge(cfg.targets[0], cfg, data, seen=r.generation)
    assert not has_pending(store.load())
    clock()
    assert check_target(cfg.targets[0], cfg, data).status is Status.UNCHANGED


def test_an_undone_change_says_so_even_when_the_copy_in_between_is_gone(tmp_path, clock) -> None:
    src = tmp_path / "a.txt"
    cfg = _config(tmp_path, f"  - name: a\n    source: '{src.as_posix()}'\n")
    data = tmp_path / "data"
    for text in ("A\n", "B\n", "A\n"):
        src.write_text(text, encoding="utf-8")
        clock()
        check_target(cfg.targets[0], cfg, data)
    store = TargetStore(data, "a")
    state = store.load()
    middle = state.history[1]
    import shutil

    shutil.rmtree(store.snapshot_dir(middle))  # as if pruned
    clock()
    r = check_target(cfg.targets[0], cfg, data)
    assert r.status is Status.CHANGED and "途中の版はもう残っていません" in r.message
