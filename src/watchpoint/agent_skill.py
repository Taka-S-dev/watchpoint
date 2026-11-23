"""The agent skill that comes with watchpoint, and putting it where AI agents look for skills.

The skill (skill/SKILL.md) tells an agent such as Copilot CLI or Claude Code how to check the
targets and summarize them along each target's reading points. It follows the Agent Skills format,
so one file serves every agent that reads it. Installing copies it, so `watchpoint skill install`
after an update keeps the skill in step with the commands it describes.

A SKILL.md already there that watchpoint did not write is never replaced without --force.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

NAME = "watchpoint"
MARK = "generated-by: watchpoint"

# Personal skill folders: ~/.agents/skills is read by Copilot CLI (and is the shared place for agents),
# ~/.claude/skills by Claude Code.
PLACES = {
    "agents": Path("~/.agents/skills"),
    "claude": Path("~/.claude/skills"),
}


def text() -> str:
    return resources.files("watchpoint").joinpath("skill", "SKILL.md").read_text(encoding="utf-8")


def destinations(places: list[str] | None, folder: Path | None) -> list[Path]:
    if folder is not None:
        return [folder.expanduser() / NAME / "SKILL.md"]
    names = places or list(PLACES)
    unknown = [p for p in names if p not in PLACES]
    if unknown:
        raise ValueError(f"知らない場所です: {', '.join(unknown)}（{' / '.join(PLACES)} か --dir）")
    return [PLACES[p].expanduser() / NAME / "SKILL.md" for p in names]


def install(dest: Path, force: bool = False) -> str:
    """Write the skill to dest. Returns "installed", "updated" or "unchanged"; raises if dest is someone else's."""
    new = text()
    if dest.exists():
        old = dest.read_text(encoding="utf-8", errors="replace")
        if old == new:
            return "unchanged"
        if MARK not in old and not force:
            raise FileExistsError(
                f"watchpoint が書いたものではない SKILL.md があります（上書きするなら --force）: {dest}"
            )
        dest.write_text(new, encoding="utf-8", newline="\n")
        return "updated"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(new, encoding="utf-8", newline="\n")
    return "installed"


def uninstall(dest: Path) -> bool:
    """Remove the skill watchpoint wrote. Leaves anything else alone."""
    if not dest.exists():
        return False
    if MARK not in dest.read_text(encoding="utf-8", errors="replace"):
        raise FileExistsError(f"watchpoint が書いたものではないので消しません: {dest}")
    dest.unlink()
    try:
        dest.parent.rmdir()  # only if nothing else is in the folder
    except OSError:
        pass
    return True
