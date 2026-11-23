"""The agent skill: skill install, uninstall, show."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.markup import escape

from .common import console, err, skill_app

PlaceOpt = typer.Option(
    None, "--to", help="入れる先: agents（Copilot CLI ほか）/ claude（Claude Code）。省略すると両方。何回でも指定可"
)
DirOpt = typer.Option(None, "--dir", help="この skills フォルダに入れる（リポジトリの .github/skills など）")


@skill_app.command("install")
def skill_install(
    to: list[str] | None = PlaceOpt,
    folder: Path | None = DirOpt,
    force: bool = typer.Option(False, "--force", help="watchpoint が書いたものではない SKILL.md も上書きする"),
) -> None:
    """スキルを入れる（もう入っていれば、この版の内容に更新する）。"""
    from .. import agent_skill

    try:
        dests = agent_skill.destinations(to, folder)
    except ValueError as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(2)
    failed = False
    for dest in dests:
        try:
            what = agent_skill.install(dest, force)
        except (FileExistsError, OSError) as e:
            err.print(f"[red]{escape(str(e))}[/red]")
            failed = True
            continue
        label = {"installed": "入れました", "updated": "更新しました", "unchanged": "最新です"}[what]
        console.print(f"{label}: {escape(str(dest))}")
    if not failed:
        console.print(
            "[dim]AI エージェントに「watchpoint の見張りを全部チェックして要約して」のように頼めます。"
            "watchpoint を新しくしたら、もう一度 `watchpoint skill install`。[/dim]"
        )
    raise typer.Exit(1 if failed else 0)


@skill_app.command("uninstall")
def skill_uninstall(to: list[str] | None = PlaceOpt, folder: Path | None = DirOpt) -> None:
    """入れたスキルを消す（watchpoint が書いたものだけ）。"""
    from .. import agent_skill

    for dest in agent_skill.destinations(to, folder):
        try:
            gone = agent_skill.uninstall(dest)
        except FileExistsError as e:
            err.print(f"[red]{escape(str(e))}[/red]")
            continue
        console.print(f"{'消しました' if gone else '入っていません'}: {escape(str(dest))}")


@skill_app.command("show")
def skill_show() -> None:
    """スキルの中身を出す（エージェントが読む内容）。"""
    from .. import agent_skill

    sys.stdout.write(agent_skill.text())
