"""Looking back without fetching: status, diff, show, history, log."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

from .. import schedule
from ..check import VIA, has_pending
from ..report import changes_between, generations, latest_changes, next_due
from ..store import TargetStore
from .common import (
    STATUS_STYLE,
    ConfigOpt,
    TagOpt,
    app,
    console,
    err,
    load_targets,
    one_target,
    print_changes,
    select_targets,
)


@app.command()
def status(config: Path | None = ConfigOpt, tag: list[str] | None = TagOpt) -> None:
    """見張りごとの最後のチェック結果を出す（取りに行かない）。"""
    cfg, path, data_dir = load_targets(config)
    targets = select_targets(cfg, None, tag)
    states = {t.name: TargetStore(data_dir, t.store_key).load() for t in targets}
    with_next = any(t.every for t in targets)

    def short(stamp: str | None) -> str:
        return stamp[5:16].replace("-", "/", 1) if stamp else "-"

    table = Table()
    table.add_column("見張り", overflow="fold", min_width=10)
    table.add_column("結果", no_wrap=True)
    table.add_column("未確認", no_wrap=True)
    table.add_column("最終チェック", no_wrap=True)
    table.add_column("最後の変更", no_wrap=True)
    if with_next:
        table.add_column("次", no_wrap=True)
    for t in targets:
        s = states[t.name]
        res = s.last_status or "未確認"
        style = next((v for k, v in STATUS_STYLE.items() if k.value == res), "")
        name = escape(t.name) + ("" if t.enabled else " [dim](無効)[/dim]")
        pending = "[yellow]あり[/]" if has_pending(s) else ""
        row = [name, f"[{style}]{res}[/]" if style else res, pending, short(s.last_checked), short(s.last_changed)]
        if with_next:
            row.append(next_due(t, s, cfg.check_hours) or "-")
        table.add_row(*row)
    console.print(table)
    for t in targets:
        if states[t.name].last_error:
            console.print(f"[red]{escape(t.name)}[/red]: {escape(states[t.name].last_error)}")
    view = schedule.heartbeat_view(data_dir)
    if view["state"] in ("late", "error"):
        console.print(f"[bold red]{escape(view['text'])}[/bold red]")
    elif view["state"] == "ok":
        console.print(f"[dim]{escape(view['text'])}[/dim]")
    console.print(f"[dim]設定: {escape(str(path))}[/dim]")


def _gen(store: TargetStore, state, number: int | None) -> str:
    try:
        return store.generation(state, number) if number else state.snapshot
    except IndexError as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(2)


@app.command()
def diff(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"),
    config: Path | None = ConfigOpt,
    from_gen: int | None = typer.Option(None, "--from", help="比べる元（history の番号）"),
    to_gen: int | None = typer.Option(None, "--to", help="比べる先（history の番号。既定は最新）"),
) -> None:
    """未確認の変更を出す。--from / --to で任意の履歴どうしも比べられる（取りに行かない）。"""
    cfg, _, data_dir = load_targets(config)
    target = one_target(cfg, name)
    store = TargetStore(data_dir, target.store_key)
    state = store.load()
    if not state.history:
        console.print("まだ履歴がありません。")
        return
    if from_gen or to_gen:
        a = _gen(store, state, from_gen) if from_gen else state.baseline
        b = _gen(store, state, to_gen)
        ch = changes_between(target, cfg, store, state, a, b)
        console.print(f"[bold]{escape(target.name)}[/bold]  {escape(ch.title)}  {escape(ch.message)}")
        if ch.text:
            print_changes(ch.text, ch.keyed)
        return
    ch = latest_changes(target, cfg, store, state)
    if ch:
        console.print(f"[bold]{escape(target.name)}[/bold]  {escape(ch.title)}  {escape(ch.message)}")
        print_changes(ch.text, ch.keyed)
        return
    console.print("まだ変更は見つかっていません。")


@app.command()
def show(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"),
    config: Path | None = ConfigOpt,
    gen: int | None = typer.Option(None, "--gen", "-g", help="履歴の番号（history の番号。既定は最新）"),
) -> None:
    """履歴のある時点のテキストを出す（取りに行かない）。"""
    cfg, _, data_dir = load_targets(config)
    target = one_target(cfg, name)
    store = TargetStore(data_dir, target.store_key)
    state = store.load()
    if not state.history:
        console.print("まだ履歴がありません。")
        raise typer.Exit(1)
    snap_id = _gen(store, state, gen)
    text = store.read_text(snap_id)
    if text is None:
        err.print("[red]その時点のテキストがありません[/red]")
        raise typer.Exit(1)
    print(text, end="")


@app.command()
def history(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"),
    config: Path | None = ConfigOpt,
) -> None:
    """残っている履歴の一覧（取りに行かない）。番号は diff --from / --to と show --gen で使う。"""
    cfg, _, data_dir = load_targets(config)
    target = one_target(cfg, name)
    store = TargetStore(data_dir, target.store_key)
    state = store.load()
    if not state.history:
        console.print("まだ履歴がありません。`watchpoint check` でチェックしてください。")
        return
    table = Table(title=escape(target.name))
    table.add_column("番号", justify="right")
    table.add_column("取得", no_wrap=True)
    table.add_column("経路", no_wrap=True)
    table.add_column("印", no_wrap=True)
    table.add_column("見つかった変更", overflow="fold")
    table.add_column("メモ", overflow="fold")
    for g in generations(store, state):
        table.add_row(str(g.number), g.when, g.via or "-", "・".join(g.marks), escape(g.summary), escape(g.note))
    console.print(table)


@app.command()
def log(
    config: Path | None = ConfigOpt,
    lines: int = typer.Option(30, "--lines", "-n", help="出す件数"),
    name: str | None = typer.Option(None, "--name", help="この見張り（name の一部）だけ"),
) -> None:
    """これまでの check と ack の記録（新しいものが下）。"""
    _, _, data_dir = load_targets(config)
    f = data_dir / "log.jsonl"
    if not f.exists():
        console.print("まだ記録がありません。")
        return
    keep: deque = deque(maxlen=lines)
    with f.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if name and name not in e.get("name", ""):
                continue
            keep.append(e)
    table = Table()
    for col in ("時刻", "見張り", "経路", "結果", "内容"):
        table.add_column(col, overflow="fold", no_wrap=col in ("時刻", "経路"))
    for e in keep:
        table.add_row(
            e.get("time", "")[5:16].replace("T", " "),
            escape(e.get("name", "")),
            VIA.get(e.get("via", ""), ""),
            escape(e.get("status", "")),
            escape(e.get("message", "")),
        )
    console.print(table)
