"""Loading and validating the targets file."""

from __future__ import annotations

import datetime as dt
import glob as globmod
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from . import sharepoint
from .commands import allowed_despite, command_line, safety_problem, unsafe_message
from .store import safe_name
from .textify import CellRefError, cell_refs
from .timewords import DEFAULT_EVERY, check_every, day_set, every_delta, parse_deadline

ENV_CONFIG = "WATCHPOINT_CONFIG"
DEFAULT_CONFIG = Path.home() / ".watchpoint" / "targets.yaml"

FetchKind = Literal["auto", "file", "folder", "web", "json", "confluence", "office", "download", "command"]
OutputFormat = Literal["text", "json", "csv", "tsv", "xml"]
ChangeKind = Literal["added", "changed", "removed"]
Normalize = Literal["whitespace", "nfkc", "case"]
Watch = Literal["content", "timestamp"]
PLAN_SUFFIXES = (".mpp", ".mpt")  # Microsoft Project plans
# A web address ending in one of these is a file to download and read as that file (a Confluence
# attachment, a file on an intranet site); anything else on the web is a page.
DOWNLOAD_SUFFIXES = {".xlsx", ".xlsm", ".docx", ".csv", ".tsv", ".txt", ".md", ".pdf", ".mpp", ".mpt"}
# Names that end like this are files: a folder is never named so (kind needs no disk for them).
FILE_SUFFIXES = DOWNLOAD_SUFFIXES | {
    ".xls", ".doc", ".ppt", ".pptx", ".vsdx", ".log", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".ini",
    ".zip", ".png", ".jpg", ".jpeg", ".gif",
}  # fmt: skip


def url_suffix(url: str) -> str:
    """The file extension of a web address's path (percent-escapes undone, query and fragment ignored)."""
    return Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).suffix.lower()


_TIME_RANGE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[-~〜]\s*(\d{1,2}):(\d{2})\s*$")


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """The targets file is missing or invalid. The message is shown to the user as is."""


class Strict(BaseModel):
    # A misspelt key (`wehre:`) must not be ignored silently.
    model_config = ConfigDict(extra="forbid")


class Auth(Strict):
    type: Literal["bearer", "basic", "header"]
    secret: str = Field(min_length=1, description="name given to `watchpoint secret set`")
    user: str | None = None  # basic
    header: str | None = None  # header, e.g. X-Redmine-API-Key

    @model_validator(mode="after")
    def _needs(self) -> Auth:
        if self.type == "basic" and not self.user:
            raise ValueError("type: basic には user が要ります")
        if self.type == "header" and not self.header:
            raise ValueError("type: header には header（ヘッダー名）が要ります")
        return self


class Rule(Strict):
    """A check on the latest values, made on every check: an empty field, an unexpected value."""

    field: str = Field(min_length=1)  # a column (rows), a cell's name (cells) or a JSON field
    required: bool = False  # must not be empty
    one_of: list[str] | None = None  # must be one of these (an empty value is left to `required`)
    pattern: str | None = None  # must match this regular expression (an empty value is left to `required`)
    when: dict[str, str | list[str]] | None = None  # only rows whose field contains one of these
    message: str | None = None  # the warning, in the person's own words

    @field_validator("pattern")
    @classmethod
    def _valid_pattern(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"pattern の正規表現の誤り「{v}」: {e}") from e
        return v

    @model_validator(mode="after")
    def _says_something(self) -> Rule:
        if not (self.required or self.one_of or self.pattern):
            raise ValueError(f"「{self.field}」のルールに、required・one_of・pattern のどれかを書いてください")
        return self


_FOLDER_TTL = 60.0  # seconds a path's answer is kept
_folders: dict[str, tuple[float, bool]] = {}


def _is_folder(source: str) -> bool:
    """Whether a path names a folder, without letting a place that cannot be reached stop the config from
    loading: a share that refuses access is taken for a file here, and its check fails with the reason.

    The answer is kept for a minute: kind is read many times per target, and the page reloads the config
    every few seconds; on a share each ask is a round trip, seconds long when the share does not answer.
    """
    now = time.monotonic()
    hit = _folders.get(source)
    if hit is not None and now - hit[0] < _FOLDER_TTL:
        return hit[1]
    try:
        answer = Path(source).is_dir()
    except (OSError, ValueError):
        answer = False
    _folders[source] = (now, answer)
    return answer


class Target(Strict):
    name: str = Field(min_length=1)
    id: str | None = None  # keeps the history when the name is changed
    source: str = ""  # required, except with command (then it shows the command)
    fetch: FetchKind = "auto"
    # A program run on this PC whose output is watched: ["tool", "log", "--xml", "D:\\repo"]. No shell.
    command: list[str] | None = None
    format: OutputFormat = "text"  # how to read the command's output
    fields: list[str] | None = None  # csv / tsv output without a header row: the column names
    tags: list[str] = Field(default_factory=list)
    # Where "open the place" on the page goes, instead of the default (a file's folder, a web page itself):
    # an http(s) URL, or a folder or file path (a file is shown selected in Explorer, never opened).
    link: str | None = None
    # Warnings made on every check: when the source has not changed by a deadline ("毎週金 17:00",
    # "平日 9:00", "3d"), and values that break a rule (an empty 承認, a 状態 outside a list).
    expect_update: str | None = None
    rules: list[Rule] = Field(default_factory=list)
    every: str | None = None  # 30m / 2h / 1d; used by `check --due`. Unset: the config's every
    enabled: bool = True
    # content: the text made from the copy. timestamp: a file's size and modified time only,
    # without copying it (a large file, or when "it was saved" is all that matters).
    watch: Watch = "content"

    points: str = ""
    notify_when: str = ""

    # shaping the text
    sections: list[str] = Field(default_factory=list)  # keep only these headings (and what is under them)
    include: list[str] = Field(default_factory=list)  # regexes; keep only matching lines
    ignore: list[str] = Field(default_factory=list)  # regexes; drop matching lines
    normalize: list[Normalize] = Field(default_factory=list)
    sort_lines: bool = False

    # deciding what counts as a change
    report: list[ChangeKind] = Field(default_factory=lambda: ["added", "changed", "removed"])
    match: list[str] = Field(default_factory=list)  # regexes; count a change only if a changed line matches
    fail_after: int = Field(default=1, ge=1)  # report a failure only after this many in a row
    max_size_mb: float | None = Field(default=None, gt=0)

    # Excel / CSV / JSON records
    sheets: list[str] | None = None
    header_row: int | Literal["auto"] = 1
    key: str | None = None
    columns: list[str] | None = None
    # columns read beside a changed row (who answers it, by when) but never compared: a change in one alone
    # is not reported
    show: list[str] | None = None
    where: dict[str, str | list[str]] | None = None
    formulas: bool = False
    # Excel: only these cells. ["集計!F20", "B3:D5"], or {合計: 集計!F20} to name what each one is.
    cells: list[str] | dict[str, str] | None = None

    # folder
    recursive: bool = False

    # web / API
    selector: str | None = None  # CSS selector of the part to watch
    remove: list[str] = Field(default_factory=list)  # CSS selectors to drop (counters, ads, ...)
    # Confluence: watch the files attached to the page whose names match (["*.xlsx"]), read as files,
    # instead of the page itself
    attachments: list[str] | None = None
    records: str | None = None  # JSON: dot path to the list of records, e.g. "issues"
    paginate: Literal["none", "offset"] = "none"
    headers: dict[str, str] = Field(default_factory=dict)
    auth: Auth | None = None
    timeout: float = Field(default=30, gt=0)
    verify: bool | str = True  # TLS: true (Windows certificate store), false, or a CA bundle path

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("空にできません")
        return v

    @field_validator("source")
    @classmethod
    def _expand_source(cls, v: str) -> str:
        v = v.strip()
        if v.startswith(("http://", "https://")):
            return v
        return os.path.expanduser(os.path.expandvars(v))

    @field_validator("ignore", "include", "match")
    @classmethod
    def _valid_regex(cls, v: list[str]) -> list[str]:
        for p in v:
            try:
                re.compile(p)
            except re.error as e:
                raise ValueError(f"正規表現の誤り「{p}」: {e}") from e
        return v

    _every_is_default: bool = PrivateAttr(default=False)

    @field_validator("link")
    @classmethod
    def _valid_link(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if "://" in v and not v.lower().startswith(("http://", "https://")):
            raise ValueError("link は http(s):// の URL か、フォルダやファイルのパスにしてください")
        if v.lower().startswith(("javascript:", "file:", "data:")):
            raise ValueError("link は http(s):// の URL か、フォルダやファイルのパスにしてください")
        return v

    @field_validator("every", mode="before")
    @classmethod
    def _valid_every(cls, v):
        return check_every(v)

    @field_validator("expect_update")
    @classmethod
    def _valid_expect_update(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        parse_deadline(v)  # raises ValueError with what to write
        return v.strip()

    @model_validator(mode="after")
    def _command(self) -> Target:
        if self.command is not None:
            if not self.command or not all(isinstance(a, str) and a.strip() for a in self.command):
                raise ValueError("command は、プログラムと引数を並べたリストにしてください（例: [git, log, -n, '20']）")
            if self.fetch not in ("auto", "command"):
                raise ValueError("command と fetch は一緒に使えません")
            if self.source:
                raise ValueError("command のときは source を書かないでください（見張るのはコマンドの出力）")
            self.source = command_line(self.command)
        elif not self.source.strip():
            raise ValueError("source（見張る場所）を書いてください")
        elif self.fetch == "command":
            raise ValueError("fetch: command には command が要ります")
        elif self.format != "text" or self.fields:
            raise ValueError("format と fields は command のときだけ使えます")
        if self.command is not None:
            if self.format == "xml" and not self.records:
                raise ValueError("format: xml には records（1件ごとにくり返す要素の名前）が要ります")
            if self.fields and self.format not in ("csv", "tsv"):
                raise ValueError("fields は format: csv / tsv のときだけ使えます")
            if self.format == "text" and self.uses_header:
                raise ValueError("key / columns / where は、format が json・csv・tsv・xml のときに使えます")
        return self

    @model_validator(mode="after")
    def _plan_tasks_by_uid(self) -> Target:
        """A Project plan's tasks are told apart by UID unless the config says otherwise: ID is the row
        number and shifts for every task below a row inserted above it."""
        plan = Path(self.source.split("?")[0]).suffix.lower() in PLAN_SUFFIXES
        if plan and self.key is None and not self.cells and self.watch != "timestamp" and self.command is None:
            self.key = "UID"
        return self

    @model_validator(mode="after")
    def _consistent(self) -> Target:
        if self.show is not None:
            if not self.key:
                raise ValueError("show（見るだけの列）は key（行を見分ける列）と一緒に使います")
            if not [c for c in self.show if c.strip()]:
                raise ValueError("show が空です。変わった行と一緒に見たい列の見出しを書いてください")
            both = [c for c in self.show if c in (self.columns or []) or c == self.key]
            if both:
                raise ValueError(
                    f"{', '.join(both)} が show と columns（または key）の両方にあります。"
                    "変わったら知らせる列なら columns に、一緒に見るだけなら show に書いてください"
                )
        if self.header_row == "auto" and not (self.key or self.columns):
            raise ValueError("header_row: auto には key か columns が要ります（見出しを探す手がかり）")
        if self.kind in ("web", "json", "confluence", "download") and not self.source.startswith(
            ("http://", "https://")
        ):
            raise ValueError(f"fetch: {self.kind} の source は http(s):// で始まる URL にしてください")
        if self.command is None and self.source.startswith(("http://", "https://")):
            address = sharepoint.classify(self.source)
            if address is not None and address.form != "file":  # a link for a browser, not the file
                raise ValueError(address.advice(self.source))
        if self.attachments is not None:
            if self.kind != "confluence" or ".atlassian.net" in self.source or "/rest/api/" in self.source:
                raise ValueError(
                    "attachments は Confluence Server / Data Center のページの URL のときだけ使えます"
                    "（例: attachments: ['*.xlsx']）"
                )
            if not [p for p in self.attachments if p.strip()]:
                raise ValueError(
                    "attachments が空です。見る添付の名前を書いてください（例: ['*.xlsx']。* で何文字でも）"
                )
        if self.paginate != "none" and self.kind != "json":
            raise ValueError("paginate は fetch: json のときだけ使えます")
        if self.watch == "timestamp":
            if self.kind != "file":
                raise ValueError(
                    "watch: timestamp は、1つのファイル（パスで書くもの）だけに使えます"
                    "（フォルダは、もともとサイズと更新日時で比べる）"
                )
            if self.cells:
                raise ValueError("watch: timestamp と cells は一緒に使えません（timestamp は中身を開かない）")
        if self.cells is not None:
            self._check_cells()
        if self.rules and not (self.is_keyed and not self.is_folder and self.watch != "timestamp"):
            raise ValueError(
                "rules は、行ごとに見る見張り（key のある Excel・CSV・JSON）と、決まったセル（cells）だけに書けます"
            )
        return self

    def _check_cells(self) -> None:
        if not self.cells:
            raise ValueError("cells が空です。見るセルを書いてください（例: [集計!F20, B3:D5]）")
        excel = Path(self.source.split("?")[0]).suffix.lower() in (".xlsx", ".xlsm") and self.kind in (
            "file",
            "office",
            "download",
        )
        if not excel and not self.attachments:  # attached files: the cells of each one
            raise ValueError("cells は Excel（.xlsx / .xlsm）のときだけ使えます")
        if self.key or self.columns or self.where:
            raise ValueError("cells と key / columns / where は一緒に使えません（セルを見るか、行を見るか）")
        try:
            cell_refs(self)
        except CellRefError as e:
            raise ValueError(str(e)) from e

    # ------------------------------------------------------------------ derived

    @property
    def store_key(self) -> str:
        return self.id or self.name

    @property
    def kind(self) -> str:
        if self.command is not None:
            return "command"
        if self.fetch != "auto":
            return self.fetch
        s = self.source
        if s.startswith(("http://", "https://")):
            low = s.lower().split("?")[0]
            if (
                "/rest/api/content/" in low
                or "/pages/" in low
                and ("atlassian.net/wiki" in low or "pageid=" in s.lower())
                # /display/SPACE/Title is Confluence's own, but too common a word to go on alone
                or "/display/" in low
                and "confluence" in low
            ):
                return "confluence"
            if "sharepoint.com" in low and low.endswith((".xlsx", ".xlsm", ".docx")):
                return "office"
            if url_suffix(s) in DOWNLOAD_SUFFIXES or "/download/attachments/" in low:
                return "download"
            if low.endswith(".json") or self.records:
                return "json"
            return "web"
        if "*" in s or "?" in s or s.endswith(("/", "\\")):
            return "folder"
        # A name ending like a file is taken for one without asking the disk: on a share the ask is a
        # network round trip, seconds long for a server that does not answer, paid on every start.
        if Path(s).suffix.lower() in FILE_SUFFIXES:
            return "file"
        return "folder" if _is_folder(s) else "file"

    @property
    def is_folder(self) -> bool:
        return self.kind == "folder"

    @property
    def uses_header(self) -> bool:
        return bool(self.key or self.columns or self.where)

    @property
    def is_keyed(self) -> bool:
        return self.is_folder or bool(self.key) or bool(self.cells) or self.watch == "timestamp"

    @property
    def interval(self) -> dt.timedelta | None:
        return every_delta(self.every) if self.every else None

    @property
    def every_is_default(self) -> bool:
        """every came from the config's every, not from this target."""
        return self._every_is_default


class Notify(Strict):
    drop_folder: str  # one JSON file per notification (folder hand-off format v1)
    on_failure: bool = True

    @field_validator("drop_folder")
    @classmethod
    def _expand(cls, v: str) -> str:
        return os.path.expanduser(os.path.expandvars(v.strip()))


class CheckHours(Strict):
    """When checks by interval run (the scheduler, the page's auto check, check --due). A check asked for by hand always runs."""

    days: str | list[str] = "平日"  # 平日 / 毎日 / [月, 火, 水, 木, 金]
    time: str = "0:00-24:00"

    @field_validator("days")
    @classmethod
    def _valid_days(cls, v):
        days = [v] if isinstance(v, str) else v
        if not days:
            raise ValueError("days が空です")
        for d in days:
            if not day_set(d):
                raise ValueError(f"days の「{d}」が分かりません。平日・毎日・土日、または 月〜日 で書いてください")
        return v

    @field_validator("time", mode="before")
    @classmethod
    def _valid_time(cls, v):
        if not isinstance(v, str):
            raise ValueError("「9:00-19:00」のように、引用符で囲んで書いてください（time: '9:00-19:00'）")
        m = _TIME_RANGE.match(v)
        if not m:
            raise ValueError("「9:00-19:00」のように書いてください")
        h1, m1, h2, m2 = map(int, m.groups())
        if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and m1 < 60 and m2 < 60) or (h1, m1) >= (h2, m2) or h2 * 60 + m2 > 1440:
            raise ValueError(
                "始まりを終わりより前にして、0:00〜24:00 の中で書いてください（日をまたぐ時間帯は書けません）"
            )
        return v

    @property
    def weekdays(self) -> set[int]:
        days = [self.days] if isinstance(self.days, str) else self.days
        return set().union(*(day_set(d) for d in days))

    @property
    def minutes(self) -> tuple[int, int]:
        h1, m1, h2, m2 = map(int, _TIME_RANGE.match(self.time).groups())  # type: ignore[union-attr]
        return h1 * 60 + m1, h2 * 60 + m2

    def allows(self, at: dt.datetime) -> bool:
        start, end = self.minutes
        return at.weekday() in self.weekdays and start <= at.hour * 60 + at.minute < end

    def next_start(self, at: dt.datetime) -> dt.datetime:
        """The first moment at or after `at` when checks may run."""
        if self.allows(at):
            return at
        start, _ = self.minutes
        for i in range(8):
            day = (at + dt.timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            begin = day + dt.timedelta(minutes=start)
            if day.weekday() in self.weekdays and begin >= at:
                return begin
        return at  # unreachable with at least one day

    @property
    def label(self) -> str:
        days = self.days if isinstance(self.days, str) else "・".join(str(d) for d in self.days)
        start, end = self.minutes
        return f"{days} {start // 60}:{start % 60:02d}〜{end // 60}:{end % 60:02d}"


class Config(Strict):
    every: str = DEFAULT_EVERY  # the interval of a target that does not say
    check_hours: CheckHours | None = None
    keep: int = Field(default=5, ge=1, le=100)
    max_diff_lines: int = Field(default=300, ge=10)
    keep_originals: bool = True
    workers: int = Field(default=4, ge=1, le=32)
    data_dir: str | None = None  # where copies are kept (default: "data" next to this file)
    vars: dict[str, str] = Field(default_factory=dict)
    include: list[str] = Field(default_factory=list)
    notify: Notify | None = None
    targets: list[Target] = Field(default_factory=list)

    @field_validator("every", mode="before")
    @classmethod
    def _valid_every(cls, v):
        if v is None:
            raise ValueError("every を空にはできません（既定は 30m）")
        return check_every(v)

    @model_validator(mode="after")
    def _default_every(self) -> Config:
        for t in self.targets:
            if t.every is None:
                t.every = self.every
                t._every_is_default = True
        return self

    @model_validator(mode="after")
    def _unique(self) -> Config:
        names: set[str] = set()
        folders: dict[str, str] = {}
        for t in self.targets:
            if t.name in names:
                raise ValueError(f"name「{t.name}」が2回以上あります。name は見張りごとに変えてください")
            names.add(t.name)
            # Windows folder names ignore case, and some characters are replaced, so two
            # different names can end up in the same data folder and mix their history.
            folder = safe_name(t.store_key).casefold()
            if folder in folders:
                raise ValueError(
                    f"「{folders[folder]}」と「{t.store_key}」は同じデータのフォルダになります"
                    "（大文字小文字や記号だけの違い）。どちらかに id を付けて分けてください"
                )
            folders[folder] = t.store_key
        return self

    def data_path(self, config_path: Path) -> Path:
        if self.data_dir:
            p = Path(os.path.expanduser(os.path.expandvars(self.data_dir)))
            return p if p.is_absolute() else config_path.parent / p
        return config_path.parent / "data"


def resolve_config_path(explicit: Path | None) -> Path:
    if explicit:
        return explicit
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env)
    return DEFAULT_CONFIG


def _is_shared(path: Path) -> bool:
    """On a network share or a mapped network drive: a place other people may be able to write."""
    p = str(path.resolve() if path.exists() else path.absolute())
    if p.startswith(("\\\\", "//")):
        return True
    if os.name == "nt" and len(p) >= 2 and p[1] == ":":
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(f"{p[:2]}\\") == 4  # DRIVE_REMOTE
    return False


def _yaml_error(path: Path, source: str, e: yaml.YAMLError) -> str:
    """Where the YAML went wrong, the line itself, and the likely fix for the usual slips."""
    mark = getattr(e, "problem_mark", None)
    problem = str(getattr(e, "problem", "") or e).strip()
    out = [f"設定ファイルの書き方に誤りがあります: {path}"]
    line = ""
    if mark is not None:
        lines = source.split("\n")
        line = lines[mark.line] if mark.line < len(lines) else ""
        out.append(f"  {mark.line + 1} 行目: {line.strip()}")
    before_var = line.split("${", 1)[0] if "${" in line else ""
    if "{" in before_var or "[" in before_var:
        out.append("  { } や [ ] の中に ${…} を書くときは、'${me}' のように引用符で囲んでください")
    elif "\t" in problem or "'\\t'" in problem:
        out.append("  タブは使えません。字下げは空白にしてください")
    elif "mapping values are not allowed" in problem:
        out.append("  値に「: 」を含むとき（URL や時刻など）は、引用符で囲んでください。字下げのずれでも起きます")
    out.append(f"  （YAML: {problem}）")
    return "\n".join(out)


def _read_yaml(path: Path, text: str | None = None) -> dict:
    source = path.read_text(encoding="utf-8") if text is None else text
    try:
        raw = yaml.safe_load(source) or {}
    except yaml.YAMLError as e:
        raise ConfigError(_yaml_error(path, source, e)) from e
    if not isinstance(raw, dict):
        raise ConfigError(f"設定ファイルの一番上は「キー: 値」の形にしてください: {path}")
    return raw


def _substitute(value, variables: dict[str, str], where: str):
    if isinstance(value, str):

        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name not in variables:
                raise ConfigError(f"{where}: 変数 ${{{name}}} が vars にありません")
            return variables[name]

        return _VAR.sub(repl, value)
    if isinstance(value, list):
        return [_substitute(v, variables, where) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, variables, where) for k, v in value.items()}
    return value


def load_config(path: Path, text: str | None = None, check_commands: bool = True) -> Config:
    """Read the config at path. With text, validate that draft as if it were saved at path.

    A command outside the read-only list makes the config invalid (see commands.py). Only
    `watchpoint allow --unsafe` reads the config without that check, to allow one in a terminal.
    """
    if text is None and not path.exists():
        raise ConfigError(
            f"設定ファイルが見つかりません: {path}\n  `watchpoint init` で作るか、--config で場所を指定してください"
        )
    raw = _read_yaml(path, text)
    variables = {str(k): str(v) for k, v in (raw.get("vars") or {}).items()}

    targets = list(raw.get("targets") or [])
    for pattern in raw.get("include") or []:
        pattern = os.path.expandvars(os.path.expanduser(str(pattern)))
        if not os.path.isabs(pattern):
            pattern = str(path.parent / pattern)
        for extra in sorted(globmod.glob(pattern)):
            sub = _read_yaml(Path(extra))
            unknown = set(sub) - {"targets"}
            if unknown:
                raise ConfigError(
                    f"取り込んだ設定 {extra} に書けるのは targets だけです（{', '.join(sorted(unknown))}）"
                )
            for t in sub.get("targets") or []:
                if isinstance(t, dict) and "command" in t:
                    raise ConfigError(
                        f"取り込んだ設定 {extra} には command を書けません。コマンドは自分の設定ファイルにだけ書けます"
                    )
            targets += list(sub.get("targets") or [])
    if any(isinstance(t, dict) and "command" in t for t in targets) and _is_shared(path):
        raise ConfigError(
            f"共有フォルダにある設定ファイルには command を書けません（ほかの人が書き換えて、あなたの権限でプログラムを動かせてしまうため）: {path}"
        )
    raw["targets"] = [
        _substitute(t, variables, f"targets の「{t.get('name', '?')}」") if isinstance(t, dict) else t for t in targets
    ]

    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        lines = [f"設定ファイルの内容に誤りがあります: {path}"]
        for err in e.errors():
            lines.append(f"  {_format_loc(err['loc'], raw)}: {_translate(err)}")
        raise ConfigError("\n".join(lines)) from e
    if check_commands:
        for t in cfg.targets:
            problem = safety_problem(t.command) if t.command else None
            if problem and not allowed_despite(t.command):  # type: ignore[arg-type]
                raise ConfigError(f"設定ファイルの内容に誤りがあります: {path}\n  " + unsafe_message(t.name, problem))
    return cfg


def _format_loc(loc: tuple, raw: dict) -> str:
    parts: list[str] = []
    for i, p in enumerate(loc):
        if isinstance(p, int) and i > 0 and loc[i - 1] == "targets":
            name = None
            try:
                name = raw["targets"][p].get("name")
            except (KeyError, IndexError, AttributeError, TypeError):
                pass
            parts.append(f"{p + 1}件目" + (f"（{name}）" if name else ""))
        elif isinstance(p, str) and p.startswith(("function-after", "function-before")):
            continue
        else:
            parts.append(str(p))
    return " > ".join(parts) or "(全体)"


def _translate(err: dict) -> str:
    kind = err.get("type", "")
    if kind == "missing":
        return "必須の項目がありません"
    if kind == "extra_forbidden":
        return "知らない項目です（書き間違いではありませんか）"
    if kind == "literal_error":
        return f"使える値ではありません（{err.get('ctx', {}).get('expected', '')}）"
    if kind == "string_type":
        return "文字列で書いてください"
    if kind in ("int_parsing", "int_type", "float_parsing", "float_type"):
        return "数字で書いてください"
    if kind == "bool_parsing":
        return "true か false で書いてください"
    if kind == "list_type":
        return "リスト（- で始まる行、または [a, b]）で書いてください"
    if kind == "dict_type":
        return "「キー: 値」の形で書いてください"
    if kind == "string_too_short":
        return "空にできません"
    if kind in ("greater_than_equal", "less_than_equal", "greater_than"):
        return f"範囲外の値です（{err.get('msg')}）"
    return str(err.get("msg", "")).removeprefix("Value error, ")


TEMPLATE = r"""
# watchpoint の設定
# 見張りごとに、どこを見るか（source）と、何を読み取ってほしいか（points）を書く。
# 元のファイルは読むだけ。毎回手元にコピーしてから比べる。書ける項目は README を参照。
# 下のサンプルはどれも enabled: false（取りに行かない）。直して使うときは enabled の行を消す。
# 画面（watchpoint gui）の「見張りを追加」でも、ファイルの中を見ながら足せる。
#
# 表（Excel・CSV）の見方の選び方:
#   - 表の列を見る（質問の列、ステータスの列など）→ 行ごとに見る: key（行を見分ける番号の列）と
#     columns（変わったら知らせる列）。一緒に見るだけの列は show。行が挿入されても、ずれない
#   - 合計・承認欄のような、決まった少しのセルを見る → cells。番地で比べるので、表の列には使わない
#     （行が 1 つ挿入されると、その下が全部「変わった」と出る）。上限に当たっても、見張りを分けて避けない

every: 30m           # 自動チェックの間隔（見張りごとの every が無いとき）。5m より短くはできない
keep: 5              # 残しておく履歴の数
max_diff_lines: 300  # 1件あたりに出す差分の行数の上限
keep_originals: true # false にすると、取ってきた原本は残さずテキストだけ残す（容量の節約）

vars:
  me: 佐藤            # 自分の名前に直す。${me} で参照できる（where: {回答者: '${me}'} など。{ } の中では引用符で囲む）

# check_hours:        # 自動チェックをする時間帯（手で押すチェックはいつでもできる）
#   days: 平日         # 平日 / 毎日 / [月, 水, 金]
#   time: '9:00-19:00'

# include: [targets.d/*.yaml]   # 見張りを別のファイルに分けるとき

# notify:
#   drop_folder: '%LOCALAPPDATA%\Notifier\queue'   # check --notify で、変更ごとに JSON を1件置く

targets:
  - name: 質問表（サンプル）
    source: '\\server\share\project\質問表.xlsx'   # %USERPROFILE% や ~ も使える
    tags: [〇〇案件]
    every: 1h            # この見張りだけ間隔を変えるとき（書かなければ上の every）
    sheets: [質問一覧]
    header_row: auto     # key の見出しがある行を探す（数字でも可）
    key: No.
    columns: [質問, 回答者, 状態, 回答]
    # show: [期限]       # 見るだけの列（変わった行と一緒に出すが、この列だけの変化は知らせない）
    where:
      回答者: ${me}
    report: [added, changed]   # 削除は数えない
    points: |
      追加された行は新しい質問。それぞれの要旨を1行で。
      状態が変わった行は、何から何に変わったかを挙げる。
    notify_when: 自分宛ての新しい質問があるとき
    enabled: false

  - name: 仕様書（サンプル）
    source: 'C:\path\to\spec.docx'
    sections: [ログ]     # この見出しの下だけを見る
    ignore: ['^最終更新']
    normalize: [whitespace, nfkc]
    enabled: false

  - name: 議事録フォルダ（サンプル）
    source: '\\server\share\project\議事録\*.docx'
    report: [added]
    enabled: false

  - name: 設計ページ（サンプル）
    source: https://confluence.example.com/pages/viewpage.action?pageId=123456
    fetch: confluence
    auth: {type: bearer, secret: confluence}   # watchpoint secret set confluence
    enabled: false

  - name: 自分の不具合チケット（サンプル）
    source: https://redmine.example.com/issues.json?assigned_to_id=me&tracker_id=1&status_id=*&limit=100
    paginate: offset                    # status_id=* なら、完了にしたチケットも「状態の変化」として出る
    key: id
    columns: [subject, status.name, priority.name, updated_on]
    auth: {type: header, header: X-Redmine-API-Key, secret: redmine}
    enabled: false
""".lstrip("\n")
