"""Fetching a target: each kind of source has its own module, and none of them compares or stores.

A fetcher returns the text to compare, a digest of what it fetched, and optionally a cheap probe
(size+mtime, a page version) that lets the next check skip the fetch when nothing changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from . import command, confluence, download, file, json_api, office, web
from .base import UNSUPPORTED_MARK, Fetched, FileBusy, retrying, sha256_text

if TYPE_CHECKING:
    from ..config import Target

__all__ = ["UNSUPPORTED_MARK", "Fetched", "FileBusy", "fetch", "probe", "sha256_text"]


def probe(target: Target) -> str | None:
    """A cheap fingerprint taken without fetching. None means "always fetch"."""
    if target.kind == "file":
        return file.probe(target)
    if target.kind == "confluence":
        return confluence.probe(target)
    return None


def fetch(target: Target, workdir: Path) -> Fetched:
    kind = target.kind
    if kind == "folder":
        return file.fetch_folder(target)
    if kind == "file" and target.watch == "timestamp":
        return file.fetch_timestamp(target)
    if kind == "file":
        return retrying(lambda: file.fetch(target, workdir))
    if kind == "web":
        return web.fetch(target, workdir)
    if kind == "json":
        return json_api.fetch(target, workdir)
    if kind == "confluence":
        return confluence.fetch(target, workdir)
    if kind == "download":
        return download.fetch(target, workdir)
    if kind == "office":
        return retrying(lambda: office.fetch(target, workdir))
    if kind == "command":
        return command.fetch(target, workdir)
    raise ValueError(f"知らない fetch です: {kind}")
