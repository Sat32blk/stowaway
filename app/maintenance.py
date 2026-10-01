"""Scheduled maintenance: restart containers on a schedule, optionally updating
them to the newest image at the same time.

This is separate from sleeping/waking: it works on any container on the
server. Jobs run one at a time. A container that's busy at its scheduled time
is given up to `busy_wait` hours to calm down first.

Schedule format (stored per container name):
    {"freq": "daily"|"weekly"|"monthly", "time": "04:00",
     "weekday": 0-6 (Mon=0, weekly), "mday": 1-28 or "last" (monthly),
     "update": bool, "busy_wait": hours}
"""
import calendar
import re
from datetime import datetime, timedelta

FREQS = ("daily", "weekly", "monthly")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def clean_schedule(s: dict) -> dict:
    """Validate a schedule from the dashboard; raises ValueError with a readable message."""
    freq = s.get("freq")
    if freq not in FREQS:
        raise ValueError("choose daily, weekly or monthly")
    t = str(s.get("time") or "")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
        raise ValueError("time must look like 04:00")
    out = {"freq": freq, "time": t, "update": bool(s.get("update")),
           "busy_wait": float(s.get("busy_wait", 6) or 0)}
    if not 0 <= out["busy_wait"] <= 24:
        raise ValueError("busy wait must be between 0 and 24 hours")
    if freq == "weekly":
        wd = int(s.get("weekday", 6))
        if not 0 <= wd <= 6:
            raise ValueError("pick a day of the week")
        out["weekday"] = wd
    if freq == "monthly":
        md = s.get("mday", 1)
        if md != "last":
            md = int(md)
            if not 1 <= md <= 28:
                raise ValueError("pick a day from 1 to 28, or the last day of the month")
        out["mday"] = md
    return out


def _matches(sched: dict, day) -> bool:
    if sched["freq"] == "daily":
        return True
    if sched["freq"] == "weekly":
        return day.weekday() == sched["weekday"]
    md = sched["mday"]
    if md == "last":
        return day.day == calendar.monthrange(day.year, day.month)[1]
    return day.day == md


def next_run(sched: dict, after_ts: float, tz) -> float:
    """First scheduled moment strictly after `after_ts`."""
    h, m = map(int, sched["time"].split(":"))
    start = datetime.fromtimestamp(after_ts, tz)
    for i in range(0, 64):
        day = (start + timedelta(days=i)).date()
        if not _matches(sched, day):
            continue
        at = datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp()
        if at > after_ts:
            return at
    raise ValueError("no upcoming run found")


def describe(sched: dict | None) -> str:
    if not sched:
        return "Off"
    t = datetime.strptime(sched["time"], "%H:%M").strftime("%I:%M %p").lstrip("0")
    if sched["freq"] == "daily":
        s = f"Daily at {t}"
    elif sched["freq"] == "weekly":
        s = f"{WEEKDAYS[sched['weekday']]}s at {t}"
    else:
        md = sched["mday"]
        day = "last day" if md == "last" else f"{md}{'th' if 11 <= md <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(md % 10, 'th')}"
        s = f"Monthly on the {day} at {t}"
    return s + (" · with updates" if sched.get("update") else "")
