"""Running a program whose output is watched, and remembering which commands the person allowed.

A config file can name a program to run (`command:`). Whoever can edit that file could then run
anything with your rights, so a command runs only after you have allowed that exact command once
(`watchpoint allow`, or the button on the page). Changing one character asks again. The list is
kept per Windows user, outside the config, in %LOCALAPPDATA%\\watchpoint.

Only commands known to read and never write may be set at all: the read-only subcommands of
version control tools, without the options that write a file or start another program, named
without a path (see READ_ONLY). Anything else (a shell, an interpreter, `docker exec`, a program
given by path) makes the config invalid, unless the person has allowed that exact command in a
terminal with `watchpoint allow <name> --unsafe`, after reading a warning and typing the name.
Editing the config alone can never make such a command run.

The program is started directly (no shell, so `;` or `|` cannot chain another one), in an empty
working folder, with no input, a time limit and a cap on how much output is read.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import locale
import os
import re
import subprocess
from pathlib import Path


def command_line(args: list[str]) -> str:
    """The command as one line to show (quoted where a part has spaces)."""
    return " ".join(f'"{a}"' if (" " in a or not a) else a for a in args)


MAX_OUTPUT = 20 * 1024 * 1024
STDERR_SHOWN = 600


# program -> its subcommands that only read (None: the program only reads, whatever the arguments)
READ_ONLY: dict[str, set[str] | None] = {
    # Only what reads the history. Subcommands that read working files (status, diff, ls-files, grep, blame)
    # run the clean filters the repository's own config and .git/info/attributes name, and nothing on the
    # command line can switch those off; a working copy on a share can be given one by anyone writing there.
    "git": {"log", "show", "ls-tree", "rev-parse", "rev-list", "describe", "shortlog", "cat-file"},
    "svn": {
        "log", "info", "list", "ls", "cat", "diff", "di", "status", "st", "stat",
        "blame", "praise", "annotate", "ann", "proplist", "plist", "pl", "propget", "pget", "pg",
    },
    "findstr": None,
}  # fmt: skip
# options before the subcommand that are allowed, and whether they take the next argument as a value
GLOBAL_OPTIONS: dict[str, dict[str, bool]] = {
    "git": {"-C": True, "--no-pager": False, "--git-dir": True, "--work-tree": True, "--bare": False},
    "svn": {},
}
# options that write a file, change the settings or start another program, anywhere after the subcommand
FORBIDDEN: dict[str, tuple[str, ...]] = {
    "git": (
        "--output",
        "--ext-diff",
        "--textconv",
        "--open-files-in-pager",
        "-O",
        "--exec",
        "--upload-pack",
        "--filters",
        "--path",
        "--attr-source",
        "--show-signature",  # verifying a signature starts the program the repository's config names
        "--dirty",  # describe --dirty and --broken read the working files (and so run filters)
        "--broken",
        "--submodule",  # would read submodules, each with a config of its own
    ),  # fmt: skip
    "svn": ("--diff-cmd", "--diff3-cmd", "--editor-cmd", "--config-option", "--config-dir"),
}
RUNS_ANYTHING = {
    "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "wsl", "python", "python3", "py", "pythonw",
    "node", "ruby", "perl", "cscript", "wscript", "mshta", "rundll32", "docker", "podman", "ssh",
}  # fmt: skip


def _program(arg: str) -> str:
    name = arg.lower()
    for ext in (".exe", ".com"):
        name = name.removesuffix(ext)
    return name


def safety_problem(command: list[str]) -> str | None:
    """Why the command is not known to only read, or None when it is."""
    first = command[0]
    if "/" in first or "\\" in first or ":" in first:
        return "プログラムをパスで書いています。読むだけのコマンドは、名前だけ（git など）で書いてください"
    prog = _program(first)
    if prog not in READ_ONLY:
        if prog in RUNS_ANYTHING:
            return f"「{first}」は、中で何でも動かせるプログラムです"
        return f"「{first}」は、読むだけと分かっているプログラムではありません"
    subs = READ_ONLY[prog]
    if subs is None:
        return None
    args = command[1:]
    i = 0
    known = GLOBAL_OPTIONS.get(prog, {})
    while i < len(args) and args[i].startswith("-"):
        opt = args[i].split("=", 1)[0]
        if opt not in known:
            return f"サブコマンドの前の「{args[i]}」は使えません（設定を書き換えたり、別のプログラムを動かしたりできるため）"
        i += 2 if known[opt] and "=" not in args[i] else 1
    if i >= len(args):
        return "サブコマンド（log など）がありません"
    sub = args[i]
    if sub not in subs:
        return f"「{prog} {sub}」は、読むだけのサブコマンドではありません"
    for a in args[i + 1 :]:
        if a == "--":
            break  # paths from here on
        if _forbidden(prog, a):
            return f"「{a}」は使えません（ファイルに書き込んだり、別のプログラムを動かしたりできるため）"
    return None


def _forbidden(prog: str, arg: str) -> bool:
    if prog == "git" and "%G" in arg:
        return True  # %G… in --pretty / --format asks for signature verification, which starts a program
    bad = FORBIDDEN.get(prog, ())
    if arg.startswith("--"):
        name = arg.split("=", 1)[0]
        # git takes an unambiguous prefix of a long option (--outp means --output): refuse those too
        return any(name == b or (len(name) >= 4 and b.startswith(name)) for b in bad if b.startswith("--"))
    if arg.startswith("-") and len(arg) > 1:
        # short options can be bundled (-iO...): refuse when a forbidden letter is among them
        letters = {b[1] for b in bad if len(b) == 2 and b[0] == "-" and b[1] != "-"}
        return any(ch in letters for ch in arg[1:])
    return False


# Settings a repository's own config or attributes could use to start a program, emptied on every run.
# --attr-source points attribute lookup at the empty tree, so no .gitattributes can name a filter or a diff
# driver (no allowed subcommand reads working files, where filters run; this is a second line). It needs git 2.40+;
# an older git stops with "unknown option", so the command fails instead of running unprotected.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# Signatures: a repository's config can turn verification on (log.showSignature) and name the program
# that verifies (gpg.*program). Verification is switched off and every such program points at nothing.
NO_PROGRAM = "watchpoint-no-such-program"
GIT_HARDENING = [
    f"--attr-source={EMPTY_TREE}",
    "-c", "core.fsmonitor=false", "-c", "diff.external=", "-c", "core.pager=cat",
    "-c", "log.showSignature=false",
    "-c", f"gpg.program={NO_PROGRAM}", "-c", f"gpg.openpgp.program={NO_PROGRAM}",
    "-c", f"gpg.x509.program={NO_PROGRAM}", "-c", f"gpg.ssh.program={NO_PROGRAM}",
    "-c", "submodule.recurse=false", "-c", "diff.submodule=short",
    # programs a fetch of missing objects would start (also switched off with GIT_NO_LAZY_FETCH)
    "-c", f"core.sshCommand={NO_PROGRAM}", "-c", f"core.askPass={NO_PROGRAM}", "-c", "credential.helper=",
    "--no-pager",
]  # fmt: skip
GIT_NO_DRIVERS = {"log": ["--no-ext-diff", "--no-textconv"], "show": ["--no-ext-diff", "--no-textconv"]}


def _git_parts(command: list[str]) -> tuple[list[str], str, list[str]]:
    """(options before the subcommand, the subcommand, the rest) of a git command."""
    args = command[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if GLOBAL_OPTIONS["git"].get(args[i].split("=", 1)[0]) and "=" not in args[i] else 1
    return args[:i], args[i], args[i + 1 :]


def _is_hardened_git(command: list[str]) -> bool:
    return _program(command[0]) == "git" and safety_problem(command) is None


def hardened(command: list[str]) -> list[str]:
    """The argument list actually run: for git, with every way of starting another program switched off."""
    if not _is_hardened_git(command):
        return list(command)
    before, sub, rest = _git_parts(command)
    return [command[0], *GIT_HARDENING, *before, sub, *GIT_NO_DRIVERS.get(sub, []), *rest]


# Settings a repository's own config could use to start a program that the -c settings above cannot name
# in advance (an upload-pack or proxy program of a remote). A working copy on a share can be written by
# others, so a repository whose own config (not the person's global one) names one is not run at all.
REPO_PROGRAM_KEYS = re.compile(
    r"^(core\.(sshcommand|gitproxy|askpass)|credential(\..+)?\.helper|remote\..+\.(uploadpack|vcs))$", re.I
)


def repo_program_problem(exe: str, command: list[str], env: dict, cwd: Path, timeout: float) -> str | None:
    """The key in the repository's own config that would start a program, or None."""
    before, _, _ = _git_parts(command)
    done = subprocess.run(  # noqa: S603 - git itself, reading config only
        [exe, *before, "config", "--list", "--show-scope", "--includes", "--null"],
        cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, check=False,
    )  # fmt: skip
    if done.returncode != 0:
        return None  # not a repository (or no config): the command itself will say what is wrong
    parts = done.stdout.split(b"\0")
    for scope, entry in zip(parts[0::2], parts[1::2], strict=False):
        if scope not in (b"local", b"worktree"):
            continue  # the person's own global and system settings are theirs to trust
        key = entry.split(b"\n", 1)[0].decode("utf-8", "replace")
        if REPO_PROGRAM_KEYS.match(key):
            return key
    return None


def unsafe_message(name: str, problem: str) -> str:
    return (
        f"「{name}」の command は使えません: {problem}\n"
        "見張りには、読むだけのコマンド（バージョン管理の log・show など、履歴を読むもの）を使ってください。"
        f'どうしても必要なら、中身を確かめてから、ターミナルで watchpoint allow "{name}" --unsafe'
        "（画面からはできません）"
    )


class NotAllowed(Exception):
    """The command has not been allowed by the person (or has changed since)."""


def _store() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "state")
    return base / "watchpoint" / "allowed-commands.json"


def fingerprint(command: list[str]) -> str:
    return hashlib.sha256(json.dumps(command, ensure_ascii=False).encode("utf-8")).hexdigest()


def _load() -> dict[str, dict]:
    try:
        data = json.loads(_store().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry(command: list[str]) -> dict | None:
    entry = _load().get(fingerprint(command))
    return entry if isinstance(entry, dict) and entry.get("command") == command else None


def is_allowed(command: list[str]) -> bool:
    """Allowed, and either known to only read, or allowed in a terminal despite that."""
    entry = _entry(command)
    return entry is not None and (safety_problem(command) is None or entry.get("unsafe") is True)


def allowed_despite(command: list[str]) -> bool:
    """A command outside the read-only list that the person allowed in a terminal (--unsafe)."""
    entry = _entry(command)
    return entry is not None and entry.get("unsafe") is True


def allow(command: list[str], name: str, unsafe: bool = False) -> None:
    """Remember the command as allowed. Outside the read-only list only with unsafe (the terminal)."""
    problem = safety_problem(command)
    if problem and not unsafe:
        raise ValueError(unsafe_message(name, problem))
    data = _load()
    data[fingerprint(command)] = {
        "command": command,
        "first_for": name,
        "allowed": dt.datetime.now().isoformat(sep=" ", timespec="seconds"),
        "unsafe": bool(problem),
    }
    path = _store()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def allowed_list() -> list[dict]:
    """Every allowed command, newest first: {id, command, line, first_for, allowed}."""
    out = []
    for fp, entry in _load().items():
        cmd = entry.get("command") if isinstance(entry, dict) else None
        if not isinstance(cmd, list) or fingerprint(cmd) != fp:
            continue  # a hand-edited or damaged entry is not shown as allowed
        problem = safety_problem(cmd)
        if problem and entry.get("unsafe") is not True:
            continue  # allowed before the read-only list existed: it does not run, so it is not listed
        out.append({
            "id": fp, "command": cmd, "line": command_line(cmd), "read_only": problem is None,
            "first_for": str(entry.get("first_for", "")), "allowed": str(entry.get("allowed", "")),
        })  # fmt: skip
    return sorted(out, key=lambda e: e["allowed"], reverse=True)


def revoke(ids: list[str]) -> list[str]:
    """Take back the given allowances. Returns the ids that were there."""
    data = _load()
    gone = [i for i in ids if i in data]
    if gone:
        for i in gone:
            del data[i]
        path = _store()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return gone


def not_allowed_message(name: str, command: list[str]) -> str:
    return (
        f"まだ許可していないコマンドです: {command_line(command)}\n"
        f"中身を確かめてから、画面の「このコマンドを許可する」か `watchpoint allow {name}` で許可してください"
    )


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "cp932", errors="replace")


def find_program(program: str) -> str | None:
    """The program on PATH, never from the current folder: a relative PATH entry (".", or an empty one) would
    let a git.exe left in whatever folder watchpoint was started from stand in for the real one."""
    # Searched here rather than with shutil.which: before Python 3.12 it looked in the current folder
    # first on Windows, even when given a PATH.
    entries = [e for e in os.environ.get("PATH", os.defpath).split(os.pathsep) if e and os.path.isabs(e)]
    if os.name == "nt" and not os.path.splitext(program)[1]:
        exts = [x for x in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if x]
    else:
        exts = [""]
    for folder in entries:
        for ext in exts:
            candidate = os.path.join(folder, program + ext)
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    return None


def run(command: list[str], name: str, workdir: Path, timeout: float) -> str:
    """Run an allowed command in workdir and return its output as text."""
    if not is_allowed(command):
        raise NotAllowed(not_allowed_message(name, command))
    program = command[0]
    exe = program if os.path.isabs(program) else find_program(program)
    if not exe:
        raise FileNotFoundError(
            f"プログラムが見つかりません: {program}（PATH が通っているか、フルパスで書いてください）"
        )
    workdir.mkdir(parents=True, exist_ok=True)  # empty: nothing there can stand in for the program
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("GIT_EXTERNAL_DIFF", "GIT_PAGER", "PAGER")}
    env["GIT_PAGER"] = "cat"
    env["GIT_TERMINAL_PROMPT"] = "0"  # never wait for a password nobody can type
    env["GIT_NO_LAZY_FETCH"] = "1"  # a partial clone must not go fetching (and start ssh) to read
    if _is_hardened_git(command):
        key = repo_program_problem(exe, command, env, workdir, timeout)
        if key:
            raise PermissionError(
                f"このリポジトリの設定（{key}）は、読むだけのはずのコマンドからプログラムを動かせるので、"
                "動かしませんでした。自分で入れた設定なら、リポジトリから外して、自分の全体の設定（--global）に"
                "置いてください"
            )
    try:
        done = subprocess.run(  # noqa: S603 - an allowed list of arguments, no shell
            [exe, *hardened(command)[1:]],
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=flags,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(
            f"コマンドが {timeout:g} 秒で終わりませんでした（timeout で延ばせます）: {command_line(command)}"
        ) from None
    if len(done.stdout) > MAX_OUTPUT:
        raise ValueError(f"コマンドの出力が大きすぎます（{MAX_OUTPUT // 1024 // 1024} MB まで）。件数を絞ってください")
    if done.returncode != 0:
        err = _decode(done.stderr).strip()[-STDERR_SHOWN:] or "（エラーの出力なし）"
        raise RuntimeError(f"コマンドが失敗しました（終了コード {done.returncode}）: {err}")
    return _decode(done.stdout)
