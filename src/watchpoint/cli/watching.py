"""Setting up and checking: init, list, check, ack."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from ..check import Result, Stale, Status, acknowledge, check_all, send_notifications
from ..config import TEMPLATE, resolve_config_path
from ..report import as_markdown, kind_label
from .common import (
    STATUS_STYLE,
    ConfigOpt,
    Format,
    TagOpt,
    app,
    console,
    data_locked,
    err,
    load_targets,
    nullctx,
    print_changes,
    select_targets,
)


@app.command()
def init(config: Path | None = ConfigOpt) -> None:
    """設定ファイルのひな形を作る。"""
    path = resolve_config_path(config)
    if path.exists():
        err.print(f"[yellow]もうあります: {path}[/yellow]（上書きしない）")
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    console.print(f"作りました: [bold]{escape(str(path))}[/bold]")
    console.print(
        "`watchpoint gui` の「見張りを追加」で、ファイルの中を見ながら追加できます。"
        "手で書くときは、サンプル（どれも無効）を書き換えて `watchpoint doctor` で確かめてください。"
    )


@app.command("list")
def list_cmd(config: Path | None = ConfigOpt, tag: list[str] | None = TagOpt) -> None:
    """設定されている見張りの一覧（name・比べ方・タグ・間隔・場所）。"""
    cfg, path, _ = load_targets(config)
    table = Table(caption=f"設定: {escape(str(path))}")
    table.add_column("name", no_wrap=True)
    table.add_column("比べ方", no_wrap=True)
    table.add_column("タグ")
    table.add_column("間隔", no_wrap=True)
    table.add_column("場所", overflow="fold")
    for t in select_targets(cfg, None, tag):
        name = escape(t.name) + ("" if t.enabled else " [dim](無効)[/dim]")
        table.add_row(name, escape(kind_label(t)), escape(", ".join(t.tags)), t.every or "-", escape(t.source))
    console.print(table)


# --------------------------------------------------------------------------- check / ack


@app.command()
def check(
    names: list[str] | None = typer.Argument(None, help="チェックする見張りの name（一部でも可）。省略するとすべて"),
    config: Path | None = ConfigOpt,
    tag: list[str] | None = TagOpt,
    fmt: Format = typer.Option(
        Format.table, "--format", "-f", help="table: 画面向け / md: AI に読ませる向け / json: プログラム向け"
    ),
    changed_only: bool = typer.Option(
        False, "--changed-only", "-C", help="変更あり・取得失敗の見張りだけを出す（AI に渡す量を減らす）"
    ),
    due: bool = typer.Option(False, "--due", help="every の間隔が過ぎた見張りだけをチェックする"),
    ack: bool = typer.Option(False, "--ack", help="出した変更を、そのまま確認済みにする"),
    send: bool = typer.Option(
        False, "--notify", help="新しい変更と取得失敗を、notify の drop_folder に知らせとして置く"
    ),
    exit_code: bool = typer.Option(
        False, "--exit-code", help="終了コードで結果を返す（0: 変更なし / 1: 変更あり / 3: 取得失敗のみ）"
    ),
) -> None:
    """見張りのコピーを取ってきて、確認済みの内容と比べる（チェック）。

    変更は `watchpoint ack` で確認済みにするまで、毎回出し続ける。
    """
    cfg, _, data_dir = load_targets(config)
    targets = select_targets(cfg, names, tag)
    if not targets:
        err.print(
            "[yellow]見張りがありません。`watchpoint gui` の「見張りを追加」か、設定ファイルの targets で足してください。[/yellow]"
        )
        raise typer.Exit(1)
    if send and not cfg.notify:
        err.print("[red]--notify には、設定ファイルの notify: drop_folder が要ります[/red]")
        raise typer.Exit(2)

    with data_locked(data_dir):
        with console.status(f"{len(targets)} 件をチェック中…") if fmt is Format.table else nullctx():
            results: list[Result] = check_all(targets, cfg, data_dir, due_only=due)
        changed = [r for r in results if r.status is Status.CHANGED]
        sent = send_notifications(results, cfg, data_dir) if send else 0
        if ack:
            for r in changed:
                acknowledge(r.target, cfg, data_dir)

    shown = [r for r in results if r.needs_attention] if changed_only else results
    if fmt is Format.md:
        print(as_markdown(shown, total=len(results)))
    elif fmt is Format.json:
        print(json.dumps([r.to_dict() for r in shown], ensure_ascii=False, indent=2))
    else:
        if shown:
            _print_table(shown)
        _print_summary(results)
        for r in shown:
            if r.status is Status.CHANGED and r.diff:
                _print_detail(r)
        for r in shown:
            if r.alerts:
                console.print(f"[bold yellow]要注意[/bold yellow] {escape(r.target.name)}")
                for a in r.alerts:
                    console.print(f"  - {escape(a)}")
        if sent:
            console.print(f"[dim]{sent} 件を知らせとして置きました。[/dim]")
        if ack and changed:
            console.print(f"[dim]{len(changed)} 件を確認済みにしました。[/dim]")
        elif changed:
            console.print(
                "[dim]見終わったら `watchpoint ack`（name を指定しなければ全部）で確認済みにしてください。[/dim]"
            )

    if exit_code:
        if changed:
            raise typer.Exit(1)
        if any(r.status is Status.FAILED for r in results):
            raise typer.Exit(3)


@app.command("ack")
def ack_cmd(
    names: list[str] | None = typer.Argument(None, help="確認済みにする見張りの name（一部でも可）。省略するとすべて"),
    config: Path | None = ConfigOpt,
    tag: list[str] | None = TagOpt,
    note: str | None = typer.Option(None, "--note", "-m", help="この確認に残すメモ（history に出る）"),
    seen: str | None = typer.Option(
        None,
        "--seen",
        help="見た履歴の番号（check -f md / json に出る）。その後に新しい変更が届いていたら確認済みにしない。見張りは1つだけ",
    ),
) -> None:
    """最後に取ってきた状態を確認済みにする（次の check から、そこからの変更だけを出す）。"""
    cfg, _, data_dir = load_targets(config)
    targets = select_targets(cfg, names, tag)
    if seen and len(targets) != 1:
        err.print("[red]--seen は、見張りを1つに絞って使ってください[/red]")
        raise typer.Exit(2)
    try:
        with data_locked(data_dir):
            done = [t.name for t in targets if acknowledge(t, cfg, data_dir, note=note, seen=seen)]
    except Stale as e:
        err.print(
            f"[yellow]{escape(str(e))} には、見たあとに新しい変更が届いています。確認済みにしませんでした。"
            "`watchpoint diff` で見てから、もう一度確認済みにしてください[/yellow]"
        )
        raise typer.Exit(1)
    if done:
        console.print("確認済みにしました: " + escape("、".join(done)))
    else:
        console.print("確認済みにするものはありませんでした。")


def _print_table(results: list[Result]) -> None:
    table = Table(show_lines=False)
    table.add_column("見張り", no_wrap=True)
    table.add_column("状態", no_wrap=True)
    table.add_column("内容", overflow="fold")
    for r in results:
        table.add_row(escape(r.target.name), f"[{STATUS_STYLE[r.status]}]{r.status.value}[/]", escape(r.message))
    console.print(table)


def _print_summary(results: list[Result]) -> None:
    changed = sum(r.status is Status.CHANGED for r in results)
    failed = sum(r.status is Status.FAILED for r in results)
    console.print(f"変更あり {changed} 件 / 取得失敗 {failed} 件 / 全 {len(results)} 件")


def _print_detail(r: Result) -> None:
    body = []
    if r.target.points.strip():
        body.append("[bold]読むポイント[/bold]\n" + escape(r.target.points.strip()))
    if r.target.notify_when.strip():
        body.append("[bold]知らせる条件[/bold]\n" + escape(r.target.notify_when.strip()))
    if body:
        console.print(Panel("\n\n".join(body), title=escape(r.target.name), border_style="yellow"))
    else:
        console.print(f"[bold yellow]{escape(r.target.name)}[/bold yellow]")
    print_changes(r.diff, r.target.is_keyed)
    if r.inline:
        console.print("[bold]行の中の変化[/bold]")
        for line in r.inline:
            console.print("  " + escape(line))
    if r.diff_truncated:
        console.print(f"[dim]…差分が長いので途中まで。全部: {escape(str(r.snapshot / 'diff.txt'))}[/dim]")
