"""Trying settings and finding sources: preview, doctor, open, clean."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from ..check import build_text
from ..config import Target
from ..credentials import has_secret
from ..report import kind_label
from ..store import TargetStore, safe_name
from .common import ConfigOpt, TagOpt, app, console, data_locked, err, load_targets, one_target, select_targets


@app.command()
def preview(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"),
    config: Path | None = ConfigOpt,
    lines: int = typer.Option(40, "--lines", "-n", help="出す行数（0 ですべて）"),
) -> None:
    """取ってきてテキストにした結果を見る。保存も比較もしない（設定を試すとき用）。"""
    cfg, _, _ = load_targets(config)
    target = one_target(cfg, name)
    with tempfile.TemporaryDirectory(prefix="watchpoint-") as tmp:
        try:
            with console.status("取得中…"):
                text, _ = build_text(target, Path(tmp))
        except Exception as e:  # noqa: BLE001
            err.print(f"[red]取得できませんでした: {escape(str(e))}[/red]")
            raise typer.Exit(1)
    all_lines = text.splitlines()
    shown = all_lines if lines == 0 else all_lines[:lines]
    console.print(f"[bold]{escape(target.name)}[/bold]  比べ方: {escape(kind_label(target))} / 全 {len(all_lines)} 行")
    console.print(Panel(escape("\n".join(shown)) or "[dim](空)[/dim]", border_style="cyan"))
    if len(shown) < len(all_lines):
        console.print(f"[dim]…残り {len(all_lines) - len(shown)} 行（-n 0 ですべて）[/dim]")


@app.command()
def doctor(
    config: Path | None = ConfigOpt,
    tag: list[str] | None = TagOpt,
    fetch: bool = typer.Option(False, "--fetch", help="実際に取ってきて、テキストにできるかまで確かめる"),
) -> None:
    """設定と、各見張りの場所・資格情報・通知先を確かめる（保存しない）。"""
    cfg, path, data_dir = load_targets(config)
    console.print(f"[green]OK[/green] 設定ファイル: {escape(str(path))}（見張り {len(cfg.targets)} 件）")
    problems = 0
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write-test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        console.print(f"[green]OK[/green] データの置き場所: {escape(str(data_dir))}")
    except OSError as e:
        problems += 1
        console.print(f"[red]NG[/red] データの置き場所に書けません: {escape(str(data_dir))}（{escape(str(e))}）")
    if cfg.notify:
        folder = Path(cfg.notify.drop_folder)
        if folder.is_dir():
            console.print(f"[green]OK[/green] 通知の置き場所: {escape(str(folder))}")
        else:
            problems += 1
            console.print(
                f"[yellow]注意[/yellow] 通知の置き場所がまだありません: {escape(str(folder))}（--notify のときに作ります）"
            )

    table = Table()
    for col in ("見張り", "比べ方", "結果", "内容"):
        table.add_column(col, overflow="fold", no_wrap=col in ("比べ方", "結果"))
    for t in select_targets(cfg, None, tag):
        ok, msg = _diagnose(t, fetch)
        if not t.enabled:
            ok, msg = True, f"(無効) {msg}"
        problems += 0 if ok else 1
        table.add_row(
            escape(t.name), escape(kind_label(t)), "[green]OK[/green]" if ok else "[red]NG[/red]", escape(msg)
        )
    console.print(table)
    if problems:
        raise typer.Exit(1)


def _diagnose(t: Target, fetch: bool) -> tuple[bool, str]:
    if t.auth and not has_secret(t.auth.secret):
        return False, f"資格情報「{t.auth.secret}」がありません。`watchpoint secret set {t.auth.secret}`"
    kind = t.kind
    if kind == "file":
        p = Path(t.source)
        if not p.exists():
            return False, "見つかりません（パス、共有フォルダへの接続、VPN を確かめてください）"
        if not p.is_file():
            return False, "ファイルではありません"
        if not os.access(p, os.R_OK):
            return False, "読めません（アクセス権）"
        if Path(t.source).suffix.lower() in (".xls", ".doc", ".ppt"):
            return (
                True,
                "古い形式は中身を比べられません（変わったかどうかだけ）。.xlsx などで保存し直すと中身も比べられます",
            )
    elif kind == "folder":
        from ..textify import folder_listing

        try:
            n = len(folder_listing(t).splitlines()) - 1
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        return True, f"ファイル {n} 件"
    elif kind == "office":
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            return False, "pywin32 が入っていません（Windows でだけ使えます）"
    if fetch or kind in ("web", "json", "confluence", "download"):
        with tempfile.TemporaryDirectory(prefix="watchpoint-") as tmp:
            try:
                text, _ = build_text(t, Path(tmp))
            except Exception as e:  # noqa: BLE001
                return False, str(e)
        return True, f"取得できました（{len(text.splitlines())} 行）"
    return True, "場所は見つかりました（--fetch で取得まで確かめる）"


@app.command("open")
def open_cmd(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"),
    config: Path | None = ConfigOpt,
    text: bool = typer.Option(False, "--text", help="原本のコピーではなく、テキストにしたものを開く"),
    place: bool = typer.Option(
        False, "--place", help="コピーではなく、見張っている場所を開く（ファイルはフォルダで選ぶだけ）"
    ),
) -> None:
    """最後に取ってきたコピーを開く（元のファイルは開かない）。--place で見張っている場所を開く。"""
    cfg, _, data_dir = load_targets(config)
    target = one_target(cfg, name)
    if place:
        from ..viewing import PLACE_LABEL, open_place, place_of

        where = place_of(target)
        if where is None:
            err.print("[yellow]開く場所がありません。設定で link: に URL かフォルダを書くと開けます。[/yellow]")
            raise typer.Exit(1)
        open_place(*where)
        console.print(f"{PLACE_LABEL[where[0]]}: {escape(where[1])}")
        return
    store = TargetStore(data_dir, target.store_key)
    state = store.load()
    if not state.snapshot:
        err.print("[yellow]まだコピーがありません。`watchpoint check` を実行してください。[/yellow]")
        raise typer.Exit(1)
    snap = store.snapshot_dir(state.snapshot)
    originals = sorted(snap.glob("original*"))
    path = snap / "text.txt" if text or not originals else originals[0]
    if not path.exists():
        err.print(f"[red]見つかりません: {escape(str(path))}[/red]")
        raise typer.Exit(1)
    from ..viewing import show

    opened, direct = show(path, target.name, state.snapshot)
    what = "開きました" if direct else "エクスプローラーで場所を示しました（開くと何かが動く形式なので）"
    console.print(f"{what}（読み取り専用のコピー。元のファイルではない）: {escape(str(opened))}")


@app.command()
def clean(
    config: Path | None = ConfigOpt,
    yes: bool = typer.Option(False, "--yes", "-y", help="一覧を出すだけでなく、実際に消す"),
) -> None:
    """設定から消した見張りのデータ（取ってきたコピー）を片付ける。"""
    cfg, _, data_dir = load_targets(config)
    if not data_dir.exists():
        console.print("片付けるものはありません。")
        return
    known = {safe_name(t.store_key) for t in cfg.targets}
    orphans = [d for d in sorted(data_dir.iterdir()) if d.is_dir() and d.name not in known]
    if not orphans:
        console.print("片付けるものはありません。")
        return
    for d in orphans:
        console.print(f"  {escape(d.name)}")
    if not yes:
        console.print(
            f"[dim]設定に無い見張りのデータが {len(orphans)} 件あります。消すなら `watchpoint clean --yes`。[/dim]"
        )
        return
    with data_locked(data_dir):
        for d in orphans:
            shutil.rmtree(d)
    console.print(f"{len(orphans)} 件を消しました。")
