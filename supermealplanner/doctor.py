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
- prep_late: a dish whose recipe counts on a cooking session before its day (`ahead` steps with
  `session: true`) when no session comes in the AHEAD_KEEPS_DAYS before that day (a session on the
  day itself is after the meal), and no reminder has someone do it at home instead.
- prep: a dish that needs something done before its day (a dried pulse to soak the night before,
  an `ahead` step) and no reminder linked to that dish and day comes in time. Lunches cooked in a
  batch session are left out: the session's sheet and the batch reminder carry them.
- going_off: food in the pantry (pantry.py) that goes off before any planned dish uses it up,
  the plan cooking as planned up to the end of next week.
- unused: food ticked as bought that no dish uses (unused_food), as the AI already sees it; food
  the going_off check names is left out of it, so nothing is said twice.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .db import DB
from .model import WEEKDAYS, Data
from .planner import (
    MEAL_HOURS,
    MEALS,
    Day,
    Plan,
    Plans,
    _choose_sessions,
)
from .portions import needs

KINDS = (
    "off_day",
    "split",
    "too_old",
    "uncooked",
    "prep_late",
    "prep",
    "stale_reminder",
    "going_off",
    "unused",
)
AHEAD_KEEPS_DAYS = (
    3  # what a session cooks for a later dish (pulses, a roast pepper) keeps this long
)
REMINDER_LEAD_DAYS = 3  # a reminder further ahead than this of its step is not about that dish


@dataclass(frozen=True)
class Problem:
    kind: str  # one of KINDS
    key: str  # the same problem keeps its key, so a new one can be told from an old one
    text: str  # what is wrong and what to change, for the AI (English, like the rest of the facts)
    args: dict = field(default_factory=dict, compare=False, hash=False)  # for the chat's line
    reminder: int | None = None  # stale_reminder: the reminder to cancel


def _day(d: dt.date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.isoformat()}"


def problems(data: Data, db: DB, today: dt.date, offered: list[dict] | tuple = ()) -> list[Problem]:
    """Everything that no longer fits, in a fixed order. offered: reminders an answer offers and
    nobody has tapped yet ({"when": "YYYY-MM-DD HH:MM", "recipe", "date"}, as llm.reminders gives
    them), counted as set, so a proposal can be checked before it goes out."""
    plans = Plans(data)
    monday = today - dt.timedelta(days=today.weekday())
    end = monday + dt.timedelta(days=13)  # the end of next week
    dates = [today + dt.timedelta(days=i) for i in range((end - today).days + 1)]
    out = _off_days(data, plans, today, end)
    out += _lunches(data, plans, today, dates)
    out += _prep(data, db, plans, dates, offered)
    out += [p for r in db.reminders() if (p := _stale(data, plans, r))]
    off = _going_off(data, db, plans, today, end)
    out += off
    named = {p.args["food"] for p in off}
    left = [x for x in unused_food(data, db, monday) if x not in named]
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


def _off_days(data: Data, plans: Plans, today: dt.date, end: dt.date) -> list[Problem]:
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


def _lunches(data: Data, plans: Plans, today: dt.date, dates: list[dt.date]) -> list[Problem]:
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


def _linked(data: Data, db: DB, offered) -> dict[tuple[str, str], list[float]]:
    """(recipe, the day it is eaten) -> when each reminder about that dish goes, set or already
    sent, in any chat, plus the ones offered."""
    zone = ZoneInfo(data.household.timezone)
    out: dict[tuple[str, str], list[float]] = {}
    for state in ("pending", "sent"):
        for r in db.reminders(state=state):
            if r.get("recipe") and r.get("date"):
                out.setdefault((r["recipe"], r["date"]), []).append(r["at"])
    for r in offered:
        if not r.get("recipe") or not r.get("date") or "when" not in r:
            continue
        try:
            when = dt.datetime.strptime(r["when"], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
        except ValueError:
            continue
        out.setdefault((r["recipe"], r["date"]), []).append(when.timestamp())
    return out


def _sessions_before(plans: Plans, d: dt.date) -> list[dt.date]:
    """The batch sessions in the AHEAD_KEEPS_DAYS before d, soonest first."""
    out = []
    for i in range(AHEAD_KEEPS_DAYS, 0, -1):
        x = d - dt.timedelta(days=i)
        plan = plans.plan(x)
        if plan and x not in plans.paused and any(s.date == x for s in plan.sessions):
            out.append(x)
    return out


def _prep(data: Data, db: DB, plans: Plans, dates: list[dt.date], offered) -> list[Problem]:
    zone = ZoneInfo(data.household.timezone)
    linked = _linked(data, db, offered)

    def at(d: dt.date, hm: str = "00:00") -> float:
        h, m = (int(x) for x in hm.split(":"))
        return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=zone).timestamp()

    out = []
    for d in dates:
        day = plans.day(d)
        if day is None:
            continue
        for meal in MEALS:
            r = data.recipes.get(getattr(day, meal) or "")
            if r is None or (meal == "lunch" and _cooked(plans.plan(d), day)):
                continue
            soak = [
                data.ingredients[i.id].name
                for i in r.ingredients
                if i.id in data.ingredients and data.ingredients[i.id].soak
            ]
            if not soak and not r.ahead:
                continue
            session = [a.step for a in r.ahead if a.session]
            before = _sessions_before(plans, d) if session else []
            # a dried pulse soaks the night before it is cooked: in the session, else that day
            cooked = before[-1] if before else d
            deadline = at(cooked) if soak else at(d, MEAL_HOURS[meal][0])
            times = linked.get((r.id, d.isoformat()), [])
            if any(
                at(cooked - dt.timedelta(days=REMINDER_LEAD_DAYS)) <= t <= deadline for t in times
            ):
                continue
            what = r.name + f" [{r.id}], {meal} on {_day(d)}"
            link = f"`dish {r.id} {d.isoformat()}`"
            if session and not before:
                plan = plans.plan(d)
                same = plan is not None and any(s.date == d for s in plan.sessions)
                when = (
                    "the session that day comes after the meal"
                    if same
                    else f"no session comes in the {AHEAD_KEEPS_DAYS} days before"
                )
                out.append(
                    Problem(
                        "prep_late",
                        f"prep_late:{r.id}:{d.isoformat()}",
                        f"{what}, counts on a cooking session before that day for: "
                        f"{'; '.join(session)}. But {when}, and no reminder has someone do it at "
                        f"home before. Fix: `set` the dish on a day a session comes before, or a "
                        f"reminders line to do it before the meal (on {d.isoformat()} or the day "
                        f"before), with {link}.",
                        {"dish": r.name, "date": d},
                    )
                )
                continue
            steps = (
                [f"soak {', '.join(soak)} the night before {cooked.isoformat()}"] if soak else []
            )
            steps += [
                a.step + (f" (in the session of {before[-1].isoformat()})" if a.session else "")
                for a in r.ahead
            ]
            fix = (
                f"It is too late for that day: `swap` it with a later day's {meal} or `set` another "
                "dish, and remind them for the new day"
                if deadline <= at(dates[0])
                else f"a reminders line for each step at its time, with {link}, or `set` another "
                "dish that day"
            )
            out.append(
                Problem(
                    "prep",
                    f"prep:{r.id}:{d.isoformat()}",
                    f"{what}, needs doing ahead: {'; '.join(steps)}. No reminder about it comes in "
                    f"time. Fix: {fix}.",
                    {"dish": r.name, "date": d},
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


def serves(data: Data, recipe: str, date: dt.date, plans: Plans | None = None) -> bool:
    """The menu as the household sees it has that recipe in some meal of that day."""
    day = (plans or Plans(data)).day(date)
    return day is not None and recipe in {getattr(day, m) for m in MEALS}


def stale(data: Data, row: dict) -> bool:
    """A reminder linked to a dish that the day's menu no longer has."""
    return _stale(data, Plans(data), row) is not None


def _stale(data: Data, plans: Plans, row: dict) -> Problem | None:
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


def _going_off(data: Data, db: DB, plans: Plans, today: dt.date, end: dt.date) -> list[Problem]:
    from .pantry import Walk, going_off
    from .portions import fmt_qty

    out = []
    for lot, d in going_off(data, db, today, end, Walk(data, db, plans)):
        ing = data.ingredients[lot.ingredient]
        last = d - dt.timedelta(days=1)
        out.append(
            Problem(
                "going_off",
                f"going_off:{lot.ingredient}:{d.isoformat()}",
                f"{ing.name} [{ing.id}] in the pantry ({fmt_qty(ing, lot.qty, data.locale)} left "
                f"by then) is good until {_day(last)} and no planned dish uses it up before. Fix: "
                f"`set` a dish with it on or before {last.isoformat()}, or say to freeze it.",
                {"food": ing.name, "date": last},
            )
        )
    return out


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
