"""Allowing the commands a target runs: allow."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .common import ConfigOpt, app, console, err, load_targets, one_target


def _allowed_table(config: Path | None) -> None:
    from .. import commands

    try:
        targets = load_targets(config)[0].targets
    except typer.Exit:
        targets = []
    entries = commands.allowed_list()
    if not entries:
        console.print("許可したコマンドはありません。")
        return
    table = Table(show_lines=False)
    for col in ("コマンド", "使っている見張り", "許可した日時"):
        table.add_column(col, overflow="fold")
    for e in entries:
        used = [t.name for t in targets if t.command == e["command"]]
        table.add_row(
            escape(e["line"]), escape("、".join(used)) or "[dim]（どの見張りも使っていない）[/dim]", e["allowed"]
        )
    console.print(table)
    console.print("[dim]取り消すには `watchpoint allow <name> --revoke`。画面の「設定」でも見られる。[/dim]")


@app.command()
def allow(
    name: str | None = typer.Argument(None, help="見張りの name（一部でも可）。省略すると、許可したコマンドの一覧"),
    config: Path | None = ConfigOpt,
    yes: bool = typer.Option(False, "--yes", "-y", help="聞かずに許可する（中身を確かめたあとで）"),
    revoke: bool = typer.Option(False, "--revoke", help="その見張りのコマンドの許可を取り消す"),
    unsafe: bool = typer.Option(
        False, "--unsafe", help="読むだけと分かっていないコマンドを、警告を読んだうえで許可する（名前の入力が要る）"
    ),
) -> None:
    """コマンドの出力を見張る見張りの、コマンドを許可する。name なしで一覧、--revoke で取り消し。"""
    from .. import commands
    from ..commands import command_line

    if name is None:
        _allowed_table(config)
        return
    # read without the read-only check: this is where a command outside it can be allowed (or revoked)
    cfg, _, _ = load_targets(config, check_commands=False)
    t = one_target(cfg, name)
    if not t.command:
        err.print(f"[red]「{escape(t.name)}」はコマンドで見張っていません[/red]")
        raise typer.Exit(1)
    if revoke:
        gone = commands.revoke([commands.fingerprint(t.command)])
        console.print("取り消しました。" if gone else "許可されていませんでした。")
        return
    console.print(
        f"「{escape(t.name)}」は、次のコマンドを動かします:\n  [bold]{escape(command_line(t.command))}[/bold]"
    )
    if commands.is_allowed(t.command):
        console.print("[green]このコマンドは許可済みです[/green]")
        return
    problem = commands.safety_problem(t.command)
    if problem and not unsafe:
        err.print(f"[red]{escape(commands.unsafe_message(t.name, problem))}[/red]")
        raise typer.Exit(1)
    if problem:
        _confirm_unsafe(t.name, problem)
    elif not yes and not typer.confirm("このコマンドを、あなたの権限で動かしてよいですか？", default=False):
        console.print("許可しませんでした。")
        raise typer.Exit(1)
    commands.allow(t.command, t.name, unsafe=bool(problem))
    console.print("[green]許可しました。[/green]コマンドを1文字でも変えると、もう一度許可が要ります。")


def _confirm_unsafe(name: str, problem: str) -> None:
    """The one way to run a command outside the read-only list: read this, and type the name."""
    console.print(
        Panel(
            f"[bold]{escape(problem)}[/bold]\n\n"
            "このコマンドが書き込みや削除をしないかは、watchpoint には分かりません。許可すると、\n"
            "自動のチェックのたびに（既定では 30 分ごとに）、あなたの権限でくり返し動きます。\n"
            "自分で書いたコマンドで、何をするか分かっているときだけ許可してください。",
            title="読むだけと分かっていないコマンド",
            border_style="red",
        )
    )
    typed = typer.prompt(
        f"許可するなら、見張りの名前「{name}」をそのまま入力してください", default="", show_default=False
    )
    if typed.strip() != name:
        console.print("名前が違うので、許可しませんでした。")
        raise typer.Exit(1)
