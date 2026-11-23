"""Warnings the person sets up: fields, rule add/list/remove, expect."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

from ..config import resolve_config_path
from .common import ConfigOpt, app, console, err, load_targets, one_target, rule_app


def _app_for(config: Path | None):
    """The page's operations, for editing the config the same careful way (checked, .bak, line endings kept)."""
    from ..app import App

    return App(resolve_config_path(config))


def _api(call):
    from ..app import ApiError

    try:
        return call()
    except ApiError as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1)


@app.command()
def fields(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"),
    config: Path | None = ConfigOpt,
) -> None:
    """最新のコピーの列（項目）ごとに、空の行の数と値の種類を出す（ルールを考えるとき用。取りに行かない）。"""
    cfg, _, _ = load_targets(config)
    target = one_target(cfg, name)
    r = _api(lambda: _app_for(config).fields(target.name))
    table = Table(title=escape(target.name))
    for col in ("項目", "行", "空", "空の行（例）", "値（多い順）"):
        table.add_column(col, overflow="fold")
    for f in r["fields"]:
        values = "、".join(f"{v['value']}（{v['count']}）" for v in f["values"])
        more = f" ほか {f['distinct'] - len(f['values'])} 種類" if f["distinct"] > len(f["values"]) else ""
        table.add_row(
            escape(f["field"]), str(f["rows"]), str(f["empty"]), escape("、".join(f["empty_in"])), escape(values + more)
        )
    console.print(table)


def _split_when(when: list[str] | None) -> dict[str, str] | None:
    out = {}
    for w in when or []:
        col, sep, value = w.partition("=")
        if not sep or not col.strip() or not value.strip():
            raise typer.BadParameter(f"--when は「列=値」の形で書いてください: {w}")
        out[col.strip()] = value.strip()
    return out or None


@rule_app.command("add")
def rule_add(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"),
    field: str = typer.Option(..., "--field", "-f", help="列・項目の名前（セルを見る見張りなら、セルに付けた名前）"),
    required: bool = typer.Option(False, "--required", help="空にしない"),
    one_of: str | None = typer.Option(None, "--one-of", help="この値のどれか（カンマ区切り）"),
    pattern: str | None = typer.Option(None, "--pattern", help="この形（正規表現）"),
    when: list[str] | None = typer.Option(None, "--when", help="この行だけ（列=値。何回でも）"),
    message: str | None = typer.Option(None, "--message", "-m", help="警告の文言（省略すると自動）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="足さずに、いま何件が引っかかるかだけ見る"),
    config: Path | None = ConfigOpt,
) -> None:
    """値のルールを足す。足す前に、いまのデータで引っかかる行を見せる。"""
    cfg, _, _ = load_targets(config)
    target = one_target(cfg, name)
    rule = {
        "field": field, "required": required, "one_of": [v.strip() for v in one_of.split(",") if v.strip()] if one_of else None,
        "pattern": pattern, "when": _split_when(when), "message": message,
    }  # fmt: skip
    a = _app_for(config)
    found = _api(lambda: a.try_rule(target.name, rule))["alerts"]
    console.print(f"いまのデータでは {len(found)} 件が引っかかります" + ("。" if found else "（いまは問題なし）。"))
    for line in found[:10]:
        console.print(f"  - {escape(line)}")
    if dry_run:
        return
    _api(lambda: a.add_rule(target.name, rule))
    console.print(
        f"[green]「{escape(target.name)}」にルールを足しました。[/green]次のチェックから「要注意」として出ます。"
    )


@rule_app.command("list")
def rule_list(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"), config: Path | None = ConfigOpt
) -> None:
    """見張りのルールと更新の期限、いまの要注意を出す。"""
    cfg, path, _ = load_targets(config)
    target = one_target(cfg, name)
    detail = _api(lambda: _app_for(config).detail(target.name))
    console.print(f"[bold]{escape(target.name)}[/bold]")
    console.print(f"更新の期限: {escape(detail['expect_update'] or 'なし')}")
    if not detail["rules"]:
        console.print("ルール: なし")
    for i, r in enumerate(detail["rules"], 1):
        console.print(f"  {i}. {escape(_rule_words(r))}")
    for a in detail["alerts"]:
        console.print(f"[yellow]要注意[/yellow] {escape(a)}")


def _rule_words(r: dict) -> str:
    parts = []
    if r.get("required"):
        parts.append("空にしない")
    if r.get("one_of"):
        parts.append(f"{'・'.join(r['one_of'])} のどれか")
    if r.get("pattern"):
        parts.append(f"形: {r['pattern']}")
    when = "".join(
        f"（{c} が {v if isinstance(v, str) else '・'.join(v)} の行）" for c, v in (r.get("when") or {}).items()
    )
    msg = f"／「{r['message']}」" if r.get("message") else ""
    return f"{r['field']}: {'、'.join(parts)}{when}{msg}"


@rule_app.command("remove")
def rule_remove(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"),
    number: int = typer.Argument(..., help="rule list の番号"),
    config: Path | None = ConfigOpt,
) -> None:
    """ルールを外す。"""
    cfg, _, _ = load_targets(config)
    target = one_target(cfg, name)
    _api(lambda: _app_for(config).remove_rule(target.name, number - 1))
    console.print("外しました。")


@app.command()
def expect(
    name: str = typer.Argument(..., help="見張りの name（一部でも可）"),
    when: str | None = typer.Argument(None, help="「毎週金 17:00」「平日 9:00」「毎月25日 17:00」「3d」など"),
    clear: bool = typer.Option(False, "--clear", help="期限をなくす"),
    config: Path | None = ConfigOpt,
) -> None:
    """更新の期限を決める。期限までに変わっていなければ「要注意」として出す。"""
    if not clear and not when:
        raise typer.BadParameter("期限を書くか、--clear を付けてください")
    cfg, _, _ = load_targets(config)
    target = one_target(cfg, name)
    _api(lambda: _app_for(config).set_expect_update(target.name, None if clear else when))
    console.print(
        "期限をなくしました。"
        if clear
        else f"「{escape(target.name)}」の更新の期限を「{escape(when or '')}」にしました。"
    )
