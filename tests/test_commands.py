"""Watching the output of a program: who may run it, how it runs, and how its output is read."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from watchpoint import commands
from watchpoint.check import Status, check_target
from watchpoint.config import ConfigError, load_config

PY = sys.executable

LOG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<log>
<logentry revision="6"><author>yamada</author><date>2026-11-23T05:03</date><msg>保持期間を直す</msg>
<paths><path action="M">/trunk/docs/a.md</path><path action="A">/trunk/docs/b.md</path></paths></logentry>
<logentry revision="2"><author>sato</author><date>2026-11-20T09:00</date><msg>最初の版</msg></logentry>
</log>
"""


@pytest.fixture(autouse=True)
def _own_allow_list(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))  # never the real list


def _printing(text: str) -> list[str]:
    return [PY, "-c", f"import sys; sys.stdout.buffer.write({text.encode('utf-8')!r})"]


def _yaml(command: list[str], name: str = "出力") -> str:
    # JSON is YAML too: the arguments go in exactly as they are, backslashes and quotes included
    return f"targets:\n  - name: {name}\n    command: {json.dumps(command, ensure_ascii=False)}\n"


def _config(tmp_path: Path, command: list[str], extra: str = "", name: str = "出力", allowed: bool = True) -> object:
    """The tests of running and reading use python, outside the read-only list: allowed as from a terminal."""
    if allowed:
        commands.allow(command, name, unsafe=True)
    return load_config(tmp_path / "t.yaml", _yaml(command, name) + extra)


def _check(cfg, tmp_path: Path):
    return check_target(cfg.targets[0], cfg, tmp_path / "data")


def _reading(tmp_path: Path, text: str) -> list[str]:
    f = tmp_path / "history.tsv"
    f.write_text(text, encoding="utf-8")
    return ["findstr", "/r", ".", str(f)]


# --------------------------------------------------------------------------- only commands that read


@pytest.mark.parametrize(
    "command",
    [
        ["git", "-C", r"D:\repo", "log", "-n", "20", "--", "docs/"],
        ["git", "--no-pager", "show", "--stat", "HEAD"],
        ["svn", "log", "--xml", "-l", "20", "https://repo.example.com/trunk/docs"],
        ["findstr", "/r", ".", r"C:\x.txt"],
    ],
)
def test_read_only_commands_pass(command: list[str]) -> None:
    assert commands.safety_problem(command) is None


@pytest.mark.parametrize(
    ("command", "why"),
    [
        (["git", "push", "--force"], "読むだけのサブコマンドではありません"),
        (["git", "clean", "-fdx"], "読むだけのサブコマンドではありません"),
        (["git", "-c", "core.pager=calc", "log"], "サブコマンドの前"),
        (["git", "log", "--output=C:\\x.txt"], "--output"),
        (["git", "show", "-Ocalc", "x"], "-Ocalc"),
        (["svn", "commit", "-m", "x"], "読むだけのサブコマンドではありません"),
        (["svn", "diff", "--diff-cmd", "calc"], "--diff-cmd"),
        (["cmd", "/c", "del", "x"], "中で何でも動かせる"),
        (["powershell", "-c", "Get-ChildItem"], "中で何でも動かせる"),
        (["docker", "exec", "c", "svn", "log"], "中で何でも動かせる"),
        ([r"C:\tools\git.exe", "log"], "パスで書いています"),
        (["robocopy", "a", "b", "/MIR"], "読むだけと分かっているプログラムではありません"),
        (["git"], "サブコマンド"),
    ],
)
def test_anything_else_is_refused(command: list[str], why: str) -> None:
    problem = commands.safety_problem(command)
    assert problem and why in problem


@pytest.mark.parametrize(
    "command",
    [
        ["git", "log", "--outp=C:\\x.txt"],  # git takes an unambiguous prefix of a long option
        ["git", "log", "--ext-d"],
        ["git", "show", "--textc"],
        ["git", "log", "-iOcalc", "x"],  # short options bundled
        ["svn", "diff", "--diff-cmd=calc"],
    ],
)
def test_abbreviated_and_bundled_options_are_refused(command: list[str]) -> None:
    assert commands.safety_problem(command)


def test_paths_after_double_dash_are_not_options() -> None:
    assert commands.safety_problem(["git", "log", "--", "-Odd-name/"]) is None


def test_git_runs_with_every_program_hook_switched_off() -> None:
    run = commands.hardened(["git", "-C", r"D:\repo", "log", "-n", "5", "--", "docs/"])
    assert run[: len(commands.GIT_HARDENING) + 1] == ["git", *commands.GIT_HARDENING]
    assert f"--attr-source={commands.EMPTY_TREE}" in run and "core.fsmonitor=false" in run
    assert run[len(commands.GIT_HARDENING) + 1 :] == [
        "-C", r"D:\repo", "log", "--no-ext-diff", "--no-textconv", "-n", "5", "--", "docs/",
    ]  # fmt: skip
    assert commands.hardened(["git", "status"])[-1] == "status"
    assert commands.hardened(["findstr", "/r", ".", "x"]) == ["findstr", "/r", ".", "x"]


@pytest.mark.parametrize(
    "args",
    [["log", "--show-signature"], ["log", "--show-sig"], ["log", "--pretty=format:%h %G?"], ["log", "--format=%GS"]],
)
def test_signature_verification_is_refused(args: list[str]) -> None:
    assert commands.safety_problem(["git", *args])


def test_a_repository_signature_program_never_runs(tmp_path: Path) -> None:
    """A repository can turn on signature checks and name the program that checks; git log would start it."""
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    repo, marker = tmp_path / "repo", tmp_path / "gpg-ran.txt"
    repo.mkdir()

    def git(*a: str, data: bytes | None = None) -> str:
        r = subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, input=data)
        return r.stdout.decode().strip()

    git("init", "-q")
    signed = (
        b"tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
        b"author t <t@example.com> 1700000000 +0000\ncommitter t <t@example.com> 1700000000 +0000\n"
        b"gpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----\n\nsigned\n"
    )
    git("update-ref", "HEAD", git("hash-object", "-w", "-t", "commit", "--stdin", data=signed))
    script = tmp_path / "fake-gpg"
    script.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8", newline="\n")
    git("config", "gpg.program", script.as_posix())
    git("config", "log.showSignature", "true")
    subprocess.run(["git", "-C", str(repo), "log"], capture_output=True, check=False)
    if not marker.exists():
        pytest.skip("this git does not start the program here, so the check below would prove nothing")
    marker.unlink()
    cmd = ["git", "-C", str(repo), "log", "-n", "1"]
    commands.allow(cmd, "x")
    assert "signed" in commands.run(cmd, "x", tmp_path / "run", 30)
    assert not marker.exists()


@pytest.mark.parametrize("args", [["cat-file", "--filters", "HEAD:a"], ["cat-file", "--path=a", "blob", "x"]])
def test_cat_file_through_filters_is_refused(args: list[str]) -> None:
    assert commands.safety_problem(["git", *args])


@pytest.mark.parametrize(
    "args",
    [["status"], ["diff"], ["ls-files", "-m"], ["grep", "x"], ["blame", "a.txt"], ["describe", "--dirty"],
     ["describe", "--broken"], ["show", "--submodule=diff"]],
)  # fmt: skip
def test_subcommands_that_read_working_files_cannot_be_set(args: list[str]) -> None:
    """Working files go through the clean filters the repository's own config names; history does not."""
    assert commands.safety_problem(["git", *args])


def _repo_with_a_filter(tmp_path: Path) -> tuple[Path, Path]:
    """A repository whose own config, .gitattributes and .git/info/attributes all name a clean filter."""
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    repo, marker = tmp_path / "repo", tmp_path / "filter-ran.txt"
    repo.mkdir()

    def git(*a: str) -> None:
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "t")
    git("config", "user.email", "t@example.com")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "first")
    evil = f"\"{PY}\" -c \"open(r'{marker}', 'w').write('ran'); import sys; sys.stdout.write(sys.stdin.read())\""
    git("config", "filter.evil.clean", evil.replace("\\", "/"))
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    return repo, marker


def test_a_repository_filter_never_runs_from_history_commands(tmp_path: Path) -> None:
    import subprocess

    repo, marker = _repo_with_a_filter(tmp_path)
    subprocess.run(["git", "-C", str(repo), "--attr-source=" + commands.EMPTY_TREE, "diff"], capture_output=True)
    if not marker.exists():
        pytest.skip("this git does not run the filter here, so the check below would prove nothing")
    marker.unlink()
    for args in (["log", "-p"], ["show"], ["describe", "--always"], ["rev-list", "HEAD"], ["ls-tree", "HEAD"],
                 ["cat-file", "-p", "HEAD:a.txt"], ["shortlog", "-s", "HEAD"]):  # fmt: skip
        cmd = ["git", "-C", str(repo), *args]
        commands.allow(cmd, "x")
        assert commands.run(cmd, "x", tmp_path / "run", 30).strip(), args
    assert not marker.exists()


def test_a_repository_that_would_start_a_program_on_fetch_is_not_run(tmp_path: Path) -> None:
    import subprocess

    repo, _ = _repo_with_a_filter(tmp_path)
    log = ["git", "-C", str(repo), "log", "--oneline"]
    commands.allow(log, "x")
    for key in ("core.sshCommand", "remote.origin.uploadpack"):
        subprocess.run(["git", "-C", str(repo), "config", key, "evil"], check=True)
        with pytest.raises(PermissionError, match=r"--global"):
            commands.run(log, "x", tmp_path / "run", 30)
        subprocess.run(["git", "-C", str(repo), "config", "--unset", key], check=True)
    assert "first" in commands.run(log, "x", tmp_path / "run", 30)


def test_a_program_left_in_the_current_folder_is_never_picked(tmp_path: Path, monkeypatch) -> None:
    import os

    real = commands.find_program("git")
    if not real:
        pytest.skip("git is not installed")
    planted = tmp_path / ("git.bat" if os.name == "nt" else "git")
    planted.write_text("@echo planted\n", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", os.environ["PATH"]]))
    assert commands.find_program("git") == real


def test_a_real_git_log_runs_hardened(tmp_path: Path) -> None:
    import shutil

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    # a history of its own: this repository's may be one commit deep (a fresh or shallow clone)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True, env=env)
    for i in range(4):
        (repo / "src" / "a.txt").write_text(str(i), encoding="utf-8")
        subprocess.run([*git, "add", "."], check=True, env=env)
        subprocess.run([*git, "commit", "-q", "-m", f"change {i}"], check=True, env=env)
    cmd = ["git", "-C", str(repo), "log", "-n", "3", "--pretty=format:%h%x09%an%x09%s", "--", "src/"]
    commands.allow(cmd, "git")
    out = commands.run(cmd, "git", tmp_path / "run", 30)
    assert len(out.strip().splitlines()) == 3 and all(line.count("\t") >= 2 for line in out.strip().splitlines())


def test_a_config_with_a_writing_command_cannot_be_loaded_or_saved(gui) -> None:
    call, _, app = gui
    before = app.config_path.read_bytes()
    text = _yaml(["git", "push", "--force"])
    with pytest.raises(ConfigError, match="(?s)command は使えません.*--unsafe"):
        load_config(app.config_path, text)
    status, r = call("/api/config/save", {"text": text, "hash": call("/api/config")[1]["hash"]})
    assert status == 422 and "読むだけのサブコマンドではありません" in r["error"]
    assert app.config_path.read_bytes() == before


def test_the_page_cannot_allow_outside_the_list(gui, tmp_path: Path) -> None:
    call, _, app = gui
    cmd = _printing("x\n")
    commands.allow(cmd, "出力", unsafe=True)
    app.config_path.write_text(_yaml(cmd), encoding="utf-8")
    commands.revoke([commands.fingerprint(cmd)])
    status, r = call("/api/commands/allow", {"name": "出力", "command": cmd})
    assert status >= 400 and not commands.is_allowed(cmd)


def test_allowing_outside_the_list_needs_the_terminal_and_the_name(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from watchpoint.cli import app

    cmd = _printing("x\n")
    cfg = tmp_path / "t.yaml"
    cfg.write_text(_yaml(cmd), encoding="utf-8")
    runner = CliRunner()
    r = runner.invoke(app, ["allow", "出力", "-c", str(cfg), "--yes"])
    assert r.exit_code == 1 and "--unsafe" in r.output and not commands.is_allowed(cmd)
    r = runner.invoke(app, ["allow", "出力", "-c", str(cfg), "--unsafe", "--yes"], input="違う\n")
    assert r.exit_code == 1 and not commands.is_allowed(cmd)  # --yes does not skip typing the name
    r = runner.invoke(app, ["allow", "出力", "-c", str(cfg), "--unsafe"], input="出力\n")
    assert r.exit_code == 0 and commands.is_allowed(cmd) and commands.allowed_despite(cmd)
    listed = commands.allowed_list()
    assert [e["read_only"] for e in listed] == [False]


def test_an_old_allowance_outside_the_list_does_not_count(tmp_path: Path) -> None:
    cmd = ["cmd", "/c", "type", "x"]
    store = commands._store()
    store.parent.mkdir(parents=True)
    fp = commands.fingerprint(cmd)
    store.write_text(json.dumps({fp: {"command": cmd, "first_for": "x", "allowed": "2026-11-23"}}), encoding="utf-8")
    assert not commands.is_allowed(cmd) and commands.allowed_list() == []


# --------------------------------------------------------------------------- allowing


def test_a_command_runs_only_after_it_is_allowed(tmp_path: Path) -> None:
    cmd = _reading(tmp_path, "r1\tsato\t初期構成\n")
    cfg = _config(tmp_path, cmd, allowed=False)
    r = _check(cfg, tmp_path)
    assert (
        r.status == Status.FAILED and "まだ許可していないコマンド" in r.message and "watchpoint allow 出力" in r.message
    )
    commands.allow(cmd, "出力")
    assert _check(cfg, tmp_path).status == Status.FIRST


def test_changing_the_command_asks_again(tmp_path: Path) -> None:
    cmd = _reading(tmp_path, "x\n")
    commands.allow(cmd, "出力")
    assert not commands.is_allowed([*cmd[:-1], cmd[-1] + "x"])


def test_commands_are_refused_from_included_files(tmp_path: Path) -> None:
    (tmp_path / "more.yaml").write_text("targets:\n  - name: x\n    command: [whoami]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="取り込んだ設定.*command を書けません"):
        load_config(tmp_path / "t.yaml", "include: [more.yaml]\ntargets: []\n")


def test_commands_are_refused_from_a_shared_config(tmp_path: Path, monkeypatch) -> None:
    import watchpoint.config as config

    monkeypatch.setattr(config, "_is_shared", lambda path: True)
    with pytest.raises(ConfigError, match="共有フォルダにある設定ファイルには command"):
        _config(tmp_path, ["whoami"])


def test_unc_paths_count_as_shared() -> None:
    from watchpoint.config import _is_shared

    assert _is_shared(Path(r"\\server\share\targets.yaml"))


# --------------------------------------------------------------------------- running


def test_no_shell_so_nothing_can_be_chained(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    cmd = [PY, "-c", "import sys; print(sys.argv[1:])", "x;", "echo", ">", str(marker)]
    cfg = _config(tmp_path, cmd)
    assert _check(cfg, tmp_path).status == Status.FIRST
    assert not marker.exists()


def test_a_failing_command_shows_its_error(tmp_path: Path) -> None:
    cmd = [PY, "-c", "import sys; sys.stderr.write('no such path'); sys.exit(3)"]
    cfg = _config(tmp_path, cmd)
    r = _check(cfg, tmp_path)
    assert r.status == Status.FAILED and "終了コード 3" in r.message and "no such path" in r.message


def test_a_slow_command_is_stopped(tmp_path: Path) -> None:
    cmd = [PY, "-c", "import time; time.sleep(30)"]
    cfg = _config(tmp_path, cmd, "    timeout: 1\n")
    r = _check(cfg, tmp_path)
    assert r.status == Status.FAILED and "1 秒で終わりませんでした" in r.message


def test_a_missing_program_says_so(tmp_path: Path) -> None:
    cmd = ["no-such-program-wp", "log"]
    cfg = _config(tmp_path, cmd)
    assert "プログラムが見つかりません" in _check(cfg, tmp_path).message


def test_output_in_the_local_code_page_is_read(tmp_path: Path) -> None:
    cmd = [PY, "-c", "import sys; sys.stdout.buffer.write('設計書\\n'.encode('cp932'))"]
    cfg = _config(tmp_path, cmd)
    _check(cfg, tmp_path)
    text = next((tmp_path / "data").rglob("text.txt")).read_text(encoding="utf-8")
    assert "設計書" in text


# --------------------------------------------------------------------------- reading the output


def _text_of(tmp_path: Path, command: list[str], extra: str) -> str:
    cfg = _config(tmp_path, command, extra)
    r = _check(cfg, tmp_path)
    assert r.status == Status.FIRST, r.message
    return next((tmp_path / "data").rglob("text.txt")).read_text(encoding="utf-8")


def test_xml_records_one_per_element(tmp_path: Path) -> None:
    text = _text_of(tmp_path, _printing(LOG_XML), "    format: xml\n    records: logentry\n    key: revision\n")
    assert "[revision=6] author=yamada | date=2026-11-23T05:03 | msg=保持期間を直す" in text
    assert "paths.path=/trunk/docs/a.md, /trunk/docs/b.md" in text
    assert "paths.path.action=M, A" in text


def test_xml_with_a_doctype_is_refused(tmp_path: Path) -> None:
    bad = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><log><e>&a;</e></log>'
    cfg = _config(tmp_path, _printing(bad), "    format: xml\n    records: e\n")
    assert "DOCTYPE" in _check(cfg, tmp_path).message


def test_json_and_tsv_with_named_fields(tmp_path: Path) -> None:
    js = '[{"id": 3, "title": "ログ"}, {"id": 4, "title": "画面"}]'
    text = _text_of(tmp_path, _printing(js), "    format: json\n    key: id\n")
    assert "[id=4] title=画面" in text
    tsv = "a1b2\tsato\t修正\nc3d4\tsuzuki\t追加\n"
    text = _text_of(
        tmp_path / "t2", _printing(tsv), "    format: tsv\n    fields: [hash, author, msg]\n    key: hash\n"
    )
    assert "[hash=c3d4] author=suzuki | msg=追加" in text


def test_a_new_record_is_reported_as_added(tmp_path: Path) -> None:
    out = tmp_path / "out.xml"
    out.write_text(LOG_XML, encoding="utf-8")
    cmd = [PY, "-c", "import sys; sys.stdout.write(open(sys.argv[1], encoding='utf-8').read())", str(out)]
    cfg = _config(
        tmp_path, cmd, "    format: xml\n    records: logentry\n    key: revision\n    columns: [author, msg]\n"
    )
    assert _check(cfg, tmp_path).status == Status.FIRST
    out.write_text(
        LOG_XML.replace("<log>", '<log><logentry revision="7"><author>kato</author><msg>図を追加</msg></logentry>'),
        encoding="utf-8",
    )
    r = _check(cfg, tmp_path)
    assert r.status == Status.CHANGED and "追加 [revision=7] author=kato | msg=図を追加" in r.diff


@pytest.mark.parametrize(
    ("extra", "msg"),
    [
        ("    source: x.txt\n", "source を書かないで"),
        ("    format: xml\n", "records"),
        ("    key: id\n", "format が json・csv・tsv・xml"),
        ("    format: json\n    fields: [a]\n", "fields は format: csv / tsv"),
    ],
)
def test_command_settings_that_do_not_fit(tmp_path: Path, extra: str, msg: str) -> None:
    with pytest.raises(ConfigError, match=msg):
        _config(tmp_path, ["tool"], extra)


def test_format_without_command_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="command のときだけ"):
        load_config(tmp_path / "t.yaml", "targets:\n  - name: x\n    source: a.txt\n    format: xml\n")


# --------------------------------------------------------------------------- the page


def test_page_lists_and_revokes_allowed_commands(gui, tmp_path: Path) -> None:
    call, _, app = gui
    used, unused = _reading(tmp_path, "a\n"), ["findstr", "/r", ".", r"C:\old.txt"]
    app.config_path.write_text(_yaml(used), encoding="utf-8")
    commands.allow(unused, "前の見張り")
    status, r = call("/api/commands")
    assert status == 200 and [w["name"] for w in r["waiting"]] == ["出力"]
    assert [(e["command"], e["used_by"]) for e in r["entries"]] == [(unused, [])]
    commands.allow(used, "出力")
    r = call("/api/commands")[1]
    assert {tuple(e["used_by"]) for e in r["entries"]} == {("出力",), ()} and r["waiting"] == []
    stale = [e["id"] for e in r["entries"] if not e["used_by"]]
    status, r = call("/api/commands/revoke", {"ids": stale})
    assert status == 200 and r["revoked"] == stale
    assert commands.is_allowed(used) and not commands.is_allowed(unused)


def test_cli_lists_and_revokes(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from watchpoint.cli import app

    cmd = _reading(tmp_path, "x\n")
    cfg = tmp_path / "t.yaml"
    cfg.write_text(_yaml(cmd), encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(app, ["allow", "出力", "-c", str(cfg), "--yes"]).exit_code == 0
    listed = runner.invoke(app, ["allow", "-c", str(cfg)], env={"COLUMNS": "300"})
    assert "出力" in listed.output and "findstr" in listed.output
    assert runner.invoke(app, ["allow", "出力", "-c", str(cfg), "--revoke"]).exit_code == 0
    assert not commands.is_allowed(cmd)


def test_a_hand_edited_entry_is_not_listed(tmp_path: Path) -> None:
    commands.allow(["git", "log"], "x")
    store = commands._store()
    data = json.loads(store.read_text(encoding="utf-8"))
    (fp,) = data
    data[fp]["command"] = ["git", "push"]  # the id no longer matches what it says it allows
    store.write_text(json.dumps(data), encoding="utf-8")
    assert commands.allowed_list() == []


def test_page_allows_only_the_command_it_showed(gui, tmp_path: Path) -> None:
    call, _, app = gui
    cmd = _reading(tmp_path, "hello\n")
    app.config_path.write_text(_yaml(cmd), encoding="utf-8")
    detail = call("/api/target?name=%E5%87%BA%E5%8A%9B")[1]
    assert detail["command"] == cmd and detail["command_allowed"] is False
    status, r = call("/api/commands/allow", {"name": "出力", "command": [*cmd, "extra"]})
    assert status == 409 and not commands.is_allowed(cmd)
    status, r = call("/api/commands/allow", {"name": "出力", "command": cmd})
    assert status == 200 and commands.is_allowed(cmd)
