"""What the page and the commands do: check, acknowledge, show, and edit the config.

Both the page (gui.py, desktop.py) and the commands (cli.py) call these operations, so a rule
is tried, a target is added or a check is started the same way wherever it is asked for. Nothing
here knows about HTTP or the window; errors meant for the person are raised as ApiError.
"""

from __future__ import annotations

import codecs
import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from pathlib import Path

from pydantic import ValidationError

from . import commands, editing
from .check import Stale, Status, acknowledge, build_text, check_target, has_pending
from .commands import is_allowed
from .config import Config, ConfigError, Rule, Target, load_config
from .lock import Busy, data_lock
from .peek import peek as peek_source
from .peek import peek_office, peek_sample
from .report import (
    as_markdown,
    changes_between,
    generations,
    kind_label,
    latest_changes,
    next_due,
    saved_results,
)
from .rules import alerts_for, field_stats, rule_alerts
from .schedule import heartbeat_view
from .store import TargetStore, replace_file
from .viewing import PLACE_LABEL, open_place, place_of, show

LOCK_WAIT_SECONDS = 5
PREVIEW_LINES = 500
LOG_LINES = 200
CRLF, LF = "\r\n", "\n"


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def _alerts(t: Target, data_dir: Path, state) -> list[str]:
    if not (t.enabled and (t.rules or t.expect_update)):
        return []
    try:
        return alerts_for(t, TargetStore(data_dir, t.store_key), state)
    except Exception as e:  # noqa: BLE001 - shown, not raised
        return [f"ルールを確かめられませんでした: {e}"]


def _place_view(t: Target) -> dict | None:
    place = place_of(t)
    return {"label": PLACE_LABEL[place[0]], "where": place[1]} if place else None


def _command_allowed(t: Target) -> bool | None:
    if not t.command:
        return None
    return is_allowed(t.command)


def _hours_view(cfg: Config) -> dict | None:
    h = cfg.check_hours
    if h is None:
        return None
    now = dt.datetime.now()
    return {"label": h.label, "now": h.allows(now), "next": f"{h.next_start(now):%m/%d %H:%M}"}


def _now() -> str:
    return dt.datetime.now().isoformat(sep=" ", timespec="seconds")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class CheckJob:
    """One run of check started from the page. The page polls it to show progress."""

    id: int
    total: int
    due: bool
    via: str = "page"
    started: str = field(default_factory=_now)
    running: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    finished: str | None = None
    error: str | None = None

    def view(self) -> dict:
        changed = sum(r["status"] == Status.CHANGED.value for r in self.results)
        failed = sum(r["status"] == Status.FAILED.value for r in self.results)
        return {**asdict(self), "changed": changed, "failed": failed, "active": self.finished is None}


class App:
    """The operations behind the page. The config is read again on every call, so edits show up at once."""

    def __init__(self, config_path: Path, picker=None):
        self.config_path = config_path
        self.picker = picker  # shows the file dialog: the window's own, or gui.pick_path for the browser page
        self.token = secrets.token_urlsafe(24)
        self._saving = threading.Lock()  # one config save at a time
        self._browsing = threading.Lock()
        self._jobs = threading.Lock()  # guards self.job
        self.job: CheckJob | None = None
        self._job_ids = 0

    # ------------------------------------------------------------------ helpers

    def _config(self, text: str | None = None) -> tuple[Config, Path]:
        try:
            cfg = load_config(self.config_path, text)
        except ConfigError as e:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
        return cfg, cfg.data_path(self.config_path)

    @classmethod
    def _targets(cls, cfg: Config, names: list[str] | None) -> list[Target]:
        return cfg.targets if names is None else [cls._target(cfg, n) for n in names]

    @staticmethod
    def _target(cfg: Config, name: str) -> Target:
        for t in cfg.targets:
            if t.name == name:
                return t
        raise ApiError(HTTPStatus.NOT_FOUND, f"見張りが見つかりません: {name}")

    @staticmethod
    def _locked(data_dir: Path, fn):
        try:
            with data_lock(data_dir, wait=LOCK_WAIT_SECONDS):
                return fn()
        except Busy as e:
            raise ApiError(HTTPStatus.CONFLICT, str(e))

    # ------------------------------------------------------------------ reads

    def overview(self) -> dict:
        cfg, data_dir = self._config()
        items = []
        for t in cfg.targets:
            s = TargetStore(data_dir, t.store_key).load()
            items.append(
                {
                    "name": t.name,
                    "kind": kind_label(t),
                    "source": t.source,
                    "tags": t.tags,
                    "every": t.every,
                    "enabled": t.enabled,
                    "status": s.last_status or "未チェック",
                    "pending": has_pending(s),
                    "snapshot": s.snapshot,
                    "last_checked": s.last_checked,
                    "last_changed": s.last_changed,
                    "error": s.last_error,
                    "next": next_due(t, s, cfg.check_hours),
                    "alerts": _alerts(t, data_dir, s),
                }
            )
        with self._jobs:
            job = self.job.view() if self.job else None
        return {
            "config": str(self.config_path),
            "data_dir": str(data_dir),
            "job": job,
            "schedule": heartbeat_view(data_dir),
            "every_default": cfg.every,
            "check_hours": _hours_view(cfg),
            "targets": items,
        }

    def detail(self, name: str) -> dict:
        cfg, data_dir = self._config()
        t = self._target(cfg, name)
        store = TargetStore(data_dir, t.store_key)
        state = store.load()
        ch = latest_changes(t, cfg, store, state)
        return {
            "name": t.name,
            "snapshot": state.snapshot,
            "kind": kind_label(t),
            "source": t.source,
            "tags": t.tags,
            "points": t.points.strip(),
            "notify_when": t.notify_when.strip(),
            "status": state.last_status or "未チェック",
            "error": state.last_error,
            "pending": has_pending(state),
            "last_checked": state.last_checked,
            "meta": state.meta,
            "enabled": t.enabled,
            "alerts": _alerts(t, data_dir, state),
            "rules": [r.model_dump(exclude_none=True, exclude_defaults=True) for r in t.rules],
            "expect_update": t.expect_update,
            "can_have_rules": t.is_keyed and not t.is_folder and t.watch != "timestamp",
            "place": _place_view(t),
            "command": t.command,
            "command_allowed": _command_allowed(t),
            "every": t.every,
            "every_is_default": t.every_is_default,
            "every_default": cfg.every,
            "next": next_due(t, state, cfg.check_hours),
            "check_hours": _hours_view(cfg),
            "has_copy": bool(state.snapshot),
            "changes": asdict(ch) if ch else None,
            "history": [asdict(g) for g in generations(store, state)],
        }

    def diff(self, name: str, a: int, b: int) -> dict:
        cfg, data_dir = self._config()
        t = self._target(cfg, name)
        store = TargetStore(data_dir, t.store_key)
        state = store.load()
        try:
            ia, ib = store.generation(state, a), store.generation(state, b)
        except IndexError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e))
        return asdict(changes_between(t, cfg, store, state, ia, ib))

    def text(self, name: str, gen: int) -> dict:
        cfg, data_dir = self._config()
        t = self._target(cfg, name)
        store = TargetStore(data_dir, t.store_key)
        state = store.load()
        try:
            snap_id = store.generation(state, gen)
        except IndexError as e:
            raise ApiError(HTTPStatus.BAD_REQUEST, str(e))
        return {"text": store.read_text(snap_id) or ""}

    def log(self) -> dict:
        _, data_dir = self._config()
        f = data_dir / "log.jsonl"
        entries = []
        if f.exists():
            with f.open(encoding="utf-8", errors="replace") as fh:  # one broken line must not hide the rest
                for line in fh:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        return {"entries": entries[-LOG_LINES:]}

    def markdown(self, names: list[str] | None, chat: bool = True, places: bool = False) -> dict:
        """The unacknowledged changes and failures, as text to paste into an AI chat.

        Returns what was put in (`seen`: the copy each change came from), so acknowledging afterwards
        covers exactly what was read and not a change that arrived since.
        """
        cfg, data_dir = self._config()
        targets = self._targets(cfg, names)
        results = saved_results(targets, cfg, data_dir)
        changed = [r for r in results if r.status is Status.CHANGED]
        return {
            "text": as_markdown(results, total=len(targets), chat=chat, places=places),
            "count": len(results),
            "changed": len(changed),
            "failed": len(results) - len(changed),
            "seen": {r.target.name: r.generation for r in changed if r.generation},
        }

    # ------------------------------------------------------------------ check (in the background)

    def start_check(self, names: list[str] | None, due: bool, via: str = "page") -> dict:
        cfg, data_dir = self._config()
        targets = self._targets(cfg, names)
        if not targets:
            raise ApiError(HTTPStatus.BAD_REQUEST, "チェックする見張りがありません")
        with self._jobs:
            if self.job and self.job.finished is None:
                raise ApiError(HTTPStatus.CONFLICT, "いまチェック中です。終わってからもう一度押してください")
            self._job_ids += 1
            job = self.job = CheckJob(self._job_ids, len(targets), due, via)
        threading.Thread(target=self._run_check, args=(job, targets, cfg, data_dir), daemon=True).start()
        return job.view()

    def _run_check(self, job: CheckJob, targets: list[Target], cfg: Config, data_dir: Path) -> None:
        def one(t: Target) -> None:
            with self._jobs:
                job.running.append(t.name)
            try:
                r = check_target(t, cfg, data_dir, due_only=job.due, via=job.via)
                entry = {"name": t.name, "status": r.status.value, "message": r.message}
            except Exception as e:  # noqa: BLE001 - check_target reports per target; this is a last resort
                entry = {"name": t.name, "status": Status.FAILED.value, "message": str(e)}
            with self._jobs:
                job.running.remove(t.name)
                job.results.append(entry)

        try:
            with data_lock(data_dir, wait=LOCK_WAIT_SECONDS):
                workers = max(1, min(cfg.workers, len(targets)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    list(pool.map(one, targets))
        except Busy as e:
            job.error = str(e)
        except Exception as e:  # noqa: BLE001 - shown on the page
            job.error = str(e) or type(e).__name__
        finally:
            with self._jobs:
                job.finished = _now()

    def job_view(self) -> dict:
        with self._jobs:
            return {"job": self.job.view() if self.job else None}

    # ------------------------------------------------------------------ other actions

    def ack(self, names: list[str] | None, note: str | None, seen: dict[str, str] | None = None) -> dict:
        """seen: target name -> the snapshot shown on the page. A target with a newer copy is left as it is."""
        cfg, data_dir = self._config()
        targets = self._targets(cfg, names)
        seen = seen or {}

        def run() -> tuple[list[str], list[str]]:
            done, stale = [], []
            for t in targets:
                try:
                    if acknowledge(t, cfg, data_dir, note=note or None, seen=seen.get(t.name)):
                        done.append(t.name)
                except Stale:
                    stale.append(t.name)
            return done, stale

        done, stale = self._locked(data_dir, run)
        return {"done": done, "stale": stale}

    def preview(self, name: str, draft: str | None = None) -> dict:
        cfg, _ = self._config(draft)
        t = self._target(cfg, name)
        with tempfile.TemporaryDirectory(prefix="watchpoint-") as tmp:
            try:
                text, _ = build_text(t, Path(tmp))
            except Exception as e:  # noqa: BLE001 - shown on the page as it is
                raise ApiError(HTTPStatus.BAD_GATEWAY, f"取得できませんでした: {e}")
        lines = text.splitlines()
        return {"text": "\n".join(lines[:PREVIEW_LINES]), "total": len(lines), "kind": kind_label(t)}

    def open_place(self, name: str) -> dict:
        """Show where the target is: its folder (the file selected, not opened), a page, or its `link:`."""
        cfg, _ = self._config()
        t = self._target(cfg, name)
        place = place_of(t)
        if place is None:
            raise ApiError(
                HTTPStatus.NOT_FOUND, "開く場所がありません。設定で link: に URL かフォルダを書くと、ここから開けます"
            )
        try:
            open_place(*place)
        except (OSError, ValueError) as e:
            raise ApiError(HTTPStatus.NOT_FOUND, str(e))
        return {"how": place[0], "where": place[1]}

    def open_copy(self, name: str, as_text: bool) -> dict:
        cfg, data_dir = self._config()
        t = self._target(cfg, name)
        store = TargetStore(data_dir, t.store_key)
        state = store.load()
        if not state.snapshot:
            raise ApiError(HTTPStatus.NOT_FOUND, "まだコピーがありません。先にチェックしてください")
        snap = store.snapshot_dir(state.snapshot)
        originals = sorted(snap.glob("original*"))
        path = snap / "text.txt" if as_text or not originals else originals[0]
        if not path.exists():
            raise ApiError(HTTPStatus.NOT_FOUND, f"見つかりません: {path}")
        opened, direct = show(path, t.name, state.snapshot)
        return {"opened": str(opened), "shown_in_folder": not direct}

    # ------------------------------------------------------------------ the config file

    def config_text(self) -> dict:
        try:
            data = self.config_path.read_bytes()
        except OSError as e:
            raise ApiError(HTTPStatus.NOT_FOUND, f"設定ファイルを読めません: {e}")
        # The page edits LF text without a BOM (a browser text box always does); save_config puts them back.
        text = data.decode("utf-8-sig").replace(CRLF, LF)
        return {"text": text, "hash": _digest(data), "path": str(self.config_path)}

    def validate(self, text: str) -> dict:
        try:
            cfg = load_config(self.config_path, text)
        except ConfigError as e:
            return {"ok": False, "error": str(e), "targets": []}
        return {"ok": True, "error": None, "targets": [t.name for t in cfg.targets]}

    def peek(self, source: str) -> dict:
        """The sheets and first rows of a source, for clicking a header, a key or cells on the form."""
        return self._with_vars(peek_source(source.strip().strip('"')))

    def peek_office(self, source: str) -> dict:
        """The same for an Excel file on SharePoint, through a copy a hidden Excel saves (slow)."""
        return self._with_vars(peek_office(source.strip().strip('"')))

    def peek_sample(self, path: str) -> dict:
        """The same from a copy the person downloaded by hand; the target keeps its URL."""
        return self._with_vars(peek_sample(path))

    def _with_vars(self, out: dict) -> dict:
        try:
            out["vars"] = load_config(self.config_path).vars  # to offer "only my rows" with ${me}
        except ConfigError:
            out["vars"] = {}
        return out

    def browse(self, folder: bool, start: str | None) -> dict:
        """Show the Windows file (or folder) dialog on this PC and return the chosen path."""
        with self._browsing:  # one dialog at a time
            if self.picker is None:
                raise ApiError(HTTPStatus.CONFLICT, "ここからはファイルを選べません。パスを書いてください")
            return {"path": self.picker(folder, start)}

    def add_and_save(self, item: object) -> dict:
        """Add a target from the form to the saved config file and save it, in one step."""
        with self._saving:
            data = self.config_path.read_bytes() if self.config_path.exists() else b""
            text = data.decode("utf-8-sig").replace(CRLF, LF)
            draft = self.add_target(text, item)
            if not draft["ok"]:
                raise ApiError(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "設定ファイルのほかの所に誤りがあるので、保存しませんでした。「設定」で直してください\n"
                    + draft["error"],
                )
            saved = self._save_config(draft["text"], draft["targets"], _digest(data))
        return {"name": draft["name"], **saved}

    def allow_command(self, name: str, command: list) -> dict:
        """Allow a target's command. The page sends the command it showed, so what was read is what is allowed."""
        cfg, _ = self._config()
        t = self._target(cfg, name)
        if not t.command:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"「{name}」はコマンドで見張っていません")
        if command != t.command:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "設定ファイルのコマンドが、画面に出したあとで変わりました。開き直して確かめてください",
            )
        commands.allow(t.command, t.name)
        return {"name": t.name, "allowed": True}

    def allowed_commands(self) -> dict:
        """The commands allowed on this PC, with the targets (in this config) that use each one."""
        try:
            cfg, _ = self._config()
            targets = cfg.targets
        except ApiError:
            targets = []
        entries = commands.allowed_list()
        for e in entries:
            e["used_by"] = [t.name for t in targets if t.command == e["command"]]
        waiting = [
            {"name": t.name, "line": t.source} for t in targets if t.command and not commands.is_allowed(t.command)
        ]
        return {"entries": entries, "waiting": waiting}

    def revoke_commands(self, ids: object) -> dict:
        if not (isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids)):
            raise ValueError("ids は文字列の並び")
        return {"revoked": commands.revoke(ids)}

    # ------------------------------------------------------------------ rules and deadlines

    def fields(self, name: str) -> dict:
        """How filled each field of the latest copy is and which values it takes: what rules are proposed from."""
        cfg, data_dir = self._config()
        t = self._target(cfg, name)
        store = TargetStore(data_dir, t.store_key)
        state = store.load()
        text = store.read_text(state.snapshot) if state.snapshot else None
        if text is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "まだコピーがありません。先にチェックしてください")
        return {"name": t.name, "fields": field_stats(text), "snapshot": state.snapshot}

    def try_rule(self, name: str, rule: object) -> dict:
        """What a rule would flag now, before it is saved."""
        cfg, data_dir = self._config()
        t = self._target(cfg, name)
        try:
            r = Rule.model_validate({k: v for k, v in dict(rule).items() if v not in (None, "", [], {}, False)})  # type: ignore[arg-type]
        except (ValidationError, TypeError, ValueError) as e:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, editing.validation_message(e))
        store = TargetStore(data_dir, t.store_key)
        state = store.load()
        text = store.read_text(state.snapshot) if state.snapshot else None
        if text is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "まだコピーがありません。先にチェックしてください")
        return {"alerts": rule_alerts(t.model_copy(update={"rules": [r]}), text)}

    def add_rule(self, name: str, rule: object) -> dict:
        if not isinstance(rule, dict):
            raise ValueError("rule はオブジェクト")
        return self._edit(lambda text: editing.add_rule(text, name, rule))

    def remove_rule(self, name: str, index: int) -> dict:
        return self._edit(lambda text: editing.remove_rule(text, name, index))

    def set_expect_update(self, name: str, when: str | None) -> dict:
        return self._edit(lambda text: editing.set_expect_update(text, name, when))

    def _edit(self, change) -> dict:
        """Change the saved config with one edit, keeping the rest of the file, after checking the result."""
        with self._saving:
            data = self.config_path.read_bytes() if self.config_path.exists() else b""
            text = data.decode("utf-8-sig").replace(CRLF, LF)
            try:
                new = change(text)
            except (ValueError, ValidationError) as e:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, editing.validation_message(e))
            checked = self.validate(new)
            if not checked["ok"]:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, checked["error"])
            return self._save_config(new, checked["targets"], _digest(data))

    def set_every(self, name: str, every: str | None) -> dict:
        """Change one target's interval in the saved config file (None: follow the config's every)."""
        with self._saving:
            data = self.config_path.read_bytes() if self.config_path.exists() else b""
            text = data.decode("utf-8-sig").replace(CRLF, LF)
            try:
                new = editing.set_every(text, name, every)
            except ValueError as e:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
            checked = self.validate(new)
            if not checked["ok"]:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, checked["error"])
            saved = self._save_config(new, checked["targets"], _digest(data))
        return {"name": name, **saved}

    def add_target(self, text: str, item: object) -> dict:
        """The draft text with one more target from the form. Nothing is saved."""
        try:
            item = editing.clean_item(item)  # type: ignore[arg-type]
            target = editing.check_item(item)
            if target.name in editing.existing_names(text):
                raise ValueError(f"name「{target.name}」はもうあります。ほかの name にしてください")
            new = editing.add_target(text, item)
        except ValueError as e:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(e))
        return {"text": new, "name": target.name, **self.validate(new)}

    def save_config(self, text: str, base_hash: str) -> dict:
        """Save only a valid config, only over the version the page loaded, keeping the previous one as .bak."""
        checked = self.validate(text)
        if not checked["ok"]:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, checked["error"])
        with self._saving:  # two pages saving at once: the second sees the first one's hash and is refused
            return self._save_config(text, checked["targets"], base_hash)

    def _save_config(self, text: str, targets: list[str], base_hash: str) -> dict:
        current = self.config_path.read_bytes() if self.config_path.exists() else b""
        if _digest(current) != base_hash:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "設定ファイルが、画面で開いたあとにほかで書き換えられました。読み直してから直してください",
            )
        text = text.replace(CRLF, LF)
        if CRLF.encode() in current:  # keep the line endings and BOM the file had (Notepad writes both)
            text = text.replace(LF, CRLF)
        data = (codecs.BOM_UTF8 if current.startswith(codecs.BOM_UTF8) else b"") + text.encode("utf-8")
        if current:
            shutil.copy2(self.config_path, self.config_path.with_name(self.config_path.name + ".bak"))
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.config_path.name}.", suffix=".tmp", dir=self.config_path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            replace_file(tmp, self.config_path)
        finally:
            tmp.unlink(missing_ok=True)
        return {"hash": _digest(data), "targets": targets}
