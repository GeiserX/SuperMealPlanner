"""Which afternoons are busy, from a private iCal (ICS) address.

Google Calendar gives every calendar a secret iCal address (Settings, "Integrate calendar").
The address is a secret: it lives in an environment variable, never in the data repo.
An event that overlaps the busy-afternoon window makes that afternoon unavailable for batch cooking.
All-day events do not block an afternoon.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from .model import Household


def _hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def busy_afternoons_from_ics(
    ics: bytes | str, start: dt.date, end: dt.date, hh: Household
) -> dict[dt.date, list[str]]:
    """Map each busy date in [start, end) to the event titles that make it busy."""
    tz = ZoneInfo(hh.timezone)
    cal = icalendar.Calendar.from_ical(ics)
    w0, w1 = (_hm(x) for x in hh.calendar.busy_afternoon)
    out: dict[dt.date, list[str]] = {}
    for ev in recurring_ical_events.of(cal).between(start, end):
        s, e = ev.get("DTSTART").dt, ev.get("DTEND").dt if ev.get("DTEND") else None
        if not isinstance(s, dt.datetime):
            continue  # all-day
        s = s.astimezone(tz) if s.tzinfo else s.replace(tzinfo=tz)
        e = (e.astimezone(tz) if e.tzinfo else e.replace(tzinfo=tz)) if e else s
        day = s.date()
        win_s = dt.datetime.combine(day, w0, tz)
        win_e = dt.datetime.combine(day, w1, tz)
        if s < win_e and e > win_s:
            out.setdefault(day, []).append(str(ev.get("SUMMARY", "evento")))
    return out


def events_from_ics(
    ics: bytes | str, start: dt.date, end: dt.date, hh: Household
) -> list[tuple[str, str]]:
    """(when, title) for every event in [start, end), in the household's timezone, sorted."""
    tz = ZoneInfo(hh.timezone)
    cal = icalendar.Calendar.from_ical(ics)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        s = ev.get("DTSTART").dt
        if isinstance(s, dt.datetime):
            s = s.astimezone(tz) if s.tzinfo else s.replace(tzinfo=tz)
            when = s.strftime("%Y-%m-%d %a %H:%M")
        else:
            when = s.strftime("%Y-%m-%d %a (all day)")
        out.append((when, str(ev.get("SUMMARY", "event"))))
    return sorted(out)


def _fetch(hh: Household) -> bytes | None:
    env = hh.calendar.ics_env
    url = os.environ.get(env, "") if env else ""
    if not url:
        return None
    if url.startswith(("/", "file://")):  # an exported .ics file
        return Path(url.removeprefix("file://")).read_bytes()
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.content


def upcoming_events(hh: Household, start: dt.date, days: int = 7) -> list[tuple[str, str]]:
    """The calendar's next events, for the AI's context. Empty without a calendar."""
    ics = _fetch(hh)
    return events_from_ics(ics, start, start + dt.timedelta(days=days), hh) if ics else []


def busy_afternoons(hh: Household, start: dt.date, end: dt.date) -> dict[dt.date, list[str]]:
    ics = _fetch(hh)
    return busy_afternoons_from_ics(ics, start, end, hh) if ics else {}
