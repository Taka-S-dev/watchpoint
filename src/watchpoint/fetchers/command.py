"""The output of a command the person allowed."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..textify import command_output_to_text
from .base import Fetched, sha256_text

if TYPE_CHECKING:
    from ..config import Target


def fetch(target: Target, workdir: Path) -> Fetched:
    from ..commands import run

    out = run(target.command or [], target.name, workdir / "run", target.timeout)
    (workdir / "original.txt").write_text(out, encoding="utf-8")
    text = command_output_to_text(out, target)
    return Fetched(text, sha256_text(text))
