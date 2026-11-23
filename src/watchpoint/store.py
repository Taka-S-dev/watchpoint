"""Per-target state and snapshot generations kept under the data folder."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def retrying(fn, tries: int = 20, pause: float = 0.05):
    """Run fn, retrying briefly on PermissionError.

    On Windows, replacing or reading a file fails while another handle has it open
    (the GUI reading state.json while a scheduled check writes it, antivirus, indexers).
    """
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(pause)
    return None


def replace_file(src: Path, dst: Path) -> None:
    retrying(lambda: src.replace(dst))


def safe_name(name: str) -> str:
    s = _UNSAFE.sub("_", name).strip(" .")
    return s or "_"


@dataclass
class State:
    last_checked: str | None = None
    last_status: str | None = None
    last_changed: str | None = None
    last_error: str | None = None
    failures: int = 0  # consecutive failed checks
    sha256: str | None = None
    probe: str | None = None  # cheap fingerprint of the source (size+mtime, page version, ...)
    snapshot: str | None = None  # the latest copy
    baseline: str | None = None  # the copy changes are reported against, until acknowledged
    notified: str | None = None  # the snapshot a notification was last sent for
    # What was found is kept apart from what was told: each is marked sent only once its notice is written,
    # so a check that does not notify (the page) or a drop folder that cannot be written loses nothing.
    failure_notified: bool = False  # a notice for the current run of failures was written
    alerts_notified: list[str] = field(default_factory=list)  # rule warnings a notice was written for
    notice: str | None = None  # a one-off warning still to be sent (changes set aside by a settings change)
    # Unacknowledged changes taken before the settings changed ({"from": id, "to": id}): texts made with
    # other settings cannot be compared with the new baseline, so they are kept, and shown, until acknowledged.
    set_aside: list[dict] = field(default_factory=list)
    source: str | None = None
    settings: str | None = None  # fingerprint of the settings that shape the text
    meta: dict = field(default_factory=dict)  # per-source details of the latest copy (title, version, ...)
    notes: dict[str, str] = field(default_factory=dict)  # snapshot id -> note given on ack
    via: dict[str, str] = field(default_factory=dict)  # snapshot id -> what started the check that took it
    alerts: list[str] = field(default_factory=list)  # the rule warnings found by the last check
    history: list[str] = field(default_factory=list)


class TargetStore:
    def __init__(self, data_dir: Path, key: str):
        self.data_dir = data_dir
        self.dir = data_dir / safe_name(key)
        self.snapshots = self.dir / "snapshots"
        self.state_path = self.dir / "state.json"

    def load(self) -> State:
        if not self.state_path.exists():
            return State()
        try:
            raw = json.loads(retrying(lambda: self.state_path.read_text(encoding="utf-8")))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
            known = {k: raw[k] for k in State.__dataclass_fields__ if k in raw}
            state = State(**known)
        except (ValueError, TypeError):
            # A damaged state must not stop this target forever: keep it aside and start over.
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            self.state_path.replace(self.state_path.with_name(f"state.broken-{stamp}.json"))
            return State(last_error="state.json が壊れていたので退避して、最初からやり直しました")
        if state.baseline is None:
            state.baseline = state.snapshot
        return state

    def save(self, state: State) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
        replace_file(tmp, self.state_path)

    def snapshot_dir(self, snap_id: str) -> Path:
        return self.snapshots / snap_id

    def incoming_dir(self) -> Path:
        d = self.dir / "_incoming"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        return d

    def commit_incoming(self, incoming: Path, snap_id: str) -> Path:
        dest = self.snapshot_dir(snap_id)
        self.snapshots.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        incoming.replace(dest)
        return dest

    def discard_incoming(self) -> None:
        d = self.dir / "_incoming"
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    def save_changes(
        self, snap_id: str, base_id: str, diff: str, changes: str | None, entries: list[dict] | None = None
    ) -> None:
        """Keep, with a copy, how it differs from the copy it was compared with when it was checked."""
        d = self.snapshot_dir(snap_id)
        (d / "diff.txt").write_text(diff, encoding="utf-8")
        if changes is None:
            (d / "changes.txt").unlink(missing_ok=True)
        else:
            (d / "changes.txt").write_text(changes, encoding="utf-8")
        (d / "compared.json").write_text(json.dumps({"from": base_id, "to": snap_id}), encoding="utf-8")
        if entries:
            (d / "changes.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        else:
            (d / "changes.json").unlink(missing_ok=True)

    def saved_entries(self, snap_id: str) -> list[dict]:
        """The per-record changes kept with a copy, as data; [] for copies saved before they were kept."""
        try:
            data = json.loads((self.snapshot_dir(snap_id) / "changes.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def compared_with(self, snap_id: str) -> str | None:
        """The copy the saved changes of snap_id are against, when that was recorded."""
        try:
            data = json.loads((self.snapshot_dir(snap_id) / "compared.json").read_text(encoding="utf-8"))
            return data["from"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def read_text(self, snap_id: str | None) -> str | None:
        if not snap_id:
            return None
        f = self.snapshot_dir(snap_id) / "text.txt"
        return f.read_text(encoding="utf-8") if f.exists() else None

    def prune(self, state: State, keep: int) -> None:
        """Drop the oldest generations, but never the baseline, the latest copy or changes set aside."""
        protected = {state.baseline, state.snapshot} | {s for pair in state.set_aside for s in pair.values()}
        removable = [s for s in state.history if s not in protected]
        while len(state.history) > keep and removable:
            old = removable.pop(0)
            state.history.remove(old)
            state.notes.pop(old, None)
            state.via.pop(old, None)
            shutil.rmtree(self.snapshot_dir(old), ignore_errors=True)

    def generation(self, state: State, number: int) -> str:
        """History number as shown by `history`: 1 is the latest copy."""
        if not 1 <= number <= len(state.history):
            raise IndexError(f"履歴の {number} 番はありません（1〜{len(state.history)}）")
        return state.history[-number]


LOG_MAX = 5_000_000
_log_lock = threading.Lock()


def append_log(data_dir: Path, entry: dict) -> None:
    """One JSON line per check of a target, kept next to the data (who saw what, when).

    Targets are checked in parallel threads, so writes are serialised: on
    Windows, two handles appending at once can overwrite each other.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"time": dt.datetime.now().isoformat(timespec="seconds"), **entry}, ensure_ascii=False) + "\n"
    path = data_dir / "log.jsonl"
    with _log_lock:
        try:
            if path.stat().st_size > LOG_MAX:  # unattended runs add lines every few minutes
                path.replace(path.with_name("log.1.jsonl"))
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
