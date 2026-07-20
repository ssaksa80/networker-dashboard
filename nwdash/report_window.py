"""Pure cadence math for report groups: which window a report covers and when
it next fires. All functions take an explicit `now` (a tz-aware datetime) so
they are deterministic and unit-testable."""
from __future__ import annotations

from datetime import datetime, timedelta

SECTION_KEYS = ["backup_sla", "management", "recovery", "clone", "alerts", "server_protection", "health"]
CADENCES = ("daily", "weekly", "monthly")


def _prev_sunday(d: datetime) -> datetime:
    return (d - timedelta(days=(d.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)


def compute_window(cadence: str, now: datetime) -> tuple[str, str, str]:
    if cadence == "daily":
        return "24h", "", ""
    if cadence == "weekly":
        this_sun = _prev_sunday(now)
        start = this_sun - timedelta(days=7)
        end = this_sun - timedelta(days=1)
        return "custom", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    if cadence == "monthly":
        first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_prev = first_this - timedelta(days=1)
        start = last_prev.replace(day=1)
        return "custom", start.strftime("%Y-%m-%d"), last_prev.strftime("%Y-%m-%d")
    raise ValueError(f"unknown cadence {cadence!r}")


def _at_time(d: datetime, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":", 1))
    return d.replace(hour=hh, minute=mm, second=0, microsecond=0)


def next_run(cadence: str, send_time: str, now: datetime) -> float:
    if cadence == "daily":
        target = _at_time(now, send_time)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()
    if cadence == "weekly":
        days_to_sun = (6 - now.weekday()) % 7
        target = _at_time(now + timedelta(days=days_to_sun), send_time)
        if target <= now:
            target += timedelta(days=7)
        return target.timestamp()
    if cadence == "monthly":
        target = _at_time(now.replace(day=1), send_time)
        if target <= now:
            year = now.year + (1 if now.month == 12 else 0)
            month = 1 if now.month == 12 else now.month + 1
            target = _at_time(now.replace(year=year, month=month, day=1), send_time)
        return target.timestamp()
    raise ValueError(f"unknown cadence {cadence!r}")
