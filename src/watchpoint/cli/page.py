"""Opening the page: gui (its own window, or a tab with --browser)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.markup import escape

from ..config import TEMPLATE, ConfigError, load_config, resolve_config_path
from .common import ConfigOpt, app, console, err


def _no_console() -> bool:
    """Started without a console (pythonw), so nothing printed can be read."""
    return sys.stdout is None or sys.stderr is None


def _message_box(text: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "watchpoint", 0x30)  # MB_ICONWARNING


@app.command()
def gui(
    config: Path | None = ConfigOpt,
    browser: bool = typer.Option(
        False, "--browser", help="専用の窓ではなく、いつものブラウザで開く（この PC の中だけのサーバーを立てる）"
    ),
    port: int = typer.Option(0, "--port", "-p", help="--browser で使うポート（既定: 空いているもの）"),
    no_browser: bool = typer.Option(False, "--no-browser", help="サーバーだけ立てて URL を出す（--browser の一種）"),
    debug: bool = typer.Option(False, "--debug", help="窓で開発者ツールと右クリックを使えるようにする", hidden=True),
) -> None:
    """一覧・変更・履歴を画面で見て、チェックする・確認済みにする（この PC の中だけで動く）。"""
    import webbrowser

    from ..app import App
    from ..gui import launch_url, make_server, pick_path

    path = resolve_config_path(config)
    if not path.exists():  # the page is where the first target gets added
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE, encoding="utf-8")
        console.print(f"設定ファイルを作りました: [bold]{escape(str(path))}[/bold]")
    try:
        load_config(path)
    except ConfigError as e:  # still open: the page can show the mistake and fix it
        err.print(f"[yellow]{escape(str(e))}[/yellow]\n画面の「設定」で直せます。")

    if not (browser or no_browser):
        from .. import desktop

        try:
            desktop.run(path, debug=debug)
            return
        except desktop.Unavailable as e:
            if _no_console():  # started by gui.bat with pythonw: a server nobody can see or stop must not start
                _message_box(f"{e}\n\nターミナルで watchpoint gui --browser を実行すると、ブラウザで開けます。")
                raise typer.Exit(1)
            err.print(f"[yellow]{escape(str(e))}[/yellow]\nかわりにブラウザで開きます。")
    elif _no_console():
        _message_box("ブラウザで開くときは、ターミナルで watchpoint gui --browser を実行してください。")
        raise typer.Exit(2)

    gui_app = App(path, picker=pick_path)
    try:
        server = make_server(gui_app, port)
    except OSError as e:
        err.print(f"[red]ポート {port} を使えません: {escape(str(e))}[/red]")
        raise typer.Exit(2)
    url = launch_url(gui_app, server)
    console.print(f"開いています: [bold]{url}[/bold]（このターミナルで Ctrl+C を押すと止まります）")
    if not no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    console.print("止めました。")
