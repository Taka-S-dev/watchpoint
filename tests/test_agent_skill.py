"""The agent skill that comes with watchpoint: its format, and installing it next to other skills."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from watchpoint import agent_skill
from watchpoint.cli import app


def _front(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md starts with YAML front matter"
    return yaml.safe_load(m.group(1))


def test_the_skill_follows_the_agent_skills_format() -> None:
    text = agent_skill.text()
    front = _front(text)
    assert front["name"] == "watchpoint" and re.fullmatch(r"[a-z0-9-]{1,64}", front["name"])
    assert 0 < len(front["description"]) <= 1024
    assert agent_skill.MARK in text
    assert len(text.splitlines()) < 500


def test_every_command_the_skill_names_exists() -> None:
    text = agent_skill.text()
    commands = set(re.findall(r"watchpoint ([a-z]+)", text))
    known = {c.name or c.callback.__name__ for c in app.registered_commands} | {g.name for g in app.registered_groups}
    assert commands and commands <= known, commands - known


def test_the_skill_never_mentions_a_tool_by_brand_beyond_what_it_needs() -> None:
    assert "svn" not in agent_skill.text().lower()


def test_install_update_and_leave_others_alone(tmp_path: Path) -> None:
    dest = tmp_path / "skills" / "watchpoint" / "SKILL.md"
    assert agent_skill.install(dest) == "installed" and dest.read_text(encoding="utf-8") == agent_skill.text()
    assert agent_skill.install(dest) == "unchanged"
    dest.write_text(agent_skill.text().replace("要約", "まとめ"), encoding="utf-8")  # an older version of ours
    assert agent_skill.install(dest) == "updated"
    dest.write_text("---\nname: watchpoint\ndescription: mine\n---\n", encoding="utf-8")  # someone else's
    try:
        agent_skill.install(dest)
        raise AssertionError("someone else's SKILL.md was replaced")
    except FileExistsError:
        pass
    assert agent_skill.install(dest, force=True) == "updated"
    assert agent_skill.uninstall(dest) and not dest.parent.exists()


def test_cli_installs_into_a_chosen_folder(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["skill", "install", "--dir", str(tmp_path / ".github" / "skills")])
    assert r.exit_code == 0 and (tmp_path / ".github" / "skills" / "watchpoint" / "SKILL.md").exists()
    r = runner.invoke(app, ["skill", "install", "--to", "nowhere"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["skill", "show"])
    assert r.exit_code == 0 and r.output.startswith("---\nname: watchpoint")


def test_default_places_are_the_personal_skill_folders(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    dests = agent_skill.destinations(None, None)
    assert dests == [
        tmp_path / ".agents" / "skills" / "watchpoint" / "SKILL.md",
        tmp_path / ".claude" / "skills" / "watchpoint" / "SKILL.md",
    ]


def test_the_skill_says_how_to_add_a_target_well() -> None:
    """An agent asked to add a table once watched its columns by cells and split them over two targets."""
    text = agent_skill.text()
    for said in ("watchpoint doctor", ".bak", "key", "columns", "show", "分けて避けない", "手元のファイル"):
        assert said in text, said
