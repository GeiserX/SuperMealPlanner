"""The consistency check: where the records that describe the same thing disagree.

The plan, the batch days, the cook dates and the chat's reminders are written at different times by
different hands (the planner, the chat, a person editing the data), so they can drift apart. This
module only reads them and lists what no longer fits, with what to change. Plain code, no AI: the
same data on the same day gives the same list.

What it looks at, from today to the end of next week (the menu the AI sees):
- off_day: a batch session still to come on a day in `batch.avoid_days`, or on a day that is not a
  batch day while the batch day the planner would pick for that half of the week has no session.
  A one-off session beside the usual ones is left alone, and so is one the planner put on another
  day because the batch day was busy. Sessions cooked the morning a lunch is eaten, and the ones a
  pause moved to the first day back, are not batch sessions and are not looked at.
- split: one recipe eaten for lunch on two days in a row, cooked on two different days, when one
  cook still to come would keep for both: the Cook page shows one dish twice.
- too_old: a lunch eaten today or later cooked longer before its day than the recipe keeps in the
  fridge, when it does not freeze.
- uncooked: a lunch eaten today or later whose cook date is already past and no session of that
  date cooked it for that day. The Cook page never showed it on that date, so nobody cooked it.
- stale_reminder: a chat reminder linked to a dish (its recipe and the day it is eaten) when that
  day's menu no longer has the dish. The app cancels these itself.
- unused: food ticked as bought that no dish uses (unused_food), as the AI already sees it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .db import DB
from .model import WEEKDAYS, Data
from .planner import (
    MEALS,
    Day,
    Plan,
    _choose_sessions,
    load_plan,
    paused_days,
    plans_dir,
    without,
)
from .portions import needs

KINDS = ("off_day", "split", "too_old", "uncooked", "stale_reminder", "unused")


@dataclass(frozen=True)
class Problem:
    kind: str  # one of KINDS
    key: str  # the same problem keeps its key, so a new one can be told from an old one
    text: str  # what is wrong and what to change, for the AI (English, like the rest of the facts)
    args: dict = field(default_factory=dict, compare=False, hash=False)  # for the chat's line
    reminder: int | None = None  # stale_reminder: the reminder to cancel


def _day(d: dt.date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.isoformat()}"


class _Plans:
    """The plans on disk, read once, and each day as the household sees it (paused days hidden)."""

    def __init__(self, data: Data):
        self.data = data
        self.paused = paused_days(data)
        self.raw = [load_plan(p) for p in sorted(plans_dir(data).glob("*.yaml"))]
        self._seen: dict[int, Plan] = {}

    def plan(self, day: dt.date, raw: bool = False) -> Plan | None:
        """The newest plan that covers `day` (planner.plan_for, without reading the files again)."""
        best = None
        for i, p in enumerate(self.raw):
            if p.start <= day < p.end:
                best = i
        if best is None:
            return None
        if raw:
            return self.raw[best]
        if best not in self._seen:
            self._seen[best] = without(self.raw[best], self.paused)
        return self._seen[best]

    def day(self, day: dt.date) -> Day | None:
        plan = self.plan(day)
        return plan.day(day) if plan else None


def problems(data: Data, db: DB, today: dt.date) -> list[Problem]:
    """Everything that no longer fits, in a fixed order."""
    plans = _Plans(data)
    monday = today - dt.timedelta(days=today.weekday())
    end = monday + dt.timedelta(days=13)  # the end of next week
    dates = [today + dt.timedelta(days=i) for i in range((end - today).days + 1)]
    out = _off_days(data, plans, today, end)
    out += _lunches(data, plans, today, dates)
    out += [p for r in db.reminders() if (p := _stale(data, plans, r))]
    left = unused_food(data, db, monday)
    if left:
        out.append(
            Problem(
                "unused",
                "unused:" + ",".join(left),
                "Bought and on no dish: " + ", ".join(left),
                {"food": ", ".join(left)},
            )
        )
    return out


def _off_days(data: Data, plans: _Plans, today: dt.date, end: dt.date) -> list[Problem]:
    batch = data.household.batch
    out = []
    seen: set[tuple[dt.date, bool]] = set()
    for plan in {id(p): p for d in _span(today, end) if (p := plans.plan(d, raw=True))}.values():
        afternoons = {s.date for s in plan.sessions if not s.morning}
        for s in plan.sessions:
            if s.morning or not today < s.date <= end or s.date in plans.paused:
                continue
            if (s.date, s.morning) in seen or plans.plan(s.date, raw=True) is not plan:
                continue
            seen.add((s.date, s.morning))
            name = WEEKDAYS[s.date.weekday()]
            if name in batch.preferred_days and name not in batch.avoid_days:
                continue
            week = s.date - dt.timedelta(days=s.date.weekday())
            busy = {d.date: d.busy for d in plan.days if d.busy}
            first_half = s.date.weekday() <= 2
            want = next(
                (
                    x.date
                    for x in _choose_sessions(data, week, busy)
                    if (x.date.weekday() <= 2) == first_half
                ),
                None,
            )
            if name not in batch.avoid_days and (want is None or want in afternoons):
                continue  # a one-off beside the usual session, or the batch day was busy
            dishes = [x.recipe for x in s.dishes]
            fix = (
                f"Move it: `set` each of its lunches again with `cook:` a batch day before the day "
                f"it is eaten that keeps it (the planner would cook on {_day(want)})"
                if want
                else "Move it: `set` each of its lunches again with `cook:` a batch day"
            )
            if week > today:
                fix += f", or `regenerate` from {week.isoformat()} for a new draw on the batch days"
            out.append(
                Problem(
                    "off_day",
                    f"off_day:{s.date.isoformat()}",
                    f"The batch session on {_day(s.date)} ({', '.join(dishes)}) is on a day that "
                    f"is not a batch day (batch days {batch.preferred_days}, never "
                    f"{batch.avoid_days}). {fix}.",
                    {"date": s.date, "want": want},
                )
            )
    return out


def _span(first: dt.date, last: dt.date) -> list[dt.date]:
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def _keeps(data: Data, rid: str, cook: dt.date, eat: dt.date) -> bool:
    r = data.recipes[rid]
    return (eat - cook).days <= r.fridge_days or r.freezes


def _lunches(data: Data, plans: _Plans, today: dt.date, dates: list[dt.date]) -> list[Problem]:
    out = []
    for d in dates:
        day = plans.day(d)
        if day is None or day.lunch not in data.recipes:
            continue
        r, cook = data.recipes[day.lunch], day.lunch_cook
        nxt = plans.day(d + dt.timedelta(days=1)) if d + dt.timedelta(days=1) in dates else None
        if nxt and nxt.lunch == day.lunch and nxt.lunch_cook != cook:
            one = [
                c
                for c in sorted({cook, nxt.lunch_cook})
                if today <= c <= d and _keeps(data, r.id, c, nxt.date)
            ]
            if one:
                c = one[0]
                other = nxt if nxt.lunch_cook != c else day
                out.append(
                    Problem(
                        "split",
                        f"split:{r.id}:{d.isoformat()}",
                        f"{r.name} [{r.id}] is lunch on {_day(d)} and {_day(nxt.date)} but cooked "
                        f"twice, on {cook.isoformat()} and {nxt.lunch_cook.isoformat()}; one cook "
                        f"on {c.isoformat()} keeps for both. Fix: `set` {other.date.isoformat()} "
                        f"lunch {r.id} with `cook: {c.isoformat()}`.",
                        {"dish": r.name, "a": d, "b": nxt.date, "cook": c},
                    )
                )
        gap = (d - cook).days
        if gap > r.fridge_days and not r.freezes:
            first = max(today, d - dt.timedelta(days=r.fridge_days))
            out.append(
                Problem(
                    "too_old",
                    f"too_old:{d.isoformat()}",
                    f"Lunch on {_day(d)}, {r.name} [{r.id}], is cooked on {cook.isoformat()}, "
                    f"{gap} days before, and keeps {r.fridge_days} days in the fridge and does not "
                    f"freeze. Fix: `set` it again with `cook:` {first.isoformat()} to "
                    f"{d.isoformat()}, or another dish.",
                    {"dish": r.name, "date": d, "cook": cook},
                )
            )
        elif cook < today and not _cooked(plans.plan(d), day):
            out.append(
                Problem(
                    "uncooked",
                    f"uncooked:{d.isoformat()}",
                    f"Lunch on {_day(d)}, {r.name} [{r.id}], says it was cooked on "
                    f"{cook.isoformat()}, but no batch session that day cooked it, so nobody did. "
                    f"Fix: `set` it again with `cook:` a day from {today.isoformat()} on, or "
                    "without cook (cooked that morning).",
                    {"dish": r.name, "date": d, "cook": cook},
                )
            )
    return out


def _cooked(plan: Plan | None, day: Day) -> bool:
    """A session on the lunch's cook date cooked that dish for that day."""
    return plan is not None and any(
        s.date == day.lunch_cook and x.recipe == day.lunch and day.date in x.eat
        for s in plan.sessions
        for x in s.dishes
    )


def serves(data: Data, recipe: str, date: dt.date, plans: _Plans | None = None) -> bool:
    """The menu as the household sees it has that recipe in some meal of that day."""
    day = (plans or _Plans(data)).day(date)
    return day is not None and recipe in {getattr(day, m) for m in MEALS}


def stale(data: Data, row: dict) -> bool:
    """A reminder linked to a dish that the day's menu no longer has."""
    return _stale(data, _Plans(data), row) is not None


def _stale(data: Data, plans: _Plans, row: dict) -> Problem | None:
    if row.get("state", "pending") != "pending" or not row.get("recipe") or not row.get("date"):
        return None
    try:
        date = dt.date.fromisoformat(row["date"])
    except ValueError:
        return None
    if serves(data, row["recipe"], date, plans):
        return None
    name = data.recipes[row["recipe"]].name if row["recipe"] in data.recipes else row["recipe"]
    return Problem(
        "stale_reminder",
        f"reminder:{row['id']}",
        f"Reminder {row['id']} ({row['text']}) is about {name} on {_day(date)}, which that day's "
        f"menu no longer has. The app cancels it; give the right one if a step is still needed.",
        {"text": row["text"]},
        reminder=row["id"],
    )


def unused_food(data: Data, db: DB, monday: dt.date) -> list[str]:
    """Food ticked as bought on last week's or this week's list that no dish uses, from last Monday
    to the end of next week: a dish was moved or dropped after the shopping. Names, sorted."""
    from .planner import plan_for

    weeks = [monday + dt.timedelta(days=7 * w) for w in (-1, 0, 1)]
    used: set[str] = set()
    for m in weeks:
        if plan := plan_for(data, m):
            used |= set(needs(data, db, plan, m))
    bought = db.ticked(weeks[0]) | db.ticked(weeks[1])
    return sorted(data.ingredients[i].name for i in bought - used if i in data.ingredients)
