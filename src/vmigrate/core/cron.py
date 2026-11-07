"""Purpose: a small, dependency-free 5-field cron parser for replica sync schedules (UTC).

Usage:
    c = Cron.parse("*/15 * * * *")
    c.next_after(time.time())          # -> epoch seconds of the next match (UTC)
    c.upcoming(time.time(), 5)         # next 5 run times

Fields: minute hour day-of-month month day-of-week (0 or 7 = Sunday).
Each field: *, N, A-B, A-B/S, */S, and comma lists of those. Names (MON, JAN) and @aliases
@hourly @daily @weekly @monthly are supported. Like Vixie cron, when both day-of-month and
day-of-week are restricted, a day matches if EITHER matches.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}
DAYS = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}


class CronError(ValueError):
    pass


def _field(spec: str, lo: int, hi: int, names: dict[str, int] | None = None) -> frozenset[int]:
    out: set[int] = set()
    for part in spec.split(","):
        if not part:
            raise CronError(f"empty item in {spec!r}")
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) == 0:
                raise CronError(f"bad step in {spec!r}")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        else:
            def num(x: str) -> int:
                x = x.upper()
                if names and x in names:
                    return names[x]
                if not x.isdigit():
                    raise CronError(f"bad value {x!r}")
                return int(x)
            if "-" in part:
                x, y = part.split("-", 1)
                a, b = num(x), num(y)
            else:
                a = b = num(part)
                if step != 1:
                    b = hi
        if hi == 7 and b == 7 and a <= 7:   # day-of-week: 7 means Sunday
            out.add(0)
            b = 6 if a <= 6 else 7
        if not (lo <= a <= hi and lo <= b <= hi) or a > b:
            raise CronError(f"value out of range {lo}-{hi} in {spec!r}")
        out.update(v % 7 if hi == 7 else v for v in range(a, b + 1, step))
    return frozenset(out)


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    dom: frozenset[int]
    months: frozenset[int]
    dow: frozenset[int]
    dom_star: bool
    dow_star: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        e = ALIASES.get(expr.strip().lower(), expr.strip())
        parts = e.split()
        if len(parts) != 5:
            raise CronError(f"cron needs 5 fields (minute hour day month weekday), got {len(parts)}: {expr!r}")
        m, h, d, mo, w = parts
        return cls(expr.strip(), _field(m, 0, 59), _field(h, 0, 23), _field(d, 1, 31), _field(mo, 1, 12, MONTHS),
                   _field(w, 0, 7, DAYS), d == "*", w == "*")

    def _day_ok(self, dt: datetime) -> bool:
        dom_ok = dt.day in self.dom
        dow_ok = (dt.weekday() + 1) % 7 in self.dow     # python Mon=0 -> cron Sun=0
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def next_after(self, ts: float) -> float:
        dt = datetime.fromtimestamp(ts, timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 5):  # bounded: a valid expression matches within a few years
            if dt.month not in self.months:
                dt = (dt.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(dt):
                dt = dt.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if dt.hour not in self.hours:
                dt = dt.replace(minute=0) + timedelta(hours=1)
                continue
            for mi in sorted(self.minutes):
                if mi >= dt.minute:
                    return dt.replace(minute=mi).timestamp()
            dt = dt.replace(minute=0) + timedelta(hours=1)
        raise CronError(f"{self.expr!r} never matches")

    def upcoming(self, ts: float, n: int = 5) -> list[float]:
        out = []
        for _ in range(n):
            ts = self.next_after(ts)
            out.append(ts)
        return out
