"""Words for time in a config: intervals (30m, 2h, 1d), days (平日, 土日, 月曜) and the deadlines a
source is expected to be updated by (毎週金 17:00, 平日 9:00, 毎月25日 17:00, 3d)."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

_EVERY = re.compile(r"^\s*(\d+)\s*(m|min|h|d)\s*$")
DEFAULT_EVERY = "30m"  # a shared folder or a web page is not fetched more often than this unless asked
MIN_EVERY = dt.timedelta(minutes=5)
DAYS = "月火水木金土日"
_TIME = r"(\d{1,2}):(\d{2})"
_WEEKLY = re.compile(rf"^毎週\s*([{DAYS}])(?:曜日|曜)?\s*{_TIME}$")
_DAILY = re.compile(rf"^(毎日|平日)\s*{_TIME}$")
_MONTHLY = re.compile(rf"^毎月\s*(\d{{1,2}})\s*日\s*{_TIME}$")
_INTERVAL = re.compile(r"^\s*\d+\s*(m|min|h|d)\s*$")
DEADLINE_HELP = (
    "「毎週金 17:00」「毎日 9:00」「平日 9:00」「毎月25日 17:00」、または「3d」「12h」のように書いてください"
)


def every_delta(v: str) -> dt.timedelta:
    n, unit = _EVERY.match(v).groups()  # type: ignore[union-attr]
    return dt.timedelta(minutes=int(n)) * {"m": 1, "min": 1, "h": 60, "d": 1440}[unit]


def check_every(v: str | None) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not _EVERY.match(v):
        raise ValueError("30m、2h、1d のように書いてください")
    if every_delta(v) < MIN_EVERY:
        raise ValueError("短すぎます。5m 以上にしてください（取りに行く先の負担になるため）")
    return v.strip()


def day_set(word: str) -> set[int]:
    """平日 / 毎日 / 土日 / 月 / 月曜 / 月曜日 as weekday numbers (Monday is 0). Empty when unknown."""
    d = str(word).strip()
    if d in ("平日", "毎日", "土日"):
        return {"平日": {0, 1, 2, 3, 4}, "毎日": set(range(7)), "土日": {5, 6}}[d]
    d = d.removesuffix("曜日").removesuffix("曜")
    return {DAYS.index(d)} if len(d) == 1 and d in DAYS else set()


def every_label(v: str) -> str:
    n, unit = _EVERY.match(v).groups()  # type: ignore[union-attr]
    return f"{int(n)} {'分' if unit in ('m', 'min') else '時間' if unit == 'h' else '日'}ごと"


@dataclass(frozen=True)
class Deadline:
    """When a source is expected to have been updated: by each calendar time, or within an interval."""

    text: str
    kind: str  # weekly / daily / weekdays / monthly / interval
    day: int = 0  # weekday (Mon=0) or day of month
    minutes: int = 0  # time of day
    interval: dt.timedelta | None = None

    def _is_on(self, d: dt.date) -> bool:
        if self.kind == "weekly":
            return d.weekday() == self.day
        if self.kind == "weekdays":
            return d.weekday() < 5
        if self.kind == "monthly":
            return d.day == self.day
        return True

    def last_before(self, now: dt.datetime) -> dt.datetime | None:
        """The latest deadline at or before now (calendar kinds only)."""
        for i in range(62):
            day = (now - dt.timedelta(days=i)).date()
            at = dt.datetime.combine(day, dt.time()) + dt.timedelta(minutes=self.minutes)
            if self._is_on(day) and at <= now:
                return at
        return None

    @property
    def label(self) -> str:
        return every_label(self.text) if self.kind == "interval" else self.text


def parse_deadline(text: str) -> Deadline:
    t = re.sub(r"\s+", " ", str(text).strip())
    if _INTERVAL.match(t):
        return Deadline(t, "interval", interval=every_delta(t))
    m = _WEEKLY.match(t)
    if m:
        return Deadline(t, "weekly", day=DAYS.index(m.group(1)), minutes=_minutes(m.group(2), m.group(3)))
    m = _DAILY.match(t)
    if m:
        return Deadline(t, "daily" if m.group(1) == "毎日" else "weekdays", minutes=_minutes(m.group(2), m.group(3)))
    m = _MONTHLY.match(t)
    if m and 1 <= int(m.group(1)) <= 28:
        return Deadline(t, "monthly", day=int(m.group(1)), minutes=_minutes(m.group(2), m.group(3)))
    if m:
        raise ValueError("毎月の日は 1〜28 日で書いてください（29 日以降はない月がある）")
    raise ValueError(f"expect_update「{text}」が読めません。{DEADLINE_HELP}")


def _minutes(h: str, m: str) -> int:
    hh, mm = int(h), int(m)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("時刻は 0:00〜23:59 で書いてください")
    return hh * 60 + mm
