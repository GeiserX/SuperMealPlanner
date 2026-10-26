"""Changes to the menu from the chat: the `plan:` list of a change block.

    plan:
      - set: {date: 2026-01-14, meal: dinner, recipe: tuna-salad}
      - swap: {a: 2026-01-14, b: 2026-01-16, meal: dinner}   # without meal: the whole day
      - pause: {from: 2026-01-12, to: 2026-01-18}            # eat off the plan; ends by itself
      - resume: {from: 2026-01-15, to: 2026-01-18}
      - regenerate: {from: 2026-01-19}                       # a new draw from that Monday
      - shift: {from: 2026-01-12, weeks: 1}                  # the menu from that day on, later
      - undo: true                                           # the last menu change

Plans are generated files under state/, outside the data repo's git, so a menu change needs no
token: it is written there, after the planner's validator found no hard rule newly broken. The
plans as they were are kept beside them, so the last change can be undone.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .model import Data
from .planner import (
    PLAN_LOCK,
    Day,
    Dish,
    Plan,
    PlanError,
    Session,
    _eligible,
    generate,
    load_plan,
    paused_days,
    plans_dir,
    repair,
    save_paused,
    validate,
)

log = logging.getLogger("supermealplanner")

MEALS = ("breakfast", "lunch", "side", "snack", "dinner")
FIELDS = {
    "set": {"date", "meal", "recipe", "cook"},
    "swap": {"a", "b", "meal"},
    "pause": {"from", "to"},
    "resume": {"from", "to"},
    "regenerate": {"from"},
    "shift": {"from", "weeks"},
}
SHIFT_MAX = 4  # weeks one shift may move the menu
PAUSE_MAX = 92  # days in one pause: a mistyped year must not silence the plan for good


class MenuError(Exception):
    """A menu change that is not allowed or would break a hard rule. Safe to show."""


@dataclass
class Op:
    kind: str  # set, swap, pause, resume, regenerate, undo
    args: dict  # dates already parsed


def _date(value, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise MenuError(f"{what} must be a date like 2026-01-14") from None


def parse(raw, data: Data, new_recipe: str | None = None) -> list[Op]:
    """Check the `plan:` list of a change block. new_recipe: the id of a recipe the same block
    adds, which a `set` may already use. Raises MenuError with the reason."""
    if not isinstance(raw, list) or not raw:
        raise MenuError("plan must be a list of menu changes")
    out = []
    for item in raw:
        if not isinstance(item, dict) or len(item) != 1:
            raise MenuError("each menu change is one of set, swap, pause, resume, regenerate, undo")
        kind, args = next(iter(item.items()))
        if kind == "undo":
            if len(raw) > 1:
                raise MenuError("undo goes alone")
            out.append(Op("undo", {}))
            continue
        if kind not in FIELDS:
            raise MenuError(f"{kind} is not a menu change; use {', '.join([*FIELDS, 'undo'])}")
        if not isinstance(args, dict) or set(args) - FIELDS[kind]:
            raise MenuError(f"{kind} takes {', '.join(sorted(FIELDS[kind]))}")
        meal = args.get("meal")
        if meal is not None and meal not in MEALS:
            raise MenuError(f"{meal} is not a meal; use {', '.join(MEALS)}")
        if kind == "set":
            rid = str(args.get("recipe", ""))
            r = data.recipes.get(rid)
            if meal is None:
                raise MenuError("set needs date, meal and recipe")
            if r is None and rid != new_recipe:
                raise MenuError(f"no recipe {rid}")
            if meal == "side" and r is not None and not r.has("side"):
                raise MenuError(f"{rid} is not a side salad")
            day = _date(args.get("date"), "date")
            op = Op(kind, {"date": day, "meal": meal, "recipe": rid})
            if "cook" in args:  # the day it is cooked, when the household said one
                cook = _date(args["cook"], "cook")
                if meal != "lunch":
                    raise MenuError("cook is the day a lunch is cooked; other meals are made fresh")
                if cook > day:
                    raise MenuError(f"{rid} cannot be cooked on {cook}, after it is eaten on {day}")
                op.args["cook"] = cook
            out.append(op)
        elif kind == "swap":
            a, b = _date(args.get("a"), "a"), _date(args.get("b"), "b")
            if a == b:
                raise MenuError("swap needs two different days")
            out.append(Op(kind, {"a": a, "b": b, "meal": meal}))
        elif kind == "regenerate":
            start = _date(args.get("from"), "from")
            if start.weekday() != 0:
                raise MenuError(
                    f"a new draw starts on a Monday and {start} is not one; "
                    "change single dishes with set, or pause the days before it"
                )
            out.append(Op(kind, {"from": start}))
        elif kind == "shift":
            weeks = args.get("weeks")
            if not isinstance(weeks, int) or isinstance(weeks, bool) or not 1 <= weeks <= SHIFT_MAX:
                raise MenuError(f"shift moves the menu 1 to {SHIFT_MAX} whole weeks")
            out.append(Op(kind, {"from": _date(args.get("from"), "from"), "weeks": weeks}))
        else:
            first, last = _date(args.get("from"), "from"), _date(args.get("to"), "to")
            if not first <= last or (last - first).days >= PAUSE_MAX:
                raise MenuError(f"{kind}: from {first} to {last} is not 1 to {PAUSE_MAX} days")
            out.append(Op(kind, {"from": first, "to": last}))
    return out


def describe(ops: list[Op], data: Data, t, names: dict[str, str] | None = None) -> list[str]:
    """One plain line per menu change, in the household's language. names: recipes not in the data
    yet (the one the same block adds)."""

    def name(rid: str | None) -> str:
        if rid in data.recipes:
            return data.recipes[rid].name
        return (names or {}).get(rid) or rid or t("change_none")

    lines = []
    for op in ops:
        a = op.args
        if op.kind == "set":
            day = _find(_load(data), a["date"], missing_ok=True)
            lines.append(
                t(
                    "change_plan_set",
                    day=t.date(a["date"]),
                    meal=t("meal_" + a["meal"]).lower(),
                    old=name(getattr(day[2], a["meal"]) if day else None),
                    new=name(a["recipe"]),
                )
                + (t("change_plan_cooked", day=t.date(a["cook"])) if a.get("cook") else "")
            )
        elif op.kind == "swap":
            key = "change_plan_swap_meal" if a["meal"] else "change_plan_swap"
            meal = t("meal_" + a["meal"]).lower() if a["meal"] else ""
            lines.append(t(key, a=t.date(a["a"]), b=t.date(a["b"]), meal=meal))
        elif op.kind == "regenerate":
            lines.append(t("change_plan_regenerate", day=t.date(a["from"])))
        elif op.kind == "shift":
            back = a["from"] + dt.timedelta(weeks=a["weeks"])
            lines.append(t("change_plan_shift", day=t.date(a["from"]), back=t.date(back)))
        elif op.kind == "undo":
            lines.append(t("change_plan_undo"))
        else:
            key = f"change_plan_{op.kind}"
            lines.append(t(key, first=t.date(a["from"]), last=t.date(a["to"])))
    return [f"• {x}" for x in lines]


# ---------------------------------------------------------------- apply


def _load(data: Data) -> dict[Path, Plan]:
    return {p: load_plan(p) for p in sorted(plans_dir(data).glob("*.yaml"))}


def _find(plans: dict[Path, Plan], day: dt.date, missing_ok: bool = False):
    """(path, plan, day) of the newest plan that has `day`."""
    hit = None
    for path, plan in plans.items():
        if plan.start <= day < plan.end and plan.day(day):
            hit = (path, plan, plan.day(day))
    if hit is None and not missing_ok:
        raise MenuError(f"no plan covers {day}")
    return hit


def _put_lunch(plan: Plan, day: Day, recipe: str, cook: dt.date) -> None:
    """Make `recipe`, cooked on `cook`, the lunch of one day: out of the container it was in, into
    the one of that dish and cook date, or a new one."""
    for s in plan.sessions:
        for x in s.dishes:
            if day.date in x.eat:
                x.eat.remove(day.date)
        s.dishes = [x for x in s.dishes if x.eat]
    plan.sessions = [s for s in plan.sessions if s.dishes]
    day.lunch, day.lunch_cook = recipe, cook
    for s in plan.sessions:
        for x in s.dishes:
            if x.recipe == recipe and x.cook == cook:
                x.eat = sorted({*x.eat, day.date})
                return
    session = next(
        (s for s in plan.sessions if s.date == cook and (s.morning or cook < day.date)), None
    )
    if session is None:  # cooked the morning it is eaten when no afternoon comes before
        session = Session(date=cook, morning=cook == day.date)
        plan.sessions.append(session)
        plan.sessions.sort(key=lambda s: (s.date, not s.morning))
    session.dishes.append(Dish(recipe=recipe, cook=cook, eat=[day.date]))


def _salad(data: Data, day: Day) -> None:
    """Keep the daily salad when a main dish changed: add a side salad if neither main is one."""
    if not data.household.rules.salad_daily or day.side:
        return
    mains = [data.recipes.get(day.lunch), data.recipes.get(day.dinner)]
    if any(r is not None and r.has("salad") for r in mains):
        return
    sides = sorted(
        r.id
        for r in data.recipes.values()
        if r.has("side") and r.has("salad") and _eligible(data, r, "lunch", day.date.month)
    )
    if sides:
        day.side = sides[day.date.toordinal() % len(sides)]


def _set(data: Data, plans, op: Op, today: dt.date) -> set[Path]:
    a = op.args
    path, plan, day = _find(plans, a["date"])
    r = data.recipes.get(a["recipe"])
    if r is None:
        raise MenuError(f"no recipe {a['recipe']}")
    if a["meal"] != "lunch":
        setattr(day, a["meal"], r.id)
    else:
        # in the batch session that cooked the lunch it replaces, when that is still to come and
        # the dish keeps until the day; otherwise cooked the same morning
        if a.get("cook"):  # the day the household said: kept to, or refused with the reason
            cook = a["cook"]
            if cook < today:
                raise MenuError(f"{cook} is already past: {r.name} cannot be cooked then")
            gap = (day.date - cook).days
            if gap > r.fridge_days and not r.freezes:
                raise MenuError(
                    f"{r.name} keeps {r.fridge_days} days in the fridge: cooked on {cook} it "
                    f"does not reach {day.date}"
                )
        else:
            # in the batch session that cooked the lunch it replaces, when that is still to come
            # and the dish keeps until the day; otherwise cooked the same morning
            cook = day.lunch_cook
            gap = (day.date - cook).days
            keeps = gap <= r.fridge_days or r.freezes
            if r.kind != "batch" or cook < today or not keeps:
                cook = day.date
        _put_lunch(plan, day, r.id, cook)
    _salad(data, day)
    return {path}


def _swap(data: Data, plans, op: Op) -> set[Path]:
    (pa, plan_a, a), (pb, plan_b, b) = _find(plans, op.args["a"]), _find(plans, op.args["b"])
    for meal in [op.args["meal"]] if op.args["meal"] else MEALS:
        if meal != "lunch":
            va, vb = getattr(a, meal), getattr(b, meal)
            setattr(a, meal, vb)
            setattr(b, meal, va)
            continue
        la, lb = (a.lunch, a.lunch_cook), (b.lunch, b.lunch_cook)
        for plan, day, (rid, cook) in ((plan_a, a, lb), (plan_b, b, la)):
            # a lunch cooked after its new day, or by another plan, is cooked that morning
            _put_lunch(plan, day, rid, cook if plan.start <= cook <= day.date else day.date)
    _salad(data, a)
    _salad(data, b)
    return {pa, pb}


def _regenerate(data: Data, plans, op: Op) -> set[Path]:
    start = op.args["from"]
    path = plans_dir(data) / f"{start.isoformat()}.yaml"
    seed = plans[path].seed + 1000 if path in plans else None  # past every seed that draw tried
    touched = {path}
    for p, plan in list(plans.items()):
        if plan.start >= start:  # replaced by the new draw
            del plans[p]
            touched.add(p)
        elif plan.end > start:  # keeps its days before the new draw, and cooks only for them
            plan.days = [d for d in plan.days if d.date < start]
            for s in plan.sessions:
                for x in s.dishes:
                    x.eat = [e for e in x.eat if e < start]
                s.dishes = [x for x in s.dishes if x.eat]
            plan.sessions = [s for s in plan.sessions if s.dishes]
            touched.add(p)
    previous = max(plans.values(), key=lambda p: p.end) if plans else None
    try:
        from .calendar import busy_afternoons

        busy = busy_afternoons(data.household, start, start + dt.timedelta(days=28))
    except Exception as e:  # noqa: BLE001  a calendar outage must not stop the new draw
        log.warning("calendar unavailable, planning without it: %s", type(e).__name__)
        busy = {}
    try:
        from .db import DB

        ratings = DB(data.state / "supermealplanner.db").ratings()
        plans[path] = generate(
            data, start, 4, busy=busy, previous=previous, seed=seed, ratings=ratings
        )
    except PlanError as e:
        raise MenuError(str(e)[:300]) from None
    return touched


def _shift(data: Data, plans, op: Op) -> set[Path]:
    """Move every planned day from `from` on later by whole weeks, so weekdays (batch days, the
    injection day) still line up. The plan that holds `from` keeps its start and grows by those
    weeks, so the paused gap stays covered and the hourly check draws nothing for it; a plan that
    starts later moves whole. A lunch cooked before `from` for a day that moves is cooked again
    on `from` at the earliest (a paused day, so the first day back cooks it). Calendar marks stay
    with their dates, not with the dishes."""
    first, delta = op.args["from"], dt.timedelta(weeks=op.args["weeks"])

    def cook(c: dt.date) -> dt.date:
        return max(c + delta, first)

    touched = set()
    for path, plan in list(plans.items()):
        if plan.end <= first:
            continue
        touched.add(path)
        for d in plan.days:
            if d.date >= first:
                d.date, d.lunch_cook, d.busy = d.date + delta, cook(d.lunch_cook), []
        sessions: dict[tuple[dt.date, bool], Session] = {}
        for s in plan.sessions:
            for x in s.dishes:
                stay = [e for e in x.eat if e < first]
                move = [e + delta for e in x.eat if e >= first]
                if stay:
                    key = (s.date, s.morning)
                    sessions.setdefault(key, Session(s.date, s.morning, list(s.busy)))
                    sessions[key].dishes.append(Dish(x.recipe, x.cook, stay))
                if move:
                    c = cook(x.cook)
                    key = (c, c == move[0])
                    sessions.setdefault(key, Session(c, c == move[0]))
                    sessions[key].dishes.append(Dish(x.recipe, c, move))
        plan.sessions = sorted(sessions.values(), key=lambda s: (s.date, not s.morning))
        if plan.start > first:  # starts later: the whole plan moves, and so does its file
            plan.start += delta
            del plans[path]
            new = plans_dir(data) / f"{plan.start.isoformat()}.yaml"
            plans[new] = plan
            touched.add(new)
        else:
            plan.weeks += op.args["weeks"]
    return touched


def _undated(errors) -> set[str]:
    """Validator errors without their dates: a shift moves an old error, it does not add one."""
    return {re.sub(r"\d{4}-\d{2}-\d{2}", "", e) for e in errors}


def _undo_dir(data: Data) -> Path:
    return data.state / "menu-undo"


def _snapshot(data: Data) -> None:
    """Keep the plans and the paused days as they are now, replacing the last copy."""
    keep = _undo_dir(data)
    shutil.rmtree(keep, ignore_errors=True)
    (keep / "plans").mkdir(parents=True)
    for p in plans_dir(data).glob("*.yaml"):
        shutil.copy2(p, keep / "plans" / p.name)
    paused = data.state / "paused.yaml"
    if paused.exists():
        shutil.copy2(paused, keep / "paused.yaml")


def _undo(data: Data) -> None:
    keep = _undo_dir(data)
    if not (keep / "plans").is_dir():
        raise MenuError("there is no menu change to undo")
    for p in plans_dir(data).glob("*.yaml"):
        p.unlink()
    plans_dir(data).mkdir(parents=True, exist_ok=True)
    for p in (keep / "plans").glob("*.yaml"):
        shutil.copy2(p, plans_dir(data) / p.name)
    paused = data.state / "paused.yaml"
    paused.unlink(missing_ok=True)
    if (keep / "paused.yaml").exists():
        shutil.copy2(keep / "paused.yaml", paused)
    shutil.rmtree(keep, ignore_errors=True)


def retire(data: Data, pairs: set[tuple[str, str]], start: dt.date) -> int:
    """A recipe was taken out of a meal: give the days planned from `start` on another dish for it.
    Returns how many days changed. The plans as they were are kept for undo."""
    with PLAN_LOCK:
        changed, days = {}, 0
        for path, plan in _load(data).items():
            before = [asdict(d) for d in plan.days]
            repair(data, plan, start, retired=pairs)
            moved = sum(1 for old, d in zip(before, plan.days, strict=True) if old != asdict(d))
            if moved:
                changed[path] = plan
                days += moved
        if changed:
            _snapshot(data)
            for path, plan in changed.items():
                plan.save(path)
        return days


def apply(data: Data, ops: list[Op], today: dt.date) -> None:
    """Make the menu changes, all or none. Raises MenuError when a day has no plan or a hard rule
    of the validator would newly break; then nothing is written. The limit on how often a dish
    is cooked is not one of them here: it shapes a draw, and this dish was asked for."""
    with PLAN_LOCK:
        if [op.kind for op in ops] == ["undo"]:
            _undo(data)
            return
        plans = _load(data)
        known = {p: set(validate(data, plan, repeats=False)) for p, plan in plans.items()}
        was = paused_days(data)
        paused = set(was)
        touched: set[Path] = set()
        for op in ops:
            if op.kind == "set":
                touched |= _set(data, plans, op, today)
            elif op.kind == "swap":
                touched |= _swap(data, plans, op)
            elif op.kind == "regenerate":
                touched |= _regenerate(data, plans, op)
            elif op.kind == "shift":
                touched |= _shift(data, plans, op)
                # the days the menu left are paused, so nothing new is drawn for them; paused
                # days after `from` move with the dishes
                first, delta = op.args["from"], dt.timedelta(weeks=op.args["weeks"])
                gap = {first + dt.timedelta(days=i) for i in range(delta.days)}
                paused = {d if d < first else d + delta for d in paused} | gap
            else:
                span = (op.args["to"] - op.args["from"]).days + 1
                days = {op.args["from"] + dt.timedelta(days=i) for i in range(span)}
                paused = paused | days if op.kind == "pause" else paused - days
        shifted = any(op.kind == "shift" for op in ops)
        before = _undated(e for errs in known.values() for e in errs)
        for p in sorted(touched & set(plans)):
            errs = validate(data, plans[p], repeats=False)
            if shifted:
                new = [e for e in errs if _undated([e]) - before]
            else:
                new = [e for e in errs if e not in known.get(p, ())]
            if new:
                raise MenuError("; ".join(new[:3]))
        if not touched and paused == was:
            return  # nothing changes: the copy for undo stays the one of the last real change
        _snapshot(data)
        for p in touched:
            if p in plans:
                plans[p].save(p)
            else:
                p.unlink(missing_ok=True)
        if paused != was:
            # days long gone are dropped, so the file never grows
            save_paused(data, {d for d in paused if d >= today - dt.timedelta(days=35)})
