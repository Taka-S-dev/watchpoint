"""Credentials kept in the Windows Credential Manager: secret set, delete, check."""

from __future__ import annotations

import getpass
import sys

import typer
from rich.markup import escape

from ..credentials import MissingSecret, delete_secret, get_secret, set_secret
from .common import console, err, secret_app


@secret_app.command("set")
def secret_set(
    name: str = typer.Argument(..., help="設定ファイルの auth: secret に書く名前"),
    stdin: bool = typer.Option(False, "--stdin", help="値を標準入力から読む（画面に出さずに渡すとき）"),
) -> None:
    """トークンや API キーを登録する（入力は画面に出ない）。"""
    value = sys.stdin.readline().rstrip("\r\n") if stdin else getpass.getpass(f"{name} の値: ")
    if not value.strip():
        err.print("[red]空の値は登録しません[/red]")
        raise typer.Exit(1)
    try:
        set_secret(name, value)  # surrounding spaces from copy-paste are dropped
    except ValueError as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1)
    console.print(f"登録しました: {escape(name)}（Windows の資格情報マネージャー、サービス名 watchpoint）")


@secret_app.command("delete")
def secret_delete(name: str = typer.Argument(...)) -> None:
    """登録した値を消す。"""
    console.print("消しました。" if delete_secret(name) else "登録されていませんでした。")


@secret_app.command("check")
def secret_check(name: str = typer.Argument(...)) -> None:
    """登録されているかを確かめる（値は出さない）。"""
    try:
        v = get_secret(name)
    except MissingSecret as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1)
    console.print(f"登録されています: {escape(name)}（{len(v)} 文字）")
