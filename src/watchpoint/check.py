"""One check of one target: fetch a copy, turn it into text, compare with the acknowledged state."""

from __future__ import annotations

import datetime as dt
import difflib
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from . import fetchers, notify
from .config import Config, Target
from .diffing import annotate_hunks, inline_changes, record_diff
from .rules import alerts_for
from .store import State, TargetStore, append_log
from .timewords import every_label

UNSUPPORTED_MARK = fetchers.UNSUPPORTED_MARK

# Settings that decide the text. If one changes, old texts are not comparable any more.
TEXT_SETTINGS = (
    "source",
    "fetch",
    "sheets",
    "header_row",
    "key",
    "columns",
    "where",
    "formulas",
    "cells",
    "watch",
    "recursive",
    "sections",
    "include",
    "ignore",
    "normalize",
    "sort_lines",
    "selector",
    "remove",
    "records",
    "command",
    "format",
    "fields",
)


class Status(StrEnum):
    FIRST = "初回"
    CHANGED = "変更あり"
    UNCHANGED = "変更なし"
    FILTERED = "条件外の変化"
    RETRYING = "一時的に取得できず"
    FAILED = "取得失敗"
    NOT_DUE = "まだ時間前"
    SKIPPED = "対象外"


@dataclass
class Result:
    target: Target
    status: Status
    message: str = ""
    diff: str = ""
    diff_truncated: bool = False
    added: int = 0
    removed: int = 0
    changed: int = 0
    inline: list[str] = field(default_factory=list)
    snapshot: Path | None = None
    meta: dict = field(default_factory=dict)
    new_since_notified: bool = False
    warning: bool = False  # something the person must hear about although it is not a change
    alerts: list[str] = field(default_factory=list)  # 要注意: a missed deadline, values that break a rule
    new_alerts: list[str] = field(default_factory=list)  # those no notice has been written for yet
    notice: str | None = None  # a one-off warning no notice has been written for yet
    held: bool = False  # shown because it is still unacknowledged, not because this check found it
    # the whole comparison, saved with the copy by a check (compare itself only reads)
    full_diff: str = ""
    full_changes: str | None = None
    entries: list[dict] = field(default_factory=list)  # per record, as data (see diffing.RecordDiff)

    @property
    def needs_attention(self) -> bool:
        return self.status in (Status.CHANGED, Status.FAILED) or self.warning or bool(self.alerts)

    @property
    def generation(self) -> str | None:
        """The snapshot id shown; `ack --seen` takes it back so a newer copy is not acknowledged unseen."""
        return self.snapshot.name if self.snapshot else None

    def to_dict(self) -> dict:
        return {
            "name": self.target.name,
            "source": self.target.source,
            "tags": self.target.tags,
            "status": self.status.value,
            "message": self.message,
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "points": self.target.points.strip(),
            "notify_when": self.target.notify_when.strip(),
            "diff": self.diff,
            "diff_truncated": self.diff_truncated,
            "inline": self.inline,
            "meta": self.meta,
            "text_path": str(self.snapshot / "text.txt") if self.snapshot else None,
            "generation": self.generation,
            "warning": self.warning,
            "alerts": self.alerts,
        }


def _now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def _settings_fingerprint(target: Target) -> str:
    return fetchers.sha256_text(target.model_dump_json(include=set(TEXT_SETTINGS)))


def build_text(target: Target, workdir: Path) -> tuple[str, str]:
    """Fetch the target into workdir and return (text, digest), without touching any state."""
    f = fetchers.fetch(target, workdir)
    return f.text, f.digest


def is_due(target: Target, state: State, now: dt.datetime | None = None) -> tuple[bool, dt.datetime | None]:
    interval = target.interval
    if interval is None or not state.last_checked:
        return True, None
    now = now or _now()
    last = dt.datetime.fromisoformat(state.last_checked)
    if last > now:
        return True, None
    nxt = last + interval
    return now >= nxt, nxt


# What started a check, kept with each copy it took and in the log.
VIA = {
    "terminal": "ターミナル",
    "scheduler": "タスク スケジューラ",
    "page": "画面で押した",
    "page-auto": "画面の自動",
    "added": "追加したとき",
}


def check_all(
    targets: list[Target], config: Config, data_dir: Path, due_only: bool = False, via: str = "terminal"
) -> list[Result]:
    """Check targets side by side (a slow file share should not hold up the rest). Order is kept."""
    workers = max(1, min(config.workers, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda t: check_target(t, config, data_dir, due_only=due_only, via=via), targets))


def check_target(
    target: Target, config: Config, data_dir: Path, due_only: bool = False, via: str = "terminal"
) -> Result:
    store = TargetStore(data_dir, target.store_key)
    if not target.enabled:
        return Result(target, Status.SKIPPED, "enabled: false")

    state = store.load()
    if due_only:
        now = _now()
        hours = config.check_hours
        if hours and not hours.allows(now):
            return Result(
                target, Status.NOT_DUE, f"時間外（{hours.label}）。次は {hours.next_start(now):%m/%d %H:%M} から"
            )
        due, nxt = is_due(target, state, now)
        if not due:
            return Result(target, Status.NOT_DUE, f"次は {nxt:%m/%d %H:%M}（{every_label(target.every)}）")

    stamp = _now().isoformat(sep=" ")
    try:
        result = _check(target, config, store, state)
    except Exception as e:  # noqa: BLE001 - every failure is reported per target
        store.discard_incoming()
        state.failures += 1
        state.last_checked = stamp
        state.last_error = str(e) or type(e).__name__
        if state.failures >= target.fail_after:
            status, msg = Status.FAILED, state.last_error
            if state.failures > 1:
                msg = f"{msg}（{state.failures} 回続けて）"
        else:
            status = Status.RETRYING
            msg = f"{state.last_error}（{state.failures}/{target.fail_after} 回目。続いたら取得失敗として出す）"
        if has_pending(state):
            msg += "。前に取った未確認の変更が残っています（watchpoint diff で見られる）"
        state.last_status = status.value
        alerts, new_alerts = _update_alerts(target, store, state)
        store.save(state)
        append_log(data_dir, {"name": target.name, "status": status.value, "message": state.last_error, "via": via})
        # Notify about a failure once per run of failures, from when it reaches fail_after until a notice is written.
        return Result(
            target,
            status,
            msg,
            meta={"failures": state.failures},
            new_since_notified=state.failures >= target.fail_after and not state.failure_notified,
            alerts=alerts,
            new_alerts=new_alerts,
            notice=state.notice,
        )

    if state.snapshot and state.snapshot in state.history and state.snapshot not in state.via:
        state.via[state.snapshot] = via  # this check took that copy
    state.failures = 0
    state.failure_notified = False  # the run of failures is over; a later one is told again
    state.last_checked = stamp
    state.last_status = result.status.value
    state.last_error = None
    state.source = target.source
    state.settings = _settings_fingerprint(target)
    if result.status is Status.CHANGED and state.notified != state.snapshot and not result.held:
        result.new_since_notified = True
    result.alerts, result.new_alerts = _update_alerts(target, store, state)
    result.notice = state.notice
    store.save(state)
    append_log(
        data_dir,
        {
            "name": target.name,
            "status": result.status.value,
            "message": result.message,
            "snapshot": state.snapshot,
            "via": via,
        },
    )
    return result


def _update_alerts(target: Target, store: TargetStore, state: State) -> tuple[list[str], list[str]]:
    """Recompute the rule warnings; return them and the ones no notice has been written for yet."""
    try:
        alerts = alerts_for(target, store, state, _now())
    except Exception as e:  # noqa: BLE001 - a broken rule must not stop the check itself
        alerts = [f"ルールを確かめられませんでした: {e}"]
    state.alerts = alerts
    # a warning that cleared is forgotten, so it is told again if it comes back
    state.alerts_notified = [a for a in state.alerts_notified if a in alerts]
    new = [a for a in alerts if a not in state.alerts_notified]
    return alerts, new


def _check(target: Target, config: Config, store: TargetStore, state: State) -> Result:
    now = _now()
    old_baseline, old_latest = state.baseline, state.snapshot
    had_pending = bool(old_latest and old_baseline and old_latest != old_baseline)
    settings_same = state.settings == _settings_fingerprint(target) and state.source == target.source
    latest_ok = state.snapshot is not None and store.read_text(state.snapshot) is not None

    current_probe = fetchers.probe(target)
    skip_fetch = current_probe is not None and settings_same and latest_ok and state.probe == current_probe

    if not skip_fetch:
        incoming = store.incoming_dir()
        fetched = fetchers.fetch(target, incoming)
        if settings_same and latest_ok and state.sha256 == fetched.digest:
            store.discard_incoming()
        elif settings_same and latest_ok and fetched.text == store.read_text(state.snapshot):
            store.discard_incoming()
            state.sha256 = fetched.digest
            state.meta = fetched.meta or state.meta
        else:
            (incoming / "text.txt").write_text(fetched.text, encoding="utf-8")
            if fetched.text != store.read_text(state.snapshot):
                state.last_changed = now.isoformat(sep=" ")
            snap_id = base_id = now.strftime("%Y%m%d-%H%M%S")
            n = 1
            while store.snapshot_dir(snap_id).exists():  # two checks in the same second
                n += 1
                snap_id = f"{base_id}-{n}"
            snap = store.commit_incoming(incoming, snap_id)
            if not config.keep_originals:
                for f in snap.glob("original*"):
                    f.unlink(missing_ok=True)
            state.snapshot = snap_id
            state.sha256 = fetched.digest
            state.meta = fetched.meta
            if snap_id not in state.history:
                state.history.append(snap_id)
        state.probe = fetched.probe or current_probe

    if not settings_same or store.read_text(state.baseline) is None:
        note = "基準として保存した（次回から差分を出す）"
        lost = False
        if state.baseline and not settings_same:
            note = "設定か場所が変わったので、基準を取り直した"
            if had_pending:
                # Texts made with other settings cannot be compared, but the person must not
                # lose an unacknowledged change without a word.
                lost = True
                note += (
                    f"。その前の未確認の変更（{pretty_id(old_baseline)} → {pretty_id(old_latest)}）は、"
                    "確認済みにするまで残して、未確認の変更として出します"
                )
        state.baseline = state.snapshot
        if lost:
            state.notice = note  # sent with the next notification, kept until it is written
            state.set_aside.append({"from": old_baseline, "to": old_latest})
        store.prune(state, config.keep)
        return Result(
            target,
            Status.FIRST,
            note,
            snapshot=store.snapshot_dir(state.snapshot),
            meta=state.meta,
            warning=lost,
        )

    result = compare(target, config, store, state)
    if result.full_diff:  # kept with the latest copy, saying what it was compared with
        store.save_changes(state.snapshot, state.baseline, result.full_diff, result.full_changes, result.entries)
    if result.status is Status.FILTERED:
        # Nobody asked to hear about this kind of change; move the baseline so it does not pile up.
        state.baseline = state.snapshot
    result = pending(target, config, store, state, compared=result) or result
    store.prune(state, config.keep)
    return result


def has_pending(state: State) -> bool:
    """Something is still to be acknowledged: a copy newer than the baseline, or changes set aside."""
    return bool(state.snapshot and state.baseline and state.snapshot != state.baseline) or bool(state.set_aside)


def pending(
    target: Target, config: Config, store: TargetStore, state: State, compared: Result | None = None
) -> Result | None:
    """What is still to be acknowledged, the one place every view and check asks.

    The changes since the baseline come first. If there are none (or none worth telling), changes set
    aside by a settings change are shown instead, so they stay in sight until acknowledged. Returns
    None when nothing is pending. compared: the baseline-to-latest comparison, when already made.
    """
    newer = bool(state.snapshot and state.baseline and state.snapshot != state.baseline)
    r = compared if compared is not None else (compare(target, config, store, state) if newer else None)
    if newer and r is not None and r.status is Status.UNCHANGED:
        r = _reverted(target, config, store, state)
    if r is not None and r.status is Status.CHANGED:
        if state.set_aside:
            r.message += "。設定を変える前の未確認の変更も残っています（確認済みにすると、両方とも確認済みになる）"
        return r
    if state.set_aside:
        pair = state.set_aside[-1]
        held = compare(target, config, store, state, base_id=pair["from"], new_id=pair["to"])
        held.status = Status.CHANGED
        held.message = (
            f"設定を変える前の未確認の変更（{pretty_id(pair['from'])} → {pretty_id(pair['to'])}）: {held.message}"
        )
        held.snapshot = store.snapshot_dir(state.snapshot)  # acknowledging it acknowledges up to the latest copy
        held.meta = state.meta
        held.held = True
        return held
    return r if newer else None


def _reverted(target: Target, config: Config, store: TargetStore, state: State) -> Result:
    """The latest copy reads the same as the baseline, yet a copy was taken in between: the source changed
    and changed back. That is still something to acknowledge (someone may have acted on the change), so it
    is shown with what the copy in between said, when that copy is still kept."""
    base_text = store.read_text(state.baseline)
    start = state.history.index(state.baseline) + 1 if state.baseline in state.history else 0
    end = state.history.index(state.snapshot) if state.snapshot in state.history else len(state.history)
    between = [s for s in state.history[start:end] if store.read_text(s) not in (None, base_text)]
    msg = "いったん変わってから、確認済みと同じ内容に戻りました"
    if not between:
        r = Result(target, Status.CHANGED, f"{msg}（途中の版はもう残っていません）")
    else:
        mid = between[-1]
        r = compare(target, config, store, state, base_id=state.baseline, new_id=mid)
        r.status = Status.CHANGED
        r.message = f"{msg}。途中の {pretty_id(mid)} の版では: {r.message}"
        r.full_diff, r.full_changes = "", None  # nothing to save with the latest copy, which reads as the baseline
    r.snapshot = store.snapshot_dir(state.snapshot)  # acknowledging it acknowledges up to the latest copy
    r.meta = state.meta
    return r


def compare(
    target: Target,
    config: Config,
    store: TargetStore,
    state: State,
    base_id: str | None = None,
    new_id: str | None = None,
) -> Result:
    """Compare two generations (by default: the baseline and the latest copy)."""
    base_id = base_id or state.baseline
    new_id = new_id or state.snapshot
    snap = store.snapshot_dir(new_id)
    base_text = store.read_text(base_id) or ""
    text = store.read_text(new_id) or ""
    meta = state.meta if new_id == state.snapshot else {}
    if base_text == text:
        return Result(target, Status.UNCHANGED, "前回確認したときと同じ", snapshot=snap, meta=meta)

    unified = list(
        difflib.unified_diff(
            base_text.splitlines(),
            text.splitlines(),
            fromfile=f"確認済み ({base_id})",
            tofile=f"今回 ({new_id})",
            lineterm="",
            n=1,
        )
    )
    full: dict = {"full_diff": "\n".join(unified) + "\n"}
    since = f"（確認済みの {pretty_id(base_id)} 時点から）"
    version = _version_note(meta)

    if text.startswith(UNSUPPORTED_MARK):
        return Result(
            target, Status.CHANGED, "ファイルが変わった（中身は比べられない形式）", snapshot=snap, meta=meta, **full
        )

    inline: list[str] = []
    if target.is_keyed:
        rd = record_diff(base_text, text, target.report)
        lines = rd.lines
        full["full_changes"] = "\n".join(lines) + "\n"
        full["entries"] = rd.entries
        counts = dict(added=rd.added, removed=rd.removed, changed=rd.changed)
        parts = [f"追加 {rd.added}", f"変更 {rd.changed}", f"削除 {rd.removed}"]
        if rd.columns:
            parts.append(f"列の変化 {rd.columns}")
        msg = " / ".join(parts) + f" 件{since}{version}"
        counted = any(counts.values()) or rd.columns > 0
        candidates = [line for line in lines if not line.startswith("## ")]
        if not counted:
            everything = record_diff(base_text, text)
            if not (everything.added or everything.removed or everything.changed or everything.columns):
                lines = annotate_hunks(unified, base_text)
                body = diff_body(unified)
                plus = sum(line.startswith("+") for line in body)
                minus = sum(line.startswith("-") for line in body)
                msg = f"+{plus} / -{minus} 行（行ごとに比べられない変化）{since}{version}"
                counted = True
                candidates = [line[1:] for line in body if line.startswith(("+", "-"))]
    else:
        lines = annotate_hunks(unified, base_text)
        body = diff_body(unified)
        plus = [line[1:] for line in body if line.startswith("+")]
        minus = [line[1:] for line in body if line.startswith("-")]
        inline = inline_changes(unified)
        counts = dict(added=len(plus), removed=len(minus))
        msg = f"+{len(plus)} / -{len(minus)} 行{since}{version}"
        wanted = set(target.report)
        counted = bool(("added" in wanted or "changed" in wanted) and plus) or bool(
            ("removed" in wanted or "changed" in wanted) and minus
        )
        candidates = (plus if wanted & {"added", "changed"} else []) + (
            minus if wanted & {"removed", "changed"} else []
        )

    if counted and target.match:
        regs = [re.compile(p) for p in target.match]
        counted = any(r.search(line) for line in candidates for r in regs)
        if not counted:
            return Result(
                target,
                Status.FILTERED,
                f"変化はあったが match に当たらない（自動で確認済み）{since}",
                snapshot=snap,
                meta=meta,
                **full,
            )
    if not counted:
        return Result(
            target,
            Status.FILTERED,
            f"変化はあったが report で知らせない種類（自動で確認済み）{since}",
            snapshot=snap,
            meta=meta,
            **full,
        )

    truncated = len(lines) > config.max_diff_lines
    return Result(
        target,
        Status.CHANGED,
        msg,
        diff="\n".join(lines[: config.max_diff_lines]),
        diff_truncated=truncated,
        inline=inline[: config.max_diff_lines],
        snapshot=snap,
        meta=meta,
        **counts,
        **full,
    )


def diff_body(lines: list[str]) -> list[str]:
    """A unified diff without its two file-header lines (content lines may start with --- or +++ too)."""
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            return lines[i:]
    return lines


def _version_note(meta: dict) -> str:
    if meta.get("version"):
        who = f"、{meta['by']}" if meta.get("by") else ""
        return f" 版 {meta['version']}{who}"
    return ""


def pretty_id(snap_id: str | None) -> str:
    if not snap_id:
        return "-"
    try:
        d = dt.datetime.strptime(snap_id[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return snap_id
    return d.strftime("%m/%d %H:%M")


class Stale(Exception):
    """The copy shown to the person is no longer the latest one."""


def acknowledge(
    target: Target, config: Config, data_dir: Path, note: str | None = None, seen: str | None = None
) -> bool:
    """Make the latest copy the new baseline. Returns False if there was nothing to acknowledge.

    seen: the snapshot the person looked at. If a newer one has arrived since, raise Stale
    instead of acknowledging a change nobody has seen.
    """
    store = TargetStore(data_dir, target.store_key)
    state = store.load()
    if not state.snapshot or not has_pending(state):
        return False
    if seen is not None and seen != state.snapshot:
        raise Stale(target.name)
    state.baseline = state.snapshot
    state.set_aside = []  # seen together with the rest
    if note:
        state.notes[state.snapshot] = note
    if state.last_status == Status.CHANGED.value:
        state.last_status = Status.UNCHANGED.value
    store.prune(state, config.keep)
    store.save(state)
    append_log(data_dir, {"name": target.name, "status": "確認済み", "message": note or "", "snapshot": state.snapshot})
    return True


def _remember(target: Target, data_dir: Path, sent) -> None:
    """Record in the target's state that a notice was written (sent is applied to the state)."""
    store = TargetStore(data_dir, target.store_key)
    state = store.load()
    sent(state)
    store.save(state)


NOTICE_LINES = 3
NOTICE_WIDTH = 120


def notice_lines(diff: str) -> list[str]:
    """The first few changed lines for a notice: headers and unchanged context lines are left out."""
    picked = [line for line in diff_body(diff.splitlines()) if line.strip() and not line.startswith(("@@", "## ", " "))]
    out = [line if len(line) <= NOTICE_WIDTH else line[: NOTICE_WIDTH - 1] + "…" for line in picked[:NOTICE_LINES]]
    if len(picked) > NOTICE_LINES:
        out.append(f"ほか {len(picked) - NOTICE_LINES} 行")
    return out


def send_notifications(results: list[Result], config: Config, data_dir: Path) -> int:
    """Leave a notice for each change, run of failures, warning and rule warning not told yet.

    A notice counts as sent only once it is written to the drop folder. If writing fails, nothing is
    marked, and the same notice is tried again by the next check that notifies.
    """
    assert config.notify
    folder = Path(config.notify.drop_folder)
    sent = 0
    for r in results:
        for text, mark in _notices(r, config):
            try:
                notify.drop(folder, text)
            except OSError as e:
                append_log(data_dir, {"name": r.target.name, "status": "通知できず", "message": f"{folder}: {e}"})
                continue
            _remember(r.target, data_dir, mark)
            sent += 1
    return sent


def _notices(r: Result, config: Config) -> list[tuple[str, Callable[[State], None]]]:
    """The notices a result still owes, each with how to mark it sent in the target's state."""
    name = r.target.name
    out: list[tuple[str, Callable[[State], None]]] = []
    if r.status is Status.CHANGED and r.new_since_notified:
        snap = r.generation

        def changed(s: State) -> None:
            s.notified = snap  # the copy that was told, not whatever is latest by the time this is written

        out.append(("\n".join([f"{name}: {r.message}", *notice_lines(r.diff)]), changed))
    if r.status is Status.FAILED and r.new_since_notified and config.notify and config.notify.on_failure:

        def failed(s: State) -> None:
            s.failure_notified = True

        out.append((f"{name}: 取得できませんでした。{r.message}", failed))
    if r.notice:
        told = r.notice

        def warned(s: State) -> None:
            if s.notice == told:
                s.notice = None

        out.append((f"{name}: {told}", warned))
    if r.new_alerts:
        new = list(r.new_alerts)
        shown = new[:10] + ([f"ほか {len(new) - 10} 件"] if len(new) > 10 else [])

        def alerted(s: State) -> None:
            s.alerts_notified += [a for a in new if a not in s.alerts_notified]

        out.append(("\n".join([f"{name}: 要注意 {len(new)} 件", *shown]), alerted))
    return out
