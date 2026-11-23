"""What the commands and the browser page show about a target, read from its saved state (never fetches)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from .check import VIA, Result, Status, compare, diff_body, is_due, pending, pretty_id
from .config import PLAN_SUFFIXES, CheckHours, Config, Target
from .rules import alerts_for
from .store import State, TargetStore

TEXT_SUFFIXES = (".txt", ".md", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".ini", ".log", ".csv", ".tsv")


def kind_label(t: Target) -> str:
    kind = t.kind
    if kind == "folder":
        return "フォルダ"
    if t.watch == "timestamp":
        return "更新日時とサイズだけ"
    if Path(t.source.split("?")[0]).suffix.lower() in PLAN_SUFFIXES:
        return f"Project の計画・タスクごと（{t.key or 'UID'} で見分け）"
    if t.attachments:
        return f"Confluence の添付（{', '.join(t.attachments)}）" + (f"・行ごと（{t.key} で見分け）" if t.key else "")
    if t.cells:
        return f"Excel・セル（{len(t.cells)} か所）" + {"office": "・Office 経由", "download": "・Web から"}.get(
            kind, ""
        )
    label = {
        "web": "Web ページ", "json": "JSON API", "confluence": "Confluence", "office": "Office 経由", "command": "コマンドの出力",
        "download": "Web のファイル",
    }.get(kind)  # fmt: skip
    if label:
        return label + (f"・行ごと（{t.key} で見分け）" if t.key else "")
    if t.key:
        return f"行ごと（{t.key} で見分け）"
    suffix = Path(t.source).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return "Excel"
    if suffix == ".docx":
        return "Word"
    if suffix in TEXT_SUFFIXES:
        return "テキスト"
    return "中身が変わったかだけ"


@dataclass
class Changes:
    """A set of changes to show: either per record (keyed) or a unified diff."""

    title: str
    message: str
    text: str
    keyed: bool
    pending: bool
    inline: list[str]
    truncated: bool = False
    entries: list[dict] = field(default_factory=list)  # per record, as data, for the page to lay out as a table
    entries_total: int = 0  # how many there were (only MAX_ENTRIES are sent)


MAX_ENTRIES = 5000  # records changed, sent to the page at once


def _capped(entries: list[dict]) -> tuple[list[dict], int]:
    return entries[:MAX_ENTRIES], len(entries)


def latest_changes(target: Target, config: Config, store: TargetStore, state: State) -> Changes | None:
    """The unacknowledged changes, or else the last changes that were found (already acknowledged)."""
    r = pending(target, config, store, state)
    if r is not None and r.status is Status.CHANGED:
        keyed = target.is_keyed and not r.diff.startswith("---")
        return Changes("未確認の変更", r.message, r.diff, keyed, True, r.inline, r.diff_truncated, *_capped(r.entries))
    for snap_id in reversed(state.history):
        d = store.snapshot_dir(snap_id)
        f = d / "changes.txt" if (d / "changes.txt").exists() else d / "diff.txt"
        if f.exists():
            text = f.read_text(encoding="utf-8").rstrip("\n")
            base = store.compared_with(snap_id)
            since = f"{pretty_id(base)} → " if base else ""
            title = f"{since}{pretty_id(snap_id)} の変更（確認済み）"
            entries, total = _capped(store.saved_entries(snap_id))
            return Changes(title, "", text, f.name == "changes.txt", False, [], False, entries, total)
    return None


def changes_between(target: Target, config: Config, store: TargetStore, state: State, a: str, b: str) -> Changes:
    r = compare(target, config, store, state, base_id=a, new_id=b)
    keyed = target.is_keyed and not r.diff.startswith("---")
    title = f"{pretty_id(a)} → {pretty_id(b)}"
    return Changes(title, r.message, r.diff, keyed, False, r.inline, r.diff_truncated, *_capped(r.entries))


@dataclass
class Generation:
    number: int  # 1 is the latest, as used by diff --from/--to and show --gen
    snap_id: str
    when: str
    marks: list[str]
    summary: str
    note: str
    via: str = ""  # what started the check that took this copy (from VIA), "" for copies taken before it was kept


def generations(store: TargetStore, state: State) -> list[Generation]:
    out = []
    for i, snap_id in enumerate(reversed(state.history), start=1):
        d = store.snapshot_dir(snap_id)
        marks = []
        if snap_id == state.snapshot:
            marks.append("最新")
        if snap_id == state.baseline:
            marks.append("確認済み")
        summary = ""
        f = d / "changes.txt" if (d / "changes.txt").exists() else d / "diff.txt"
        if f.exists():
            raw = f.read_text(encoding="utf-8").splitlines()
            if f.name == "diff.txt":
                body = [line for line in diff_body(raw) if line.startswith(("+", "-"))]
            else:
                body = [line for line in raw if line and not line.startswith("## ")]
            summary = f"{len(body)} 行: " + (body[0][:60] if body else "")
        via = VIA.get(state.via.get(snap_id, ""), "")
        out.append(Generation(i, snap_id, pretty_id(snap_id), marks, summary, state.notes.get(snap_id, ""), via))
    return out


def next_due(target: Target, state: State, hours: CheckHours | None = None) -> str | None:
    """When the next check by interval will happen, moved into the check hours when there are some."""
    if not target.every or not target.enabled:
        return None
    _, nxt = is_due(target, state)
    if hours:
        now = dt.datetime.now().replace(second=0, microsecond=0)
        nxt = hours.next_start(max(nxt or now, now))
    return f"{nxt:%m/%d %H:%M}" if nxt else None


def saved_results(targets: list[Target], config: Config, data_dir: Path) -> list[Result]:
    """Results rebuilt from saved state, without fetching: pending changes, current failures and 要注意."""
    out = []
    for t in targets:
        store = TargetStore(data_dir, t.store_key)
        state = store.load()
        failed = state.last_status == Status.FAILED.value
        alerts = alerts_for(t, store, state) if t.enabled and (t.rules or t.expect_update) else []
        r = pending(t, config, store, state)
        if r is not None and r.status is Status.CHANGED:
            if failed:  # the change fetched earlier is still unseen; do not hide it behind the failure
                r.message += f"。ただし最新の取得は失敗: {state.last_error or ''}"
            r.alerts = alerts
            out.append(r)
        elif failed:
            out.append(Result(t, Status.FAILED, state.last_error or "", alerts=alerts))
        elif alerts:
            out.append(Result(t, Status.UNCHANGED, "変更はないが、要注意あり", alerts=alerts))
    return out


def as_markdown(results: list[Result], total: int | None = None, chat: bool = False, places: bool = True) -> str:
    """The results for an AI to read.

    For an agent (the default) it carries what the agent needs to act: where the text is kept and
    the command that acknowledges. For a chat (chat=True, what the page copies) those lines are left
    out: a person pastes it, reads the answer and acknowledges on the page. places=False also leaves
    out where each source is (share paths, URLs), so nothing is pasted that the summary does not need.
    """
    changed = sum(r.status is Status.CHANGED for r in results)
    failed = sum(r.status is Status.FAILED for r in results)
    alerted = sum(bool(r.alerts) for r in results)
    out = [
        "# watchpoint check の結果",
        "",
        f"全 {total if total is not None else len(results)} 件のうち、変更あり {changed} 件 / 取得失敗 {failed} 件"
        + (f" / 要注意 {alerted} 件。" if alerted else "。")
        + ("（変更なしの見張りは省略）" if total is not None and total != len(results) else ""),
        "",
        "「要注意」は、本人が決めたルール（更新の期限・値の決まり）に合わないもので、watchpoint が確かめた事実です。"
        "あれば、ほかより先に挙げてください。"
        "各見張りの「読むポイント」に従って、変更を読んでください。"
        "「知らせる条件」に当たらない見張りは「特になし」としてください。"
        + (
            "見張りごとに、何が変わったかを短くまとめてください。"
            if chat
            else "変更は確認済みにするまで出続けます。本人が見終えたら、見張りごとの「確認済みにする」のコマンドを実行してください"
            "（その後に届いた新しい変更までは確認済みにしません）。"
        ),
        "",
    ]
    for r in results:
        out.append(f"## {r.target.name}")
        out.append(f"- 状態: {r.status.value}")
        if r.message:
            out.append(f"- 内容: {r.message}")
        if places:
            out.append(f"- {'コマンド' if r.target.command else '元の場所'}: {r.target.source}")
        if r.target.tags:
            out.append(f"- タグ: {', '.join(r.target.tags)}")
        if r.meta.get("title"):
            out.append(
                f"- ページ: {r.meta['title']}（版 {r.meta.get('version')}、{r.meta.get('by') or '-'}、{r.meta.get('when') or '-'}）"
            )
        if r.snapshot and not chat:
            out.append(f"- 今回のテキスト: {r.snapshot / 'text.txt'}")
        if r.alerts:
            out += ["", "### 要注意（本人が決めたルールに合わない）"] + [f"- {a}" for a in r.alerts]
        if r.status is Status.CHANGED and r.generation and not chat:
            out.append(f'- 確認済みにする: `watchpoint ack "{r.target.name}" --seen {r.generation}`')
        if r.status is Status.CHANGED:
            if r.target.points.strip():
                out += ["", "### 読むポイント", r.target.points.strip()]
            if r.target.notify_when.strip():
                out += ["", "### 知らせる条件", r.target.notify_when.strip()]
            if r.diff and r.target.is_keyed and not r.diff.startswith("---"):
                out += ["", "### 変更（行ごと。追加 / 変更 / 削除）", "```", r.diff, "```"]
            elif r.diff:
                out += ["", "### 差分（- 確認済み / + 今回。@@ の後ろはその下の見出し）", "```diff", r.diff, "```"]
            if r.inline:
                out += ["", "### 行の中の変化（[前→後]）"] + [f"- {line}" for line in r.inline]
            if r.diff_truncated:
                out.append("（長いので途中まで）" if chat else f"（長いので途中まで。全部: {r.snapshot / 'diff.txt'}）")
        out.append("")
    return "\n".join(out)
