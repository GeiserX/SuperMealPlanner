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
  date cooked it for that day (the Cook page never showed it on that date, so nobody cooked it), or
  someone marked that session not done.
- stale_reminder: a chat reminder linked to a dish (its recipe and the day it is eaten) when that
  day's menu no longer has the dish. The app cancels these itself.
- prep_late: a dish whose recipe counts on a cooking session before its day (`ahead` steps with
  `session: true`) when no session comes in the AHEAD_KEEPS_DAYS before that day (a session on the
  day itself is after the meal), and no reminder has someone do it at home instead.
- prep: a dish with an `ahead` step and no reminder linked to that dish and day comes in time.
  Lunches cooked in a batch session are left out: the session's sheet and the batch reminder carry
  them.

The chains, from the task list (tasks.py), each step a task with its time, the chat's reminders
by their step kind (a reminder that says no step counts as none):
- no_session: a lunch counting on a session before its day (its cook date still to come) when no
  session cooks it for that day.
- no_cook: a dish with a dried pulse (an ingredient marked `soak`) that nothing cooks before the
  meal: a session that cooks it for that day, a reminder of kind cook, or the meal itself when the
  recipe is cooked (not only assembled) and has no `ahead` steps.
- no_soak: that pulse with no soak in the SOAK_MAX_H before its cook (the app's own soak the
  evening before the session that cooks it, or a reminder of kind soak).
- soak_long: a soak before the cook, but longer than SOAK_MAX_H before it (SOAK_FRIDGE_MAX_H for a
  reminder of kind soak-fridge, in the fridge).
- thaw_long: a thaw (the app's, a container out of the freezer, a reminder of kind thaw) whose
  cook comes more than THAW_MAX_H after it: the session that cooks the dish, a reminder of kind
  cook, or the meal. The app's own thaw is food marked `frozen`, served with the meal (bread), so
  its meal is what uses it, also for a lunch a session cooked days before.
- thaw_uncooked: a thaw of kind thaw from the chat after the last cook of its dish (a lunch a
  session cooked before the thaw), or with nothing to cook it: what comes out waits uncooked.
- avoided: a recipe on some meal that uses a food someone avoids. The planner never picks it, so
  it never reaches the menu and nothing else ever looks at it again: swap the food or retire it.
- going_off: food in the pantry (pantry.py) that goes off before any planned dish uses it up,
  the plan cooking as planned up to the end of next week.
- unused: food ticked as bought that no dish uses (unused_food), as the AI already sees it; food
  the going_off check names is left out of it, so nothing is said twice.

A step is done only when someone marked it done (tasks.py, its buttons or a change block). A
session, soak, thaw or move of the ASK_DAYS before today that was marked not done counts as never
made, so the chains and uncooked see what is missing; one nobody marked counts as not made either,
and what it leaves missing is said as one problem of its own (a session that cooks a lunch still to
come, however long ago; tasks.tracked, so nothing from before the buttons existed):
- unconfirmed: a past step nobody marked, that a dish still to come counts on. Ask whether it was
  done; if not, the rest of the week is reorganised.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from . import tasks
from .db import DB
from .model import WEEKDAYS, Data
from .planner import (
    MEAL_HOURS,
    MEALS,
    Day,
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
    "no_session",
    "no_cook",
    "no_soak",
    "soak_long",
    "thaw_long",
    "unconfirmed",
    "avoided",
    "stale_reminder",
    "going_off",
    "unused",
)
AHEAD_KEEPS_DAYS = (
    3  # what a session cooks for a later dish (pulses, a roast pepper) keeps this long
)
REMINDER_LEAD_DAYS = 3  # a reminder further ahead than this of its step is not about that dish
SOAK_MAX_H = 24  # a dried pulse soaks at most this long before it is cooked, out of the fridge
SOAK_FRIDGE_MAX_H = 48  # in the fridge (a reminder of kind soak-fridge)
THAW_MAX_H = 48  # thawed food is cooked within this long of the thaw (fish within 24 to 48 h)
SOAKS = ("soak", "soak-fridge")


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
    said = db.task_states()  # db.mark_task forgets the old ones, so this stays small
    unsure: dict[str, tuple[dt.date, str, list[str]]] = {}  # task key -> (day, kind, dishes)
    out += _lunches(data, db, plans, today, dates, said, unsure)
    out += _prep(data, db, plans, dates, offered)
    # a step of the last days nobody marked done counts as not made; when only that breaks a
    # chain, the problem is that nobody said, not the chain
    window = [
        x
        for x in tasks.tracked(data, db, today - dt.timedelta(days=tasks.ASK_DAYS), today)
        if x.kind in tasks.STEPS and x.day < today
    ]
    undone = {x.key for x in window if said.get(x.key) == tasks.NOT_DONE}
    unknown = {x.key: x for x in window if x.key not in said}
    strict = _chains(data, db, plans, today, dates, offered, undone | set(unknown))
    loose = (
        {p.key for p in _chains(data, db, plans, today, dates, offered, undone)}
        if unknown
        else {p.key for p in strict}
    )
    for p in strict:
        rid, _, date = p.key.partition(":")[2].partition(":")
        on = [
            x
            for x in unknown.values()
            if (x.recipe, x.eat and x.eat.isoformat()) == (rid, date)
            or any(r == rid and e.isoformat() == date for r, e in x.dishes)
        ]
        if p.key in loose or not on:
            out.append(p)
            continue
        name = data.recipes[rid].name if rid in data.recipes else rid
        for x in on:
            unsure.setdefault(x.key, (x.day, x.kind, []))[2].append(f"{name} [{rid}] for {date}")
    out += _unsure(data, unsure)
    out += _avoided(data)
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


def _avoided(data: Data) -> list[Problem]:
    """Recipes left out of every plan for good by a food someone avoids. Who avoids it is not said:
    the line goes to the group."""
    avoided = data.household.avoided()
    out = []
    for r in sorted(data.recipes.values(), key=lambda r: r.id):
        bad = sorted({i.id for i in r.ingredients if i.id in avoided})
        if not r.meals or not bad:
            continue
        food = ", ".join(data.ingredients[i].name for i in bad)
        out.append(
            Problem(
                "avoided",
                f"avoided:{r.id}:{','.join(bad)}",
                f"Recipe {r.id} uses {', '.join(bad)}, which someone avoids, so the planner never "
                "picks it: swap that food in the recipe for one everyone eats, or retire the "
                "recipe with `meals: []` when the food is what the dish is about.",
                {"dish": r.name, "food": food},
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


def _unsure(data: Data, unsure: dict) -> list[Problem]:
    out = []
    for key, (day, kind, dishes) in sorted(unsure.items()):
        dishes = list(dict.fromkeys(dishes))
        out.append(
            Problem(
                "unconfirmed",
                f"unconfirmed:{key}",
                f"Nobody said whether the task {key} ({kind}, {_day(day)}) was done, and "
                f"{'; '.join(dishes)} count on it. Until someone marks it done it counts as not "
                "done. Fix: ask the household whether it was done (the morning message asks with "
                "buttons); if it was not, reorganise the rest of the week: `set` those dishes "
                "again with a new cook, or move their steps, and nothing made by it (a dish, a "
                "soaked pulse, thawed food) is said to be ready.",
                {"date": day, "dish": ", ".join(d.split(" [")[0] for d in dishes)},
            )
        )
    return out


def _span(first: dt.date, last: dt.date) -> list[dt.date]:
    return [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]


def _keeps(data: Data, rid: str, cook: dt.date, eat: dt.date) -> bool:
    r = data.recipes[rid]
    return (eat - cook).days <= r.fridge_days or r.freezes


def _lunches(
    data: Data,
    db: DB,
    plans: Plans,
    today: dt.date,
    dates: list[dt.date],
    said: dict,
    unsure: dict,
) -> list[Problem]:
    """said: the tasks' answers (db.task_states); unsure: where a session nobody marked goes. A
    session counts as unsure however long ago it was, while a lunch still to come counts on it:
    the containers' messages say "if" of it and carry its buttons until someone answers."""
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
        elif cook < today:
            key = _session_key(plans, day)
            state = said.get(key) if key else None
            if key and state is None and not tasks.untracked(db, cook):  # nobody said
                unsure.setdefault(key, (cook, "cook", []))[2].append(
                    f"{r.name} [{r.id}] for {d.isoformat()}"
                )
            elif key is None or state == tasks.NOT_DONE:
                why = (
                    "but its session was marked not done"
                    if key
                    else "but no batch session that day cooked it"
                )
                out.append(
                    Problem(
                        "uncooked",
                        f"uncooked:{d.isoformat()}",
                        f"Lunch on {_day(d)}, {r.name} [{r.id}], says it was cooked on "
                        f"{cook.isoformat()}, {why}, so nobody did. Fix: `set` it again with "
                        f"`cook:` a day from {today.isoformat()} on, or without cook (cooked that "
                        "morning), and reorganise what else that session cooked.",
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
        if x not in plans.paused and plans.sessions_on(x):
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
            if r is None or not r.ahead or (meal == "lunch" and _cooked(plans, day)):
                continue  # a dried pulse's soak and cook are the chain checks' (_chains)
            session = [a.step for a in r.ahead if a.session]
            before = _sessions_before(plans, d) if session else []
            # a step `days` ahead is due by the end of that day, one on the day before the meal
            due = [
                at(d - dt.timedelta(days=a.days - 1)) if a.days else at(d, MEAL_HOURS[meal][0])
                for a in r.ahead
                if not a.session
            ]
            first = max((a.days for a in r.ahead if not a.session), default=0)
            cooked = before[-1] if before else d - dt.timedelta(days=first)
            deadline = min([at(d, MEAL_HOURS[meal][0]), *due])
            times = linked.get((r.id, d.isoformat()), [])
            if any(
                at(cooked - dt.timedelta(days=REMINDER_LEAD_DAYS)) <= t <= deadline for t in times
            ):
                continue
            what = r.name + f" [{r.id}], {meal} on {_day(d)}"
            link = f"`dish {r.id} {d.isoformat()}`"
            if session and not before:
                same = bool(plans.sessions_on(d))
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
            steps = [
                a.step
                + (
                    f" (in the session of {before[-1].isoformat()})"
                    if a.session
                    else f" (by {(d - dt.timedelta(days=a.days)).isoformat()})"
                    if a.days
                    else ""
                )
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


def _chains(
    data: Data,
    db: DB,
    plans: Plans,
    today: dt.date,
    dates: list[dt.date],
    offered,
    drop: set[str] = frozenset(),
) -> list[Problem]:
    """The steps that hang on each other, from the task list (tasks.py): a dried pulse soaked and
    then cooked before its meal, thawed food cooked soon after the thaw, a lunch cooked by the
    session it counts on. Each link that is missing or too far apart is one problem. drop: the
    keys of the tasks that count as not made (past, and not marked done)."""
    zone = ZoneInfo(data.household.timezone)

    def at(d: dt.date, hm: str = "00:00") -> dt.datetime:
        h, m = (int(x) for x in hm.split(":"))
        return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=zone)

    def when(x: tasks.Task) -> dt.datetime:
        return at(x.day, x.at)

    def hm(t: dt.datetime) -> str:
        return f"{_day(t.date())} {t:%H:%M}"

    def hours(a: dt.datetime, b: dt.datetime) -> float:
        # in real time: the same zone on both sides subtracts clock faces, an hour off across DST
        return (b.astimezone(dt.UTC) - a.astimezone(dt.UTC)).total_seconds() / 3600

    start = at(today)
    first = today - dt.timedelta(days=3)  # a soak or thaw a few days back still counts
    found = tasks.tasks_for(data, db, first, dates[-1])
    auto = [x for x in found if x.source == "auto" and x.key not in drop]
    gone = {f"r:{int(r['cancel'])}" for r in offered if str(r.get("cancel", "")).isdigit()}
    gone |= drop
    chat = [x for x in tasks.chat_tasks(data, db) if x.key not in gone]
    chat += tasks.offered_tasks(data, [r for r in offered if "cancel" not in r])
    steps: dict[tuple[str, dt.date], list[tasks.Task]] = {}
    for x in chat:
        if x.recipe and x.eat:
            steps.setdefault((x.recipe, x.eat), []).append(x)
    sessions: dict[tuple[str, dt.date], list[dt.datetime]] = {}
    for x in auto:
        if x.kind == "cook":
            for rid, eat in x.dishes:
                sessions.setdefault((rid, eat), []).append(when(x))
    # the app's own soak, the evening before the session that cooks the dish
    soaked = {(x.recipe, x.key.split(":")[1]) for x in auto if x.kind == "soak"}

    out: list[Problem] = []
    for d in dates:
        day = plans.day(d)
        if day is None:
            continue
        for meal in MEALS:
            r = data.recipes.get(getattr(day, meal) or "")
            if r is None:
                continue
            eat = at(d, MEAL_HOURS[meal][0])
            what = f"{r.name} [{r.id}], {meal} on {_day(d)}"
            link = f"`dish {r.id} {d.isoformat()}"
            mine = steps.get((r.id, d), [])
            by_session = [c for c in sessions.get((r.id, d), []) if c < eat]
            counts = meal == "lunch" and day.lunch_cook < d  # cooked in a session before its day
            if counts and day.lunch_cook < today:
                continue  # already past: the uncooked check says when no session cooked it
            if counts and not by_session:
                out.append(
                    Problem(
                        "no_session",
                        f"no_session:{r.id}:{d.isoformat()}",
                        f"Lunch on {_day(d)}, {r.name} [{r.id}], counts on being cooked on "
                        f"{_day(day.lunch_cook)}, but no session cooks it for that day, "
                        "so nobody will. Fix: `set` it again with `cook:` a day a session comes, "
                        "or without cook (cooked that morning).",
                        {"dish": r.name, "date": d, "cook": day.lunch_cook},
                    )
                )
                continue
            pulses = [
                data.ingredients[i.id].name
                for i in r.ingredients
                if i.id in data.ingredients and data.ingredients[i.id].soak
            ]
            if not pulses:
                continue
            food = ", ".join(pulses)
            cooks = by_session + [when(x) for x in mine if x.kind == "cook" and when(x) < eat]
            if not counts and not r.ahead and r.kind != "assemble":
                cooks.append(eat)  # cooked for the meal itself
            untyped = sorted(x.reminder for x in mine if x.kind == "note" and x.reminder)
            legacy = (
                f" Reminders {untyped} about this dish say no step, so they count as neither a "
                "soak nor a cook: cancel each and list it again with the step at the end of its "
                "dish field."
                if untyped
                else ""
            )
            if not cooks:
                out.append(
                    Problem(
                        "no_cook",
                        f"no_cook:{r.id}:{d.isoformat()}",
                        f"{what}, uses {food}, a dried pulse, and nothing cooks it before the meal: "
                        "no session cooks it for that day and no reminder of kind cook comes "
                        f"before {hm(eat)}"
                        + (", and the recipe is only assembled" if r.kind == "assemble" else "")
                        + f". Fix: a reminders line to cook it ahead with {link} cook` and one to "
                        f"soak it in the {SOAK_MAX_H} h before that cook with {link} soak`, or "
                        f"`set` another dish.{legacy}",
                        {"dish": r.name, "date": d, "food": food},
                    )
                )
                continue
            cook = max(cooks)
            if cook < start:
                continue  # cooked already, or it was to be: what is past is not checked again
            soaks = [(when(x), x.kind) for x in mine if x.kind in SOAKS]

            if any(
                (c in by_session and (r.id, c.date().isoformat()) in soaked)  # the app's own soak
                or any(0 < hours(s, c) <= _soak_max(k) for s, k in soaks)
                for c in cooks
            ):
                continue
            early = [(s, k) for s, k in soaks if s < cook]
            if not early:
                out.append(
                    Problem(
                        "no_soak",
                        f"no_soak:{r.id}:{d.isoformat()}",
                        f"{what}, uses {food}, a dried pulse cooked on {hm(cook)}, and no soak "
                        f"comes in the {SOAK_MAX_H} h before that cook. Fix: a reminders line to "
                        f"soak it then with {link} soak`, or up to {SOAK_FRIDGE_MAX_H} h before "
                        f"in the fridge with {link} soak-fridge` (and say fridge in its text), or "
                        f"`set` another dish.{legacy}",
                        {"dish": r.name, "date": d, "food": food, "cook": cook.date()},
                    )
                )
                continue
            s, k = max(early)
            fridge = k == "soak-fridge"
            out.append(
                Problem(
                    "soak_long",
                    f"soak_long:{r.id}:{d.isoformat()}",
                    f"{what}: {food} soaks from {hm(s)} and is cooked on {hm(cook)}, "
                    f"{hours(s, cook):.0f} h in water, more than the {_soak_max(k)} h it may soak"
                    + (" in the fridge" if fridge else " out of the fridge")
                    + ". Fix: move the soak (cancel its reminder and list it again) to at most "
                    f"{SOAK_MAX_H} h before the cook"
                    + (
                        ""
                        if fridge
                        else f", or say to soak it in the fridge, up to {SOAK_FRIDGE_MAX_H} h, "
                        f"with {link} soak-fridge`"
                    )
                    + ", or move the cook earlier.",
                    {
                        "dish": r.name,
                        "date": d,
                        "food": food,
                        "soak": s.date(),
                        "cook": cook.date(),
                    },
                )
            )
    out += _thaws(data, plans, today, auto + chat, sessions, steps, at, when, hm, hours)
    return out


def _soak_max(kind: str) -> int:
    return SOAK_FRIDGE_MAX_H if kind == "soak-fridge" else SOAK_MAX_H


def _thaws(data, plans, today, found, sessions, steps, at, when, hm, hours) -> list[Problem]:
    """Every thaw (the app's own, a container out of the freezer, a reminder of kind thaw) needs
    its cooking within THAW_MAX_H of it: a session that cooks the dish, a reminder of kind cook, or
    the meal. A move to the fridge or the freezer is not looked at: a move to the freezer has no
    clock."""
    out: list[Problem] = []
    seen: set[str] = set()
    for x in sorted(found, key=lambda x: (when(x), x.key)):
        if x.kind not in ("thaw", "defrost") or not x.recipe or not x.eat or x.eat < today:
            continue
        t0 = when(x)
        if t0 < at(today - dt.timedelta(days=3)):
            continue
        day = plans.day(x.eat)
        meal = next((m for m in MEALS if day and getattr(day, m) == x.recipe), None)
        if meal is None:
            continue  # the menu dropped the dish: the stale reminder check says so
        eat = at(x.eat, MEAL_HOURS[meal][0])
        counts = meal == "lunch" and day.lunch_cook < x.eat
        if x.kind == "defrost" or x.source == "auto":
            # a cooked container is warmed up for the meal; the app's own thaw is food marked
            # `frozen`, served with the meal (bread), so the meal is what uses it
            cooks = [eat]
        else:
            cooks = [c for c in sessions.get((x.recipe, x.eat), []) if c < eat]
            cooks += [
                when(y)
                for y in steps.get((x.recipe, x.eat), [])
                if y.kind == "cook" and when(y) < eat
            ]
            if not counts:
                cooks.append(eat)
        r = data.recipes.get(x.recipe)
        if r is None:
            continue  # a recipe since deleted: a crash here would silence every check
        which = f" (reminder {x.reminder})" if x.reminder else ""
        after = [c for c in cooks if c >= t0]
        if not after:  # the dish is cooked before this thaw: what comes out of the freezer waits
            key = f"thaw_uncooked:{x.recipe}:{x.eat.isoformat()}"
            if key in seen:
                continue
            seen.add(key)
            done = max(cooks) if cooks else None
            out.append(
                Problem(
                    "thaw_uncooked",
                    key,
                    f"{r.name} [{r.id}], {meal} on {_day(x.eat)}, is thawed on {hm(t0)}{which}, "
                    + (f"after it is cooked on {hm(done)}" if done else "and nothing cooks it")
                    + ", so nothing cooks what comes out of the freezer. Fix: move the thaw to "
                    f"the {THAW_MAX_H} h before the cook (cancel its reminder and list it again), "
                    "or cancel it if nothing raw is thawed (food marked frozen that is served "
                    "with the meal, like bread, the app already says the evening before).",
                    {"dish": r.name, "date": x.eat, "thaw": t0.date()},
                )
            )
            continue
        cook = min(after)
        key = f"thaw_long:{x.recipe}:{x.eat.isoformat()}"
        if hours(t0, cook) <= THAW_MAX_H or key in seen:
            continue
        seen.add(key)
        out.append(
            Problem(
                "thaw_long",
                key,
                f"{r.name} [{r.id}], {meal} on {_day(x.eat)}, is thawed on {hm(t0)}{which} and "
                f"cooked on {hm(cook)}, {hours(t0, cook):.0f} h later: thawed food is cooked "
                f"within {THAW_MAX_H} h (fish within 24 to 48 h). Fix: move the thaw to at most "
                f"{THAW_MAX_H} h before the cook (cancel its reminder and list it again), or move "
                "the cook earlier; if it is already out of the freezer, cook it now or `set` the "
                "dish on an earlier day.",
                {"dish": r.name, "date": x.eat, "thaw": t0.date(), "cook": cook.date()},
            )
        )
    return out


def _session_key(plans: Plans, day: Day) -> str | None:
    """The task key of the session on the lunch's cook date that cooks it for that day."""
    return next(iter(tasks.session_keys(plans, day)), None)


def _cooked(plans: Plans, day: Day) -> bool:
    """A session on the lunch's cook date, of any plan (one may come before its plan starts),
    cooked that dish for that day."""
    return any(
        x.recipe == day.lunch and day.date in x.eat
        for _, s in plans.sessions_on(day.lunch_cook)
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
                f"`set` a dish with it on or before {last.isoformat()}, or say to freeze it; "
                f"once it is frozen, or its label gives a later day, `pantry` says so.",
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
