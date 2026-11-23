"""The package's own shape: every source file is kept by git, and modules import each other one way only."""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "watchpoint"


def test_every_source_file_is_kept_by_git() -> None:
    """.gitignore lets through only listed paths: a new folder left off the list would be dropped from commits,
    and ruff, which follows .gitignore, would skip it without a word."""
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    files = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "src").rglob("*")
        if p.suffix in (".py", ".html", ".md", ".css", ".js")
    ]
    files = [f for f in files if "__pycache__" not in f]
    r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", *files], capture_output=True, text=True)
    assert r.stdout.split() == [], "add these to .gitignore's list"


def _top_level_imports(path: Path, package: list[str]) -> set[str]:
    out = set()
    for n in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(n, ast.ImportFrom) and n.level:  # `if TYPE_CHECKING:` blocks are not at top level
            base = package[: len(package) - (n.level - 1)]
            names = [n.module] if n.module else [a.name for a in n.names]
            out |= {".".join([*base, *m.split(".")]) for m in names}
    return out


def test_modules_do_not_import_each_other_in_a_circle() -> None:
    graph = {}
    for p in PKG.rglob("*.py"):
        parts = list(p.relative_to(PKG).with_suffix("").parts)
        package = parts[:-1] if p.name != "__init__.py" else parts[:-1]
        name = ".".join(parts[:-1] if p.name == "__init__.py" else parts)
        graph[name] = _top_level_imports(p, package if p.name != "__init__.py" else parts[:-1])
    graph = {m: {d for d in ds if d in graph and d != m} for m, ds in graph.items()}
    seen: dict[str, int] = {}

    def visit(m: str, path: list[str]) -> None:
        seen[m] = 1
        for d in graph[m]:
            assert seen.get(d) != 1, " -> ".join([*path, m, d])
            if d not in seen:
                visit(d, [*path, m])
        seen[m] = 2

    for m in graph:
        if m not in seen:
            visit(m, [])


@pytest.mark.parametrize(
    ("lower", "upper"),
    [("config", "check"), ("config", "app"), ("fetchers", "check"), ("fetchers", "store"),
     ("textify", "check"), ("check", "app"), ("check", "gui"), ("check", "cli"), ("app", "gui"), ("app", "cli")],
)  # fmt: skip
def test_lower_layers_never_import_upper_ones(lower: str, upper: str) -> None:
    """Data and core never reach up into the page or the commands, so either can change on its own."""
    paths = [PKG / f"{lower}.py"] if (PKG / f"{lower}.py").exists() else list((PKG / lower).rglob("*.py"))
    for p in paths:
        text = p.read_text(encoding="utf-8")
        assert f"from .{upper} import" not in text and f"from ..{upper} import" not in text, p.name
        assert f"from . import {upper}" not in text and f"from .. import {upper}" not in text, p.name


def test_commands_are_listed_in_the_order_of_use() -> None:
    """cli/__init__.py imports the command modules in this order; sorting those imports would reorder --help."""
    from watchpoint.cli import app

    names = [c.name or c.callback.__name__ for c in app.registered_commands]
    assert names == [
        "init", "list", "check", "ack", "status", "diff", "show", "history", "log", "allow",
        "preview", "doctor", "open", "clean", "gui", "fields", "expect",
    ]  # fmt: skip
    assert [g.name for g in app.registered_groups] == ["secret", "schedule", "rule", "skill"]
