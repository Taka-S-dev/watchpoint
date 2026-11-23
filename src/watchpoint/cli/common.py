"""Shared by every command: the Typer apps, common options, output styles and config loading."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax

from .. import __version__
from ..check import Status
from ..config import Config, ConfigError, Target, load_config, resolve_config_path
from ..lock import Busy, data_lock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

app = typer.Typer(
    help="見張っているファイルやページのコピーを取ってきて、確認済みにしたときから何が変わったかを出す。元のファイルは読むだけ。",
    no_args_is_help=True,
    add_completion=False,
)
secret_app = typer.Typer(
    help="トークンや API キーを Windows の資格情報マネージャーに置く（設定ファイルには書かない）。",
    no_args_is_help=True,
)
app.add_typer(secret_app, name="secret")
schedule_app = typer.Typer(
    help="Windows のタスク スケジューラで、間隔の過ぎた見張りを自動でチェックして知らせる（画面は出ない）。",
    no_args_is_help=True,
)
app.add_typer(schedule_app, name="schedule")
rule_app = typer.Typer(
    help="値のルール（空にしない・決まった値だけ）を見る・足す・外す。合わない行は「要注意」として毎回出る。",
    no_args_is_help=True,
)
app.add_typer(rule_app, name="rule")
skill_app = typer.Typer(
    help="AI エージェント（Copilot CLI・Claude Code など）が watchpoint を使うためのスキル（SKILL.md）を入れる。",
    no_args_is_help=True,
)
app.add_typer(skill_app, name="skill")

console = Console()
err = Console(stderr=True)

ConfigOpt = typer.Option(
    None, "--config", "-c", help="設定ファイル（既定: $WATCHPOINT_CONFIG か ~/.watchpoint/targets.yaml）"
)
TagOpt = typer.Option(None, "--tag", "-t", help="このタグの付いた見張りだけ（何回でも指定可）")

STATUS_STYLE = {
    Status.CHANGED: "bold yellow",
    Status.FIRST: "cyan",
    Status.UNCHANGED: "green",
    Status.FILTERED: "green",
    Status.RETRYING: "yellow",
    Status.FAILED: "bold red",
    Status.NOT_DUE: "dim",
    Status.SKIPPED: "dim",
}
CHANGE_STYLE = {"追加": "green", "削除": "red", "変更": "yellow", "列　": "magenta"}


class Format(StrEnum):
    table = "table"
    md = "md"
    json = "json"


# --------------------------------------------------------------------------- helpers


def load_targets(config_path: Path | None, check_commands: bool = True) -> tuple[Config, Path, Path]:
    path = resolve_config_path(config_path)
    try:
        cfg = load_config(path, check_commands=check_commands)
    except ConfigError as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(2)
    return cfg, path, cfg.data_path(path)


@contextmanager
def data_locked(data_dir: Path):
    try:
        with data_lock(data_dir):
            yield
    except Busy as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(4)


def select_targets(cfg: Config, names: list[str] | None, tags: list[str] | None = None) -> list[Target]:
    targets = cfg.targets
    if tags:
        targets = [t for t in targets if set(tags) & set(t.tags)]
        if not targets:
            known = sorted({tag for t in cfg.targets for tag in t.tags})
            err.print(
                f"[red]タグ {escape(', '.join(tags))} の見張りがありません[/red]（あるタグ: {escape(', '.join(known)) or 'なし'}）"
            )
            raise typer.Exit(2)
    if not names:
        return targets
    by_name = {t.name: t for t in targets}
    picked, unknown = [], []
    for n in names:
        if n in by_name:
            picked.append(by_name[n])
            continue
        partial = [t for t in targets if n in t.name]
        if len(partial) == 1:
            picked.append(partial[0])
        else:
            unknown.append(n)
    if unknown:
        err.print(f"[red]見張りが見つからない（または複数に当たる）: {escape(', '.join(unknown))}[/red]")
        err.print("  あるもの: " + escape(", ".join(t.name for t in targets)))
        raise typer.Exit(2)
    return picked


def one_target(cfg: Config, name: str) -> Target:
    return select_targets(cfg, [name])[0]


class nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def print_changes(text: str, keyed: bool) -> None:
    if not keyed or text.startswith("---"):
        console.print(Syntax(text, "diff", word_wrap=True, background_color="default"))
        return
    for line in text.splitlines():
        head = line[:2]
        if head in CHANGE_STYLE:
            console.print(f"[{CHANGE_STYLE[head]}]{escape(head)}[/] {escape(line[3:])}")
        else:
            console.print(f"[bold]{escape(line)}[/bold]")


# --------------------------------------------------------------------------- main


def _version(value: bool) -> None:
    if value:
        console.print(f"watchpoint {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="版を出す"),
) -> None:
    pass
