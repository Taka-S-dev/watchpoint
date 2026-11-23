"""Unattended checks from the Task Scheduler: schedule install, remove, status, run."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.markup import escape

from .. import schedule
from ..config import resolve_config_path
from .common import ConfigOpt, console, err, load_targets, schedule_app

TaskOpt = typer.Option(
    schedule.DEFAULT_TASK, "--name", help="タスク スケジューラでのタスク名（設定ファイルごとに分けるとき）"
)


@schedule_app.command("install")
def schedule_install(
    config: Path | None = ConfigOpt,
    every: int = typer.Option(5, "--every", min=1, max=1440, help="何分おきに動かすか"),
    name: str = TaskOpt,
    now: bool = typer.Option(True, "--now/--no-now", help="登録したらすぐ1回動かす"),
) -> None:
    """自分のユーザーで動くタスクを登録する（管理者権限・パスワードは要らない。サインイン中だけ動く）。

    毎回 `check --due --notify` と同じことをする。各見張りの every より短い間隔なら、時間が来た見張りだけをチェックする。
    """
    cfg, path, _ = load_targets(config)
    if os.name != "nt":
        err.print(
            "[red]タスク スケジューラは Windows だけです。cron などから `watchpoint schedule run` を呼んでください[/red]"
        )
        raise typer.Exit(2)
    if not cfg.notify:
        err.print("[yellow]notify: drop_folder が無いので、変更は status と画面でしか分かりません[/yellow]")
    try:
        command = schedule.install(name, path, every)
        if now:
            schedule.run_now(name)
    except schedule.ScheduleError as e:
        err.print(f"[red]登録できませんでした[/red]: {escape(str(e))}")
        raise typer.Exit(1)
    console.print(f"登録しました: タスク「{escape(name)}」 {every} 分おき")
    console.print(f"[dim]{escape(command)}[/dim]")
    console.print(
        "[dim]動いているかは `watchpoint schedule status` か画面の上に出ます。止めるときは `watchpoint schedule remove`。[/dim]"
    )


@schedule_app.command("remove")
def schedule_remove(name: str = TaskOpt) -> None:
    """登録したタスクを消す（取ってきたコピーや確認済みの状態は残る）。"""
    if schedule.remove(name):
        console.print(f"消しました: タスク「{escape(name)}」")
    else:
        console.print(f"タスク「{escape(name)}」はありません。")


@schedule_app.command("status")
def schedule_status(config: Path | None = ConfigOpt, name: str = TaskOpt) -> None:
    """タスクが登録されているか、最後にいつ動いて、どうだったか。"""
    _, path, data_dir = load_targets(config)
    task = schedule.query(name) if os.name == "nt" else None
    if task is None:
        console.print(f"タスク「{escape(name)}」は登録されていません。")
    else:
        on = "有効" if task["enabled"] else "[yellow]無効[/yellow]"
        console.print(f"タスク「{escape(name)}」: {on}・{task['every'] or '?'} 分おき")
        if str(path.resolve()) not in task["arguments"]:
            console.print(f"[yellow]  別の設定ファイルを見ています[/yellow]: {escape(task['arguments'])}")
    view = schedule.heartbeat_view(data_dir)
    style = {"ok": "green", "late": "bold red", "error": "bold red"}.get(view["state"], "dim")
    console.print(f"[{style}]{escape(view['text'])}[/]")
    beat = schedule.read_heartbeat(data_dir) or {}
    if beat.get("result") == "ok":
        console.print(
            f"[dim]  最後の回: チェック {beat.get('checked', 0)} 件・変更 {beat.get('changed', 0)} 件・"
            f"失敗 {beat.get('failed', 0)} 件・知らせ {beat.get('sent', 0)} 件[/dim]"
        )
    log = data_dir / schedule.RUN_LOG
    if log.exists() and log.stat().st_size:
        console.print(f"[dim]  出力の記録: {escape(str(log))}[/dim]")


@schedule_app.command("run", hidden=True)
def schedule_run(
    config: Path | None = ConfigOpt,
    every: int = typer.Option(5, "--every", help="登録したときの間隔（遅れの判定に使う）"),
) -> None:
    """タスク スケジューラから呼ばれる1回分。結果は heartbeat.json に残し、画面には何も出さない。"""
    raise typer.Exit(schedule.run(resolve_config_path(config), every))
